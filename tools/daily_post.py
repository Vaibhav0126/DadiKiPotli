"""Pick one story idea and publish a short (YouTube + Instagram).

Used by the daily GitHub Action:

    python -m tools.daily_post
    python -m tools.daily_post --idea "..." --publish youtube
    python -m tools.daily_post --no-motion

Reads the first unused line from ideas.txt. On success, moves that line to
ideas_used.txt so the next run takes the next idea. If the queue is empty,
asks Gemini for a fresh Akbar-Birbal / Tenali / Vikram-Betaal idea.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai  # noqa: E402

from src.config import ROOT, Config, load_config  # noqa: E402
from src.ffmpeg_util import FFmpegError, ensure_available  # noqa: E402
from src.main import run_pipeline  # noqa: E402
from src.publish_engine import parse_platforms  # noqa: E402

log = logging.getLogger("daily_post")

IDEAS = ROOT / "ideas.txt"
IDEAS_USED = ROOT / "ideas_used.txt"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _read_queue(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _rewrite_queue(path: Path, remaining: list[str], *, header: str) -> None:
    body = "\n".join(remaining)
    text = f"{header.rstrip()}\n\n{body}\n" if body else f"{header.rstrip()}\n"
    path.write_text(text, encoding="utf-8")


def _consume_idea(idea: str) -> None:
    """Remove idea from ideas.txt and append it to ideas_used.txt."""
    queue = _read_queue(IDEAS)
    remaining = [line for line in queue if line != idea]
    if len(remaining) == len(queue) and idea not in queue:
        # Generated ideas are not in the queue; still record them as used.
        pass
    else:
        _rewrite_queue(
            IDEAS,
            remaining,
            header=(
                "# One story idea per line. Blank lines and # comments are ignored.\n"
                "# Daily Action consumes the first unused line and moves it to ideas_used.txt."
            ),
        )

    used = _read_queue(IDEAS_USED)
    if idea not in used:
        used.append(idea)
    _rewrite_queue(
        IDEAS_USED,
        used,
        header="# Ideas already posted by tools.daily_post (newest at the bottom).",
    )


def _invent_idea(cfg: Config) -> str:
    log.info("ideas.txt is empty; inventing a fresh idea with Gemini")
    client = genai.Client(api_key=cfg.gemini_api_key)
    prompt = (
        "Invent ONE original one-line story idea for a Hindi moral short "
        "in the style of Akbar-Birbal, Tenali Raman, or Vikram-Betaal. "
        "Return ONLY the idea sentence in English, no quotes, no numbering, "
        "no explanation. Keep it under 20 words. Example style: "
        "'Akbar asks Birbal to bring him a flower that never wilts'."
    )
    last_error: Exception | None = None
    resp = None
    for model in cfg.story.models:
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            text = str(exc)
            if "NOT_FOUND" not in text and "404" not in text and "not available" not in text:
                raise
            log.debug("model %s unavailable, trying next", model)
    if resp is None:
        raise RuntimeError(f"Could not invent an idea via Gemini: {last_error}")
    idea = (getattr(resp, "text", None) or "").strip().strip('"').strip("'")
    if not idea:
        raise RuntimeError("Gemini returned an empty story idea")
    idea = " ".join(idea.split())
    log.info("invented idea: %s", idea)
    return idea


def next_idea(cfg: Config, override: str | None) -> str:
    if override and override.strip():
        return override.strip()
    queue = _read_queue(IDEAS)
    if queue:
        return queue[0]
    return _invent_idea(cfg)


def _resolve_platforms(raw: str | None) -> list[str]:
    """Default to both platforms, dropping any that lack credentials."""
    wanted = parse_platforms(raw or "youtube,instagram")
    cfg = load_config()
    available: list[str] = []
    for name in wanted:
        if name == "youtube":
            token = cfg.youtube_token_path()
            if token.exists():
                available.append(name)
            else:
                log.warning("skipping youtube: no token at %s", token)
        elif name == "instagram":
            if (
                cfg.meta_access_token
                and cfg.instagram_user_id
                and cfg.r2_account_id
                and cfg.r2_access_key_id
                and cfg.r2_secret_access_key
                and cfg.r2_bucket
                and cfg.r2_public_base_url
            ):
                available.append(name)
            else:
                log.warning(
                    "skipping instagram: need META_ACCESS_TOKEN, INSTAGRAM_USER_ID, "
                    "and full R2_* credentials"
                )
    if not available:
        raise RuntimeError(
            "No publish platforms available. For YouTube, set secret "
            "YOUTUBE_TOKEN_JSON. For Instagram, set META_ACCESS_TOKEN, "
            "INSTAGRAM_USER_ID, and R2_* secrets."
        )
    return available


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily Hindi short: render + publish")
    parser.add_argument("--idea", help="override the next ideas.txt entry")
    parser.add_argument(
        "--publish",
        default=os.getenv("PUBLISH_PLATFORMS", "youtube,instagram"),
        metavar="PLATFORMS",
        help="platforms CSV (default: youtube,instagram)",
    )
    parser.add_argument(
        "--no-motion",
        action="store_true",
        default=os.getenv("NO_MOTION", "").lower() in {"1", "true", "yes"},
        help="Ken Burns only (no FAL spend)",
    )
    parser.add_argument(
        "--keep-idea",
        action="store_true",
        help="do not move the idea to ideas_used.txt after success",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    load_dotenv(ROOT / ".env")
    cfg = load_config()

    if args.no_motion or not cfg.fal_key:
        if not args.no_motion and not cfg.fal_key:
            log.info("FAL_KEY not set; using --no-motion")
        cfg.motion.enabled = False

    try:
        platforms = _resolve_platforms(args.publish)
    except ValueError as exc:
        parser.error(str(exc))
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    try:
        ensure_available()
    except FFmpegError as exc:
        log.error("%s", exc)
        return 1

    idea = next_idea(cfg, args.idea)
    log.info("platforms: %s", ",".join(platforms))
    log.info("idea: %s", idea)

    try:
        run_pipeline(
            cfg,
            idea,
            force=False,
            review=False,
            mode="seed",
            platforms=platforms,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("daily post failed: %s", exc)
        return 1

    if not args.keep_idea:
        _consume_idea(idea)
        log.info("recorded idea in %s", IDEAS_USED.name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
