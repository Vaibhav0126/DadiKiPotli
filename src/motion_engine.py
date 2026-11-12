"""Module 3b: animate the hero scenes, Ken Burns the rest.

Real image-to-video is the only paid step in the pipeline, so it is applied
only to the handful of scenes that gain most from motion (chosen in Module 1),
guarded by a hard spend cap, and cached so a re-render never pays twice.

Every non-hero scene gets a Ken Burns move built natively by ffmpeg's zoompan,
which costs nothing and renders far faster than doing it frame-by-frame in
Python.
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path

import httpx

from . import cache
from .config import Config
from .cost import BudgetExceeded, CostLedger
from .ffmpeg_util import run
from .models import Scene, SceneAudio, SceneVisual, Story

log = logging.getLogger(__name__)

FAL_QUEUE = "https://queue.fal.run"

# Ken Burns variants, cycled by scene index so consecutive stills never move
# the same way. Expressions are in terms of `on` (output frame) and TOTAL.
KEN_BURNS = ["zoom_in", "pan_right", "zoom_out", "pan_left"]

# Scene 1 gets a punch-in instead of a Ken Burns move: the viewer's first frame
# lands hard, with a camera shake and a white flash.
PUNCH_IN = "punch_in"

# Strength of the Ken Burns move. 0.25 is clearly visible on a phone screen;
# the old 0.14 read as nearly static.
KEN_BURNS_AMOUNT = 0.25
PUNCH_IN_ZOOM = 0.35  # starts at 1.35x and settles to 1.0x
PUNCH_IN_SECONDS = 0.5
PUNCH_IN_SHAKE_PX = 18  # in the 1.5x-upscaled source frame
FLASH_FRAMES = 4


def _fal_submit(cfg: Config, image: Path, prompt: str) -> str:
    key = cfg.fal_key
    if not key:
        raise RuntimeError("FAL_KEY is not set; add it to .env or set motion.enabled: false")

    # fal accepts data URIs, so the still never needs to be hosted anywhere.
    b64 = base64.b64encode(image.read_bytes()).decode("ascii")
    payload = {
        "prompt": prompt,
        "image_url": f"data:image/png;base64,{b64}",
        "resolution": cfg.motion.resolution,
        "duration": cfg.motion.clip_seconds,
        "enable_prompt_expansion": False,
    }
    resp = httpx.post(
        f"{FAL_QUEUE}/{cfg.motion.model}",
        headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()["request_id"]


def _fal_await(cfg: Config, request_id: str) -> str:
    key = cfg.fal_key
    headers = {"Authorization": f"Key {key}"}
    base = f"{FAL_QUEUE}/{cfg.motion.model}/requests/{request_id}"
    deadline = time.monotonic() + cfg.motion.timeout_s

    while time.monotonic() < deadline:
        status = httpx.get(f"{base}/status", headers=headers, timeout=60.0)
        status.raise_for_status()
        state = status.json().get("status")
        if state == "COMPLETED":
            result = httpx.get(base, headers=headers, timeout=60.0)
            result.raise_for_status()
            return result.json()["video"]["url"]
        if state in {"FAILED", "CANCELLED"}:
            raise RuntimeError(f"fal job {request_id} ended as {state}: {status.text}")
        time.sleep(cfg.motion.poll_interval_s)

    raise TimeoutError(f"fal job {request_id} did not finish within {cfg.motion.timeout_s}s")


def _animate(
    cfg: Config, scene: Scene, image: Path, dest: Path, ledger: CostLedger
) -> Path:
    key = cache.key_for(
        model=cfg.motion.model,
        resolution=cfg.motion.resolution,
        seconds=cfg.motion.clip_seconds,
        prompt=scene.image_prompt,
        image=image.stat().st_size,
    )
    if cache.is_fresh(dest, key):
        log.info("scene %d: clip cached, no spend", scene.scene_number)
        return dest

    usd = cfg.motion.usd_per_second * cfg.motion.clip_seconds
    label = f"scene {scene.scene_number} i2v"
    ledger.check_affordable(usd, label)

    motion_prompt = (
        f"{scene.image_prompt}. Subtle natural motion, gentle camera push, "
        "characters breathing and blinking, cloth and hair moving slightly. "
        "Keep the art style and character appearance identical to the input image."
    )
    log.info("scene %d: animating (%.2f USD)", scene.scene_number, usd)
    request_id = _fal_submit(cfg, image, motion_prompt)
    url = _fal_await(cfg, request_id)

    with httpx.stream("GET", url, timeout=300.0, follow_redirects=True) as resp:
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)

    ledger.record(usd, label)
    cache.mark_fresh(dest, key)
    return dest


def ken_burns_filter(cfg: Config, duration: float, variant: str) -> str:
    """zoompan expression for a still.

    Two details here are load-bearing for speed and correctness:

    `d=1` makes zoompan emit one output frame per input frame, with the
    incoming `fps` filter supplying the frame count. The intuitive `d=frames`
    form makes zoompan emit a whole clip per *input* frame, and since `-loop 1`
    feeds it frames indefinitely it ends up doing ~50x redundant work — 154s
    versus 3s for a 4-second segment.

    zoompan's own `fps` defaults to 25, so it is set explicitly; otherwise the
    segment silently comes out at the wrong rate and desyncs from the audio.

    Zoom is driven by the output frame number rather than by accumulating
    `zoom+step`, which drifts and visibly jitters on longer holds.
    """
    if variant == PUNCH_IN:
        return punch_in_filter(cfg, duration)

    fps = cfg.video.fps
    frames = max(int(round(duration * fps)), 2)
    amount = KEN_BURNS_AMOUNT
    # Ease-out: the move starts quickly and settles, rather than drifting at a
    # constant speed. `e` runs from 0 to 1 over the segment.
    p = f"(on/{frames})"
    e = f"(1-pow(1-{p},2))"

    if variant == "zoom_in":
        z = f"1+{amount}*{e}"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif variant == "zoom_out":
        z = f"{1 + amount}-{amount}*{e}"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif variant == "pan_right":
        z = f"{1 + amount}"
        x, y = f"(iw-iw/zoom)*{e}", "ih/2-(ih/zoom/2)"
    else:  # pan_left
        z = f"{1 + amount}"
        x, y = f"(iw-iw/zoom)*(1-{e})", "ih/2-(ih/zoom/2)"

    return _zoompan_chain(cfg, z, x, y, fps)


def punch_in_filter(cfg: Config, duration: float) -> str:
    """Scene 1: a fast punch-in from 1.35x to 1.0x, with a decaying shake.

    The zoom and shake both run over PUNCH_IN_SECONDS and then hold still, so the
    narration lands on a settled frame. A white flash is drawn over the first
    FLASH_FRAMES frames.
    """
    fps = cfg.video.fps
    half = max(PUNCH_IN_SECONDS * fps, 1.0)
    # Progress through the punch-in, clamped to 1.0 once it has settled.
    p = f"min(on/{half:.3f},1)"
    z = f"1+{PUNCH_IN_ZOOM}*pow(1-{p},2)"
    shake = f"{PUNCH_IN_SHAKE_PX}*(1-{p})*sin(on*2.3)"
    x = f"iw/2-(iw/zoom/2)+{shake}"
    y = f"ih/2-(ih/zoom/2)+{shake}*0.6"
    return _zoompan_chain(cfg, z, x, y, fps, flash=True)


def _zoompan_chain(cfg: Config, z: str, x: str, y: str, fps: int, *, flash: bool = False) -> str:
    w, h = cfg.video.width, cfg.video.height
    # Upscaling first means zoompan crops from a larger source and scales
    # *down* into frame, which stays sharp. Straight from the 832x1472 still it
    # would be scaling up and would look soft. Costs about 0.1s per segment.
    chain = (
        f"fps={fps},"
        f"scale={int(w * 1.5)}:{int(h * 1.5)}:flags=lanczos,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps}"
    )
    if flash:
        # `n` counts frames inside this segment, so the flash is always the
        # first few frames of the scene, whatever comes before it in the video.
        chain += f",drawbox=x=0:y=0:w=iw:h=ih:color=white@1:t=fill:enable='lt(n,{FLASH_FRAMES})'"
    return f"{chain},setsar=1"


def build_still_segment(
    cfg: Config, image: Path, duration: float, dest: Path, variant: str
) -> None:
    run(
        [
            "-loop", "1",
            "-t", f"{duration:.3f}",
            "-i", str(image),
            "-vf", ken_burns_filter(cfg, duration, variant),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", str(cfg.video.fps),
            "-preset", "medium",
            "-crf", "18",
            str(dest),
        ],
        label=f"ken burns ({dest.name})",
    )


def build_clip_segment(cfg: Config, clip: Path, duration: float, dest: Path) -> None:
    """Fit a generated clip to its scene slot.

    Clips come back at a fixed length that rarely matches the narration, so
    stretch modestly to cover a shortfall and freeze the final frame for
    anything beyond that. Stretching too far looks like slow motion.
    """
    from .ffmpeg_util import probe_duration

    w, h, fps = cfg.video.width, cfg.video.height, cfg.video.fps
    clip_len = probe_duration(clip)
    max_stretch = 1.30

    common = (
        f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={w}:{h},unsharp=5:5:0.5,setsar=1,fps={fps}"
    )

    if clip_len >= duration:
        vf = f"{common},trim=0:{duration:.3f},setpts=PTS-STARTPTS"
    else:
        ratio = min(duration / clip_len, max_stretch)
        stretched = clip_len * ratio
        vf = f"{common},setpts={ratio:.4f}*PTS"
        if stretched < duration - 0.04:
            # tpad clones the last frame to cover the remainder.
            vf += f",tpad=stop_mode=clone:stop_duration={duration - stretched:.3f}"
        vf += f",trim=0:{duration:.3f},setpts=PTS-STARTPTS"

    run(
        [
            "-i", str(clip),
            "-an",
            "-vf", vf,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-preset", "medium",
            "-crf", "18",
            str(dest),
        ],
        label=f"clip fit ({dest.name})",
    )


def generate_motion(
    cfg: Config,
    story: Story,
    visuals: list[SceneVisual],
    out_dir: Path,
    ledger: CostLedger,
) -> list[SceneVisual]:
    """Attach animated clips to hero scenes. Failures degrade to Ken Burns."""
    if not cfg.motion.enabled or cfg.motion.provider == "none":
        log.info("motion disabled; every scene will use Ken Burns")
        return visuals

    out_dir.mkdir(parents=True, exist_ok=True)
    by_number = {v.scene_number: v for v in visuals}

    for scene in story.scenes:
        if not scene.is_hero:
            continue
        visual = by_number[scene.scene_number]
        dest = out_dir / f"scene_{scene.scene_number:02d}.mp4"
        try:
            path = _animate(cfg, scene, Path(visual.image_path), dest, ledger)
            by_number[scene.scene_number] = visual.model_copy(
                update={"clip_path": str(path)}
            )
        except BudgetExceeded as exc:
            log.warning("%s -- remaining scenes stay on Ken Burns", exc)
            break
        except Exception as exc:  # noqa: BLE001 - a failed clip is not fatal
            log.warning(
                "scene %d: animation failed (%s), falling back to Ken Burns",
                scene.scene_number,
                exc,
            )

    return [by_number[v.scene_number] for v in visuals]
