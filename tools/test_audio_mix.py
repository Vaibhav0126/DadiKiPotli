"""Quick narration + music mix with zero paid / free-tier AI credits.

Uses a hardcoded Hindi script (no Gemini), edge-tts (free), and the local
music bed. Useful for tuning music.volume_db / duck_db without burning
Cloudflare neurons or fal spend.

    .venv/bin/python -m tools.test_audio_mix
    .venv/bin/python -m tools.test_audio_mix --volume-db -18
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import audio_engine, composer  # noqa: E402
from src.config import ROOT, load_config  # noqa: E402
from src.models import Character, Scene, Story, Timeline  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("test_audio")

# Short sample — enough to hear ducking under voice and music in the gaps.
LINES = [
    ("hook", "अकबर ने बीरबल से पूछा, सच क्या है?", "Akbar ne Birbal se poocha, sach kya hai?"),
    ("setup", "बीरबल मुस्कुराए और बोले, सुनो बादशाह सलामत।", "Birbal muskuraye aur bole, suno badshah salamat."),
    (
        "twist",
        "सच वही है जो दिल से कहा जाए, झूठ नहीं।",
        "Sach wahi hai jo dil se kaha jaye, jhooth nahi.",
    ),
]


def _story() -> Story:
    scenes = [
        Scene(
            scene_number=i,
            beat=beat,  # type: ignore[arg-type]
            hindi_narration=hi,
            hinglish_caption=en,
            image_prompt="n/a",
            characters_present=["Akbar", "Birbal"],
        )
        for i, (beat, hi, en) in enumerate(LINES, start=1)
    ]
    return Story(
        title_hindi="सच की परीक्षा",
        title_hinglish="Sach ki Pariksha",
        characters=[
            Character(name="Akbar", visual_description="n/a"),
            Character(name="Birbal", visual_description="n/a"),
        ],
        scenes=scenes,
        moral_hinglish="Sach hamesha jeetta hai",
        slug="_audio_mix_test",
        idea="audio mix volume test",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Narration + music mix (no AI credits)")
    parser.add_argument(
        "--volume-db",
        type=float,
        default=None,
        help="override music.volume_db for this run only",
    )
    parser.add_argument(
        "--duck-db",
        type=float,
        default=None,
        help="override music.duck_db (informational; ducking uses sidechain)",
    )
    args = parser.parse_args()

    cfg = load_config()
    if args.volume_db is not None:
        cfg.music.volume_db = args.volume_db
    if args.duck_db is not None:
        cfg.music.duck_db = args.duck_db

    work = ROOT / "work" / "_audio_mix_test"
    work.mkdir(parents=True, exist_ok=True)
    out = ROOT / "output" / "audio_mix_test.m4a"
    out.parent.mkdir(parents=True, exist_ok=True)

    story = _story()
    log.info("synthesizing narration via edge-tts (free)…")
    scenes = audio_engine.generate_audio(cfg, story, work / "audio")
    duration = audio_engine.total_duration(scenes)
    log.info("narration total: %.1fs", duration)

    timeline = Timeline(
        story=story,
        audio=scenes,
        visuals=[],
        total_duration=duration,
    )

    voice = work / "voice.wav"
    composer.build_voice_track(cfg, timeline, voice)

    music = composer.pick_music(cfg)
    if music is None:
        log.error("no music in %s — drop an mp3 there first", cfg.music_dir())
        return 1
    log.info(
        "mixing with %s at volume_db=%s",
        music.name,
        cfg.music.volume_db,
    )
    composer.mix_audio(cfg, voice, music, duration, out)
    log.info("wrote %s (%.1fs)", out, duration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
