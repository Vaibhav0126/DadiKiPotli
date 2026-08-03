"""Pick one story idea and publish a YouTube Short.

Used by the daily GitHub Action:

    python -m tools.daily_post
    python -m tools.daily_post --idea "..."
    python -m tools.daily_post --from-queue
    python -m tools.daily_post --no-motion

By default, Gemini invents a fresh idea each run (avoiding anything already in
ideas_used.txt). Pass --from-queue to take the next unused line from ideas.txt
instead. On success, the idea is appended to ideas_used.txt.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ROOT, Config, load_config  # noqa: E402
from src.ffmpeg_util import FFmpegError, ensure_available  # noqa: E402
from src.main import run_pipeline  # noqa: E402

log = logging.getLogger("daily_post")

IDEAS = ROOT / "ideas.txt"
IDEAS_USED = ROOT / "ideas_used.txt"

_CHARACTERS = ("Akbar-Birbal", "Tenali Raman", "Vikram-Betaal")
_MAX_INVENT_ATTEMPTS = 4


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


def _norm(idea: str) -> str:
    return " ".join(idea.casefold().split())


def _consume_idea(idea: str) -> None:
    """Remove idea from ideas.txt (if present) and append it to ideas_used.txt."""
    queue = _read_queue(IDEAS)
    remaining = [line for line in queue if _norm(line) != _norm(idea)]
    if len(remaining) != len(queue):
        _rewrite_queue(
            IDEAS,
            remaining,
            header=(
                "# Optional backlog of story ideas (one per line).\n"
                "# Used only with: python -m tools.daily_post --from-queue\n"
                "# Daily Action invents a fresh idea by default."
            ),
        )

    used = _read_queue(IDEAS_USED)
    if _norm(idea) not in {_norm(u) for u in used}:
        used.append(idea)
    _rewrite_queue(
        IDEAS_USED,
        used,
        header="# Ideas already posted by tools.daily_post (newest at the bottom).",
    )


def _banned_ideas() -> list[str]:
    """Ideas we must not invent again (already used + still queued)."""
    seen: set[str] = set()
    out: list[str] = []
    for idea in _read_queue(IDEAS_USED) + _read_queue(IDEAS):
        key = _norm(idea)
        if key and key not in seen:
            seen.add(key)
            out.append(idea)
    return out


def _invent_idea(cfg: Config) -> str:
    log.info("inventing a fresh idea with Gemini")
    client = genai.Client(api_key=cfg.gemini_api_key)
    banned = _banned_ideas()
    banned_block = ""
    if banned:
        # Keep the prompt bounded; newest rejects matter most.
        recent = banned[-40:]
        listed = "\n".join(f"- {idea}" for idea in recent)
        banned_block = (
            "\n\nDo NOT repeat, paraphrase, or lightly remix any of these "
            f"already-used ideas:\n{listed}\n"
        )

    last_error: Exception | None = None
    for attempt in range(1, _MAX_INVENT_ATTEMPTS + 1):
        cast = random.choice(_CHARACTERS)
        prompt = (
            "Invent ONE original one-line story idea for a Hindi moral short "
            f"featuring {cast}. "
            "The premise must be new: a different conflict, test, or riddle "
            "than common textbook retellings. "
            "Return ONLY the idea sentence in English, no quotes, no numbering, "
            "no explanation. Keep it under 20 words."
            f"{banned_block}"
        )
        resp = None
        for model in cfg.story.models:
            try:
                resp = client.models.generate_content(model=model, contents=prompt)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                text = str(exc)
                if (
                    "NOT_FOUND" not in text
                    and "404" not in text
                    and "not available" not in text
                ):
                    raise
                log.debug("model %s unavailable, trying next", model)
        if resp is None:
            raise RuntimeError(f"Could not invent an idea via Gemini: {last_error}")
        idea = (getattr(resp, "text", None) or "").strip().strip('"').strip("'")
        if not idea:
            log.warning("Gemini returned an empty idea (attempt %d)", attempt)
            continue
        idea = " ".join(idea.split())
        if _norm(idea) in {_norm(b) for b in banned}:
            log.warning("invented a duplicate idea, retrying: %s", idea)
            continue
        log.info("invented idea: %s", idea)
        return idea

    raise RuntimeError("Gemini kept returning empty or duplicate story ideas")


def next_idea(cfg: Config, override: str | None, *, from_queue: bool) -> str:
    if override and override.strip():
        return override.strip()
    if from_queue:
        queue = _read_queue(IDEAS)
        if queue:
            return queue[0]
        log.info("ideas.txt is empty; falling back to invent")
    return _invent_idea(cfg)


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily Hindi short: render + YouTube")
    parser.add_argument("--idea", help="use this idea instead of inventing one")
    parser.add_argument(
        "--from-queue",
        action="store_true",
        help="take the next unused line from ideas.txt instead of inventing",
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
        help="do not append the idea to ideas_used.txt after success",
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

    if not cfg.youtube_token_path().exists():
        log.error(
            "YouTube token missing at %s. Set YOUTUBE_TOKEN_JSON in GitHub "
            "Actions or run: python -m tools.auth_youtube",
            cfg.youtube_token_path(),
        )
        return 1

    try:
        ensure_available()
    except FFmpegError as exc:
        log.error("%s", exc)
        return 1

    idea = next_idea(cfg, args.idea, from_queue=args.from_queue)
    log.info("idea: %s", idea)

    try:
        run_pipeline(
            cfg,
            idea,
            force=False,
            review=False,
            mode="seed",
            publish=True,
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
