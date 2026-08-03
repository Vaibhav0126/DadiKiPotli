"""Orchestrator. Runs the four modules in order, resuming from whatever exists.

    python -m src.main "Akbar asks Birbal for a flower that never wilts"
    python -m src.main --batch ideas.txt
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from pydantic import BaseModel

from . import audio_engine, caption_engine, composer, motion_engine, script_engine, visual_engine
from .config import OUTPUT, Config, load_config, work_dir
from .cost import CostLedger
from .doctor import run_checks
from .ffmpeg_util import FFmpegError, ensure_available
from .models import SceneAudio, Story, Timeline, read_json, slugify, write_json

log = logging.getLogger("pipeline")


class AudioBundle(BaseModel):
    scenes: list[SceneAudio]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _stage(name: str) -> None:
    log.info("%s", f"--- {name} ".ljust(60, "-"))


def _script_stage(cfg: Config, idea: str, force: bool) -> tuple[Story, Path]:
    _stage("Module 1: script")
    wd = work_dir(slugify(idea))
    existing = wd / "story.json"
    if existing.exists() and not force:
        story = read_json(existing, Story)
        log.info("reusing script: %s", story.title_hinglish)
        return story, wd

    story = script_engine.generate_story(cfg, idea)
    write_json(wd / "story.json", story)
    log.info("title: %s", story.title_hinglish)
    log.info("moral: %s", story.moral_hinglish)
    return story, wd


def _audio_stage(cfg: Config, story: Story, wd: Path, force: bool) -> tuple[Story, list[SceneAudio]]:
    _stage("Module 2: narration + timing")
    bundle_path = wd / "audio.json"
    if bundle_path.exists() and not force:
        scenes = read_json(bundle_path, AudioBundle).scenes
        log.info("reusing narration (%.1fs)", audio_engine.total_duration(scenes))
        return story, scenes

    scenes = audio_engine.generate_audio(cfg, story, wd / "audio")
    total = audio_engine.total_duration(scenes)
    log.info("narration total: %.1fs", total)

    # Word count only approximates duration, so the real check happens here,
    # against measured audio, and feeds a rewrite if it misses the window.
    attempts = 0
    while (
        not (cfg.story.min_duration_s <= total <= cfg.story.max_duration_s)
        and attempts < cfg.story.max_script_retries
    ):
        attempts += 1
        log.warning(
            "%.1fs is outside the %.0f-%.0fs window; rewriting (attempt %d)",
            total, cfg.story.min_duration_s, cfg.story.max_duration_s, attempts,
        )
        story = script_engine.revise_for_duration(cfg, story, total)
        write_json(wd / "story.json", story)
        scenes = audio_engine.generate_audio(cfg, story, wd / "audio")
        total = audio_engine.total_duration(scenes)
        log.info("narration total: %.1fs", total)

    if not (cfg.story.min_duration_s <= total <= cfg.story.max_duration_s):
        log.warning("proceeding at %.1fs after %d rewrites", total, attempts)

    write_json(bundle_path, AudioBundle(scenes=scenes))
    return story, scenes


def _visual_stage(cfg: Config, story: Story, wd: Path, mode: str):
    _stage("Module 3a: scene images")
    visuals = visual_engine.generate_images(cfg, story, wd / "images", consistency_mode=mode)
    sheet = visual_engine.contact_sheet(visuals, wd / "contact_sheet.jpg")
    log.info("QC contact sheet: %s", sheet)
    return visuals


def _motion_stage(cfg: Config, story: Story, visuals, wd: Path):
    _stage("Module 3b: motion")
    ledger = CostLedger(wd / "cost.json", cfg.motion.max_cost_usd)
    heroes = [s.scene_number for s in story.scenes if s.is_hero]
    if cfg.motion.enabled and heroes:
        log.info("animating scenes %s (cap $%.2f)", heroes, cfg.motion.max_cost_usd)
    visuals = motion_engine.generate_motion(cfg, story, visuals, wd / "clips", ledger)
    log.info("spend this run: $%.2f", ledger.spent)
    return visuals, ledger


def run_pipeline(cfg: Config, idea: str, *, force: bool, review: bool, mode: str) -> Path:
    story, wd = _script_stage(cfg, idea, force)
    story, scenes = _audio_stage(cfg, story, wd, force)
    visuals = _visual_stage(cfg, story, wd, mode)

    if review:
        answer = input("\nApprove these stills and continue to paid animation? [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            log.info("stopped before spending; rerun to continue")
            sys.exit(0)

    visuals, ledger = _motion_stage(cfg, story, visuals, wd)

    _stage("Module 4a: captions")
    captions = caption_engine.build_ass(cfg, scenes, wd / "captions.ass")

    _stage("Module 4b: assembly")
    timeline = Timeline(
        story=story,
        audio=scenes,
        visuals=visuals,
        total_duration=audio_engine.total_duration(scenes),
    )
    write_json(wd / "timeline.json", timeline)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    dest = OUTPUT / f"{story.slug}.mp4"
    composer.compose(cfg, timeline, wd, captions, dest)

    animated = sum(1 for v in visuals if v.animated)
    _stage("Done")
    log.info("title     : %s", story.title_hinglish)
    log.info("duration  : %.1fs", timeline.total_duration)
    log.info("scenes    : %d (%d animated, %d Ken Burns)",
             len(visuals), animated, len(visuals) - animated)
    log.info("spend     : $%.2f", ledger.spent)
    log.info("output    : %s", dest)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="Hindi story Shorts pipeline")
    parser.add_argument("idea", nargs="?", help="one-line story idea")
    parser.add_argument("--batch", type=Path, help="file of story ideas, one per line")
    parser.add_argument("--check", action="store_true", help="run preflight checks and exit")
    parser.add_argument("--force", action="store_true", help="ignore cached script and audio")
    parser.add_argument("--review", action="store_true", help="pause for QC before paid animation")
    parser.add_argument("--no-motion", action="store_true", help="Ken Burns only, spend nothing")
    parser.add_argument(
        "--consistency", choices=["seed", "kontext"], default="seed",
        help="character consistency strategy for images",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    cfg = load_config()
    if args.no_motion:
        cfg.motion.enabled = False

    if args.check:
        return run_checks(cfg)

    if not args.idea and not args.batch:
        parser.error("provide a story idea or --batch")

    try:
        ensure_available()
    except FFmpegError as exc:
        log.error("%s", exc)
        return 1

    ideas: list[str] = []
    if args.batch:
        ideas = [
            line.strip()
            for line in args.batch.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if args.idea:
        ideas.insert(0, args.idea)

    failures = 0
    for i, idea in enumerate(ideas, start=1):
        log.info("")
        log.info("=" * 60)
        log.info("[%d/%d] %s", i, len(ideas), idea)
        log.info("=" * 60)
        try:
            run_pipeline(cfg, idea, force=args.force, review=args.review, mode=args.consistency)
        except KeyboardInterrupt:
            log.warning("interrupted")
            return 130
        except Exception as exc:  # noqa: BLE001 - one bad idea should not stop a batch
            failures += 1
            log.exception("failed: %s", exc)

    if failures:
        log.error("%d of %d stories failed", failures, len(ideas))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
