"""End-to-end check of the render path using synthetic assets.

Exercises Ken Burns, crossfade concatenation, the voice track layout, the audio
mix and the caption burn without touching any paid API. Run this after changing
anything in composer.py, motion_engine.py or caption_engine.py.

    .venv/bin/python -m tools.smoke_test
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import caption_engine, composer  # noqa: E402
from src.config import ROOT, load_config  # noqa: E402
from src.ffmpeg_util import ffmpeg_binary, probe_duration, run  # noqa: E402
from src.models import (  # noqa: E402
    Character,
    Scene,
    SceneAudio,
    SceneVisual,
    Story,
    Timeline,
    WordTiming,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("smoke")

PALETTE = [(198, 63, 45), (37, 92, 138), (214, 154, 42)]
LINES = [
    "Akbar ne Birbal se ek sawal poocha",
    "Birbal muskuraya aur bola suno",
    # Deliberately long words: checks that a wide group wraps instead of
    # running off the edges of the frame.
    "Sachchai hamesha imaandari khoobsurati jeet jaati hai",
]


def make_image(path: Path, colour: tuple[int, int, int], label: str, size=(832, 1472)) -> None:
    img = Image.new("RGB", size, colour)
    draw = ImageDraw.Draw(img)
    # A few shapes give zoompan real detail to move across, so pan smoothness
    # is actually visible in the output.
    for i in range(0, size[0], 80):
        draw.line([(i, 0), (i, size[1])], fill=(255, 255, 255), width=2)
    for i in range(0, size[1], 80):
        draw.line([(0, i), (size[0], i)], fill=(255, 255, 255), width=2)
    draw.ellipse([size[0] // 4, size[1] // 3, size[0] * 3 // 4, size[1] * 2 // 3], fill="white")
    draw.text((size[0] // 2 - 40, size[1] // 2), label, fill=colour)
    img.save(path)


def make_audio(path: Path, seconds: float, freq: int) -> None:
    run(
        [
            "-f", "lavfi",
            "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=48000",
            "-c:a", "libmp3lame", "-b:a", "128k",
            str(path),
        ],
        label="synthetic narration",
    )


def main() -> int:
    cfg = load_config()
    log.info("ffmpeg: %s", ffmpeg_binary())

    work = ROOT / "work" / "_smoke"
    work.mkdir(parents=True, exist_ok=True)
    (work / "images").mkdir(exist_ok=True)
    (work / "audio").mkdir(exist_ok=True)

    scenes: list[Scene] = []
    audio: list[SceneAudio] = []
    visuals: list[SceneVisual] = []
    cursor = 0.0

    for i, (colour, line) in enumerate(zip(PALETTE, LINES), start=1):
        img = work / "images" / f"scene_{i:02d}.png"
        make_image(img, colour, str(i))

        speech = 3.0 + i * 0.4
        mp3 = work / "audio" / f"scene_{i:02d}.mp3"
        make_audio(mp3, speech, 220 * i)
        speech = probe_duration(mp3)

        tokens = line.split()
        step = speech / len(tokens)
        words = [
            WordTiming(word=t, start=cursor + j * step, end=cursor + (j + 1) * step)
            for j, t in enumerate(tokens)
        ]

        slot = speech + cfg.audio.scene_gap_s
        scenes.append(
            Scene(
                scene_number=i,
                beat="setup",
                hindi_narration=line,
                hinglish_caption=line,
                image_prompt="test",
            )
        )
        audio.append(
            SceneAudio(
                scene_number=i,
                audio_path=str(mp3),
                speech_duration=speech,
                duration=slot,
                start=cursor,
                words=words,
            )
        )
        visuals.append(SceneVisual(scene_number=i, image_path=str(img)))
        cursor += slot

    story = Story(
        slug="_smoke",
        idea="smoke test",
        title_hindi="धुआँ",
        title_hinglish="Smoke Test",
        characters=[Character(name="Birbal", visual_description="test")],
        scenes=scenes,
        moral_hinglish="Test everything.",
    )
    timeline = Timeline(
        story=story, audio=audio, visuals=visuals, total_duration=round(cursor, 3)
    )

    captions = caption_engine.build_ass(cfg, audio, work / "captions.ass")
    dest = ROOT / "output" / "_smoke.mp4"
    composer.compose(cfg, timeline, work, captions, dest)

    actual = probe_duration(dest)
    expected = timeline.total_duration
    drift = abs(actual - expected)
    log.info("expected %.3fs, got %.3fs (drift %.3fs)", expected, actual, drift)

    if drift > 0.15:
        log.error("A/V timeline drift is too large")
        return 1
    log.info("OK -> %s", dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
