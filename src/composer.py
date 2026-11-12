"""Module 4b: assemble stills, clips, narration, music and captions into the MP4.

Each scene occupies a slot of exactly `SceneAudio.duration`, so video and audio
are laid out on the same grid and cannot drift. Crossfades are compensated for
by building segments slightly long, which keeps the finished runtime equal to
the sum of the scene slots.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

from .config import Config
from .ffmpeg_util import escape_filter_path, probe_duration, run
from .models import Timeline
from .motion_engine import KEN_BURNS, PUNCH_IN, build_clip_segment, build_still_segment
from .sfx import cues as sfx_cues
from .sfx import ensure_sfx

log = logging.getLogger(__name__)


def build_segments(cfg: Config, timeline: Timeline, seg_dir: Path) -> list[Path]:
    seg_dir.mkdir(parents=True, exist_ok=True)
    transition = 0.0 if cfg.video.transition == "none" else cfg.video.transition_s
    paths: list[Path] = []
    last = len(timeline.audio) - 1

    for i, audio in enumerate(timeline.audio):
        visual = timeline.visual_for(audio.scene_number)
        # Every segment but the last carries an extra tail that the transition
        # consumes, so the summed runtime still matches the audio timeline.
        duration = audio.duration + (transition if i < last else 0.0)
        dest = seg_dir / f"seg_{audio.scene_number:02d}.mp4"

        if visual.clip_path:
            build_clip_segment(cfg, Path(visual.clip_path), duration, dest)
        else:
            # Scene 1 punches in on the first frame; the rest alternate Ken Burns
            # directions so no two neighbouring cuts move the same way.
            variant = PUNCH_IN if i == 0 else KEN_BURNS[(i - 1) % len(KEN_BURNS)]
            build_still_segment(cfg, Path(visual.image_path), duration, dest, variant)
        paths.append(dest)
        log.info("segment %d built (%.2fs)", audio.scene_number, duration)

    return paths


def concat_segments(cfg: Config, segments: list[Path], dest: Path) -> None:
    if len(segments) == 1:
        run(["-i", str(segments[0]), "-c", "copy", str(dest)], label="single segment")
        return

    if cfg.video.transition == "none":
        listing = dest.parent / "segments.txt"
        listing.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in segments) + "\n", encoding="utf-8"
        )
        run(
            ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(dest)],
            label="concat",
        )
        return

    t = cfg.video.transition_s
    inputs: list[str] = []
    for seg in segments:
        inputs += ["-i", str(seg)]

    durations = [probe_duration(p) for p in segments]
    steps: list[str] = []
    current = "[0:v]"
    accumulated = durations[0]

    for i in range(1, len(segments)):
        label = f"[x{i}]" if i < len(segments) - 1 else "[vout]"
        offset = max(accumulated - t, 0.0)
        steps.append(
            f"{current}[{i}:v]xfade=transition={cfg.video.transition}"
            f":duration={t}:offset={offset:.3f}{label}"
        )
        accumulated = accumulated + durations[i] - t
        current = label

    run(
        [
            *inputs,
            "-filter_complex", ";".join(steps),
            "-map", "[vout]",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", str(cfg.video.fps),
            "-preset", "medium",
            "-crf", "18",
            str(dest),
        ],
        label="crossfade concat",
    )


def build_voice_track(cfg: Config, timeline: Timeline, dest: Path) -> None:
    """Concatenate scene narration, padding each to its full slot length."""
    inputs: list[str] = []
    steps: list[str] = []
    labels: list[str] = []

    for i, scene in enumerate(timeline.audio):
        inputs += ["-i", scene.audio_path]
        d = scene.duration
        steps.append(
            f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"apad=whole_dur={d:.3f},atrim=0:{d:.3f},asetpts=N/SR/TB[a{i}]"
        )
        labels.append(f"[a{i}]")

    steps.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[vout]")
    run(
        [
            *inputs,
            "-filter_complex", ";".join(steps),
            "-map", "[vout]",
            "-c:a", "pcm_s16le",
            str(dest),
        ],
        label="voice track",
    )


def pick_music(cfg: Config) -> Path | None:
    if not cfg.music.enabled:
        return None
    directory = cfg.music_dir()
    if not directory.exists():
        return None
    tracks = sorted(
        p for p in directory.iterdir() if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg"}
    )
    if not tracks:
        log.info("no music found in %s; continuing without a bed", directory)
        return None
    return random.choice(tracks)


def mix_audio(
    cfg: Config,
    voice: Path,
    music: Path | None,
    duration: float,
    dest: Path,
    cues: list[tuple[float, str, float]] | None = None,
    sfx_files: dict[str, Path] | None = None,
) -> None:
    """Narration, an optional ducked music bed, and sound-effect cues in one track.

    Each effect is delayed to its cue time, so it stays on the scene grid. The
    result is cut to `duration`, the sum of the scene slots.
    """
    cues = cues or []
    sfx_files = sfx_files or {}

    inputs = ["-i", str(voice)]
    next_input = 1
    music_input = None
    if music is not None:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        music_input = next_input
        next_input += 1

    cue_inputs: list[int] = []
    for _, name, _ in cues:
        inputs += ["-i", str(sfx_files[name])]
        cue_inputs.append(next_input)
        next_input += 1

    graph = ["[0:a]aformat=sample_rates=48000:channel_layouts=stereo[voice]"]
    if music_input is None:
        graph.append("[voice]anull[bed]")
    else:
        # The music bed loops for the full narration length and is ducked by the
        # voice rather than sitting at a fixed level, so it can be louder in the
        # gaps without fighting the narrator.
        graph += [
            "[voice]asplit=2[vsc][vmix]",
            f"[{music_input}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"volume={cfg.music.volume_db}dB[m]",
            "[m][vsc]sidechaincompress=threshold=0.045:ratio=8:attack=15:release=350[duck]",
            "[duck][vmix]amix=inputs=2:normalize=0[bed]",
        ]

    labels = ["[bed]"]
    for (start, _, gain), index in zip(cues, cue_inputs):
        delay_ms = max(int(round(start * 1000)), 0)
        label = f"[cue{index}]"
        graph.append(
            f"[{index}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"volume={gain},adelay={delay_ms}:all=1{label}"
        )
        labels.append(label)

    if len(labels) > 1:
        graph.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=first[mixed]"
        )
    else:
        graph.append("[bed]anull[mixed]")
    graph.append("[mixed]alimiter=limit=0.95[aout]")

    run(
        [
            *inputs,
            "-filter_complex", ";".join(graph),
            "-map", "[aout]",
            "-t", f"{duration:.3f}",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            str(dest),
        ],
        label="audio mix",
    )


def render_final(
    cfg: Config, video: Path, audio: Path, captions: Path, duration: float, dest: Path
) -> None:
    vf = (
        f"ass='{escape_filter_path(captions)}'"
        f":fontsdir='{escape_filter_path(cfg.font_path().parent)}'"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "-i", str(video),
            "-i", str(audio),
            "-vf", vf,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-t", f"{duration:.3f}",
            "-c:v", "libx264",
            "-profile:v", "high",
            "-level", "4.0",
            "-pix_fmt", "yuv420p",
            "-r", str(cfg.video.fps),
            "-preset", "medium",
            "-crf", "20",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(dest),
        ],
        label="final render",
    )


def compose(cfg: Config, timeline: Timeline, work: Path, captions: Path, dest: Path) -> Path:
    duration = timeline.total_duration

    segments = build_segments(cfg, timeline, work / "segments")
    silent = work / "video_silent.mp4"
    concat_segments(cfg, segments, silent)

    voice = work / "voice.wav"
    build_voice_track(cfg, timeline, voice)

    mixed = work / "audio_mixed.m4a"
    mix_audio(
        cfg, voice, pick_music(cfg), duration, mixed,
        cues=sfx_cues(timeline), sfx_files=ensure_sfx(),
    )

    render_final(cfg, silent, mixed, captions, duration, dest)
    log.info("rendered %s (%.1fs)", dest.name, duration)
    return dest
