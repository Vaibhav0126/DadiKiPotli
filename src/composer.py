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
from .motion_engine import KEN_BURNS, build_clip_segment, build_still_segment

log = logging.getLogger(__name__)


def build_segments(cfg: Config, timeline: Timeline, seg_dir: Path) -> list[Path]:
    seg_dir.mkdir(parents=True, exist_ok=True)
    transition = cfg.video.transition_s if cfg.video.transition == "fade" else 0.0
    paths: list[Path] = []
    last = len(timeline.audio) - 1

    for i, audio in enumerate(timeline.audio):
        visual = timeline.visual_for(audio.scene_number)
        # Every segment but the last carries an extra tail that the crossfade
        # consumes, so the summed runtime still matches the audio timeline.
        duration = audio.duration + (transition if i < last else 0.0)
        dest = seg_dir / f"seg_{audio.scene_number:02d}.mp4"

        if visual.clip_path:
            build_clip_segment(cfg, Path(visual.clip_path), duration, dest)
        else:
            build_still_segment(
                cfg,
                Path(visual.image_path),
                duration,
                dest,
                KEN_BURNS[i % len(KEN_BURNS)],
            )
        paths.append(dest)
        log.info("segment %d built (%.2fs)", audio.scene_number, duration)

    return paths


def concat_segments(cfg: Config, segments: list[Path], dest: Path) -> None:
    if len(segments) == 1:
        run(["-i", str(segments[0]), "-c", "copy", str(dest)], label="single segment")
        return

    if cfg.video.transition != "fade":
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
            f"{current}[{i}:v]xfade=transition=fade:duration={t}:offset={offset:.3f}{label}"
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


def mix_audio(cfg: Config, voice: Path, music: Path | None, duration: float, dest: Path) -> None:
    if music is None:
        run(
            [
                "-i", str(voice),
                "-filter_complex", "[0:a]alimiter=limit=0.95[aout]",
                "-map", "[aout]",
                "-t", f"{duration:.3f}",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                str(dest),
            ],
            label="audio (voice only)",
        )
        return

    # The music bed is ducked by the narration rather than sitting at a fixed
    # level, so it can be louder in the gaps without ever fighting the voice.
    graph = (
        "[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[v1][v2];"
        f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
        f"volume={cfg.music.volume_db}dB[m];"
        "[m][v1]sidechaincompress=threshold=0.045:ratio=8:attack=15:release=350[duck];"
        "[duck][v2]amix=inputs=2:normalize=0[mixed];"
        "[mixed]alimiter=limit=0.95[aout]"
    )
    run(
        [
            "-i", str(voice),
            "-stream_loop", "-1", "-i", str(music),
            "-filter_complex", graph,
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
    mix_audio(cfg, voice, pick_music(cfg), duration, mixed)

    render_final(cfg, silent, mixed, captions, duration, dest)
    log.info("rendered %s (%.1fs)", dest.name, duration)
    return dest
