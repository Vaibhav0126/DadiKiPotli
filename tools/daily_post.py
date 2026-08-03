"""Pick one story idea and publish to YouTube.

Used by the daily Shorts Action and the Sunday long-form Action:

    python -m tools.daily_post
    python -m tools.daily_post --profile long
    python -m tools.daily_post --idea "..."
    python -m tools.daily_post --from-queue
    python -m tools.daily_post --no-motion

By default, Gemini invents a fresh idea each run (avoiding anything already in
the used list). Pass --from-queue to take the next unused line from the ideas
file instead. On success, the idea is appended to the used list.

Short profile: ideas.txt / ideas_used.txt
Long profile:  ideas_long.txt / ideas_long_used.txt
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

QUEUES = {
    "short": (ROOT / "ideas.txt", ROOT / "ideas_used.txt"),
    "long": (ROOT / "ideas_long.txt", ROOT / "ideas_long_used.txt"),
}

_CHARACTERS = ("Akbar-Birbal", "Tenali Raman", "Vikram-Betaal")
_MAX_INVENT_ATTEMPTS = 4


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _queue_key(profile: str) -> str:
    name = (profile or "short").strip().lower()
    if name in {"long", "landscape", "sunday"}:
        return "long"
    return "short"


def _ideas_path(profile: str) -> Path:
    return QUEUES[_queue_key(profile)][0]


def _used_path(profile: str) -> Path:
    return QUEUES[_queue_key(profile)][1]


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


def _ideas_header(profile: str) -> str:
    if _queue_key(profile) == "long":
        return (
            "# Optional backlog of long-form story ideas (one per line).\n"
            "# Used only with: python -m tools.daily_post --profile long --from-queue\n"
            "# Sunday Action invents a fresh idea by default."
        )
    return (
        "# Optional backlog of story ideas (one per line).\n"
        "# Used only with: python -m tools.daily_post --from-queue\n"
        "# Daily Action invents a fresh idea by default."
    )


def _consume_idea(idea: str, *, profile: str) -> None:
    """Remove idea from the ideas file (if present) and append it to used."""
    ideas_path = _ideas_path(profile)
    used_path = _used_path(profile)
    queue = _read_queue(ideas_path)
    remaining = [line for line in queue if _norm(line) != _norm(idea)]
    if len(remaining) != len(queue):
        _rewrite_queue(ideas_path, remaining, header=_ideas_header(profile))

    used = _read_queue(used_path)
    if _norm(idea) not in {_norm(u) for u in used}:
        used.append(idea)
    _rewrite_queue(
        used_path,
        used,
        header=(
            "# Ideas already posted by tools.daily_post "
            f"({_queue_key(profile)} queue; newest at the bottom)."
        ),
    )


def _banned_ideas(profile: str) -> list[str]:
    """Ideas we must not invent again (already used + still queued)."""
    seen: set[str] = set()
    out: list[str] = []
    for idea in _read_queue(_used_path(profile)) + _read_queue(_ideas_path(profile)):
        key = _norm(idea)
        if key and key not in seen:
            seen.add(key)
            out.append(idea)
    return out


def _invent_idea(cfg: Config, *, profile: str) -> str:
    kind = _queue_key(profile)
    log.info("inventing a fresh %s idea with Gemini", kind)
    client = genai.Client(api_key=cfg.gemini_api_key)
    banned = _banned_ideas(profile)
    banned_block = ""
    if banned:
        recent = banned[-40:]
        listed = "\n".join(f"- {idea}" for idea in recent)
        banned_block = (
            "\n\nDo NOT repeat, paraphrase, or lightly remix any of these "
            f"already-used ideas:\n{listed}\n"
        )

    last_error: Exception | None = None
    for attempt in range(1, _MAX_INVENT_ATTEMPTS + 1):
        cast = random.choice(_CHARACTERS)
        if kind == "long":
            prompt = (
                "Invent ONE original one-line story idea for a longer Hindi moral "
                f"tale (6+ minutes) featuring {cast}. "
                "It needs room for setup, rising conflict, and a clever twist. "
                "The premise must be new: a different conflict, test, or riddle "
                "than common textbook retellings. "
                "Return ONLY the idea sentence in English, no quotes, no numbering, "
                "no explanation. Keep it under 25 words."
                f"{banned_block}"
            )
        else:
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


def next_idea(cfg: Config, override: str | None, *, from_queue: bool, profile: str) -> str:
    if override and override.strip():
        return override.strip()
    if from_queue:
        queue = _read_queue(_ideas_path(profile))
        if queue:
            return queue[0]
        log.info("%s is empty; falling back to invent", _ideas_path(profile).name)
    return _invent_idea(cfg, profile=profile)


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish one Hindi story to YouTube")
    parser.add_argument("--idea", help="use this idea instead of inventing one")
    parser.add_argument(
        "--profile",
        default=os.getenv("POST_PROFILE", "short"),
        help="short (daily Shorts) or long (Sunday 16:9)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="explicit config yaml path (overrides --profile)",
    )
    parser.add_argument(
        "--from-queue",
        action="store_true",
        help="take the next unused line from the ideas file instead of inventing",
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
        help="do not append the idea to the used list after success",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    load_dotenv(ROOT / ".env")
    cfg = load_config(path=args.config, profile=args.profile)
    profile = _queue_key(args.profile)

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

    idea = next_idea(cfg, args.idea, from_queue=args.from_queue, profile=profile)
    log.info("profile: %s (%s)", profile, "16:9 long" if profile == "long" else "9:16 short")
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
        _consume_idea(idea, profile=profile)
        log.info("recorded idea in %s", _used_path(profile).name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
