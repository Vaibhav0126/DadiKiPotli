"""Pick one story idea and publish to YouTube.

Used by the daily Shorts Action and the Sunday long-form Action:

    python -m tools.daily_post
    python -m tools.daily_post --profile long
    python -m tools.daily_post --idea "..."
    python -m tools.daily_post --from-queue
    python -m tools.daily_post --no-motion

By default the next post is planned by src/ideas.py: about half the time a
famous classic from prompts/classics.txt (each one once), otherwise a new story.
Character family and premise type never repeat from the previous post. Gemini
writes the one-line idea for new stories, and ideas that break the rules are
rejected and retried. Pass --from-queue to take the next unused line from the
ideas file instead. On success, the idea is appended to the used list with its
kind, character and premise.

Short profile: ideas.txt / ideas_used.txt
Long profile:  ideas_long.txt / ideas_long_used.txt
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google import genai

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import performance  # noqa: E402
from src.characters import DISPLAY_NAMES, detect_character  # noqa: E402
from src.config import ROOT, Config, load_config, work_dir  # noqa: E402
from src.ffmpeg_util import FFmpegError, ensure_available  # noqa: E402
from src.gemini_util import generate_with_fallback  # noqa: E402
from src.ideas import (  # noqa: E402
    Classic,
    Entry,
    Plan,
    Scores,
    choose_plan,
    idea_problem,
    load_classics,
    norm,
    parse_entries,
)
from src.main import run_pipeline  # noqa: E402
from src.models import read_json, slugify  # noqa: E402
from src.publish_engine import PublishResult  # noqa: E402
from src.schedule import next_publish_time  # noqa: E402

log = logging.getLogger("daily_post")

QUEUES = {
    "short": (ROOT / "ideas.txt", ROOT / "ideas_used.txt"),
    "long": (ROOT / "ideas_long.txt", ROOT / "ideas_long_used.txt"),
}
CLASSICS_FILE = ROOT / "prompts" / "classics.txt"

_MAX_INVENT_ATTEMPTS = 6

_PREMISE_HELP = {
    "trick": "someone outwits a cheat or schemer with a clever trick",
    "rescue": "someone is trapped in danger and must find a way out",
    "test_of_wits": "a ruler or rival sets an impossible test that a wise answer defeats",
    "comeuppance": "a boastful or greedy person gets exactly what they deserve, by their own rules",
    "riddle": "a riddle or puzzle must be solved before a deadline",
}


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


def _read_lines(path: Path) -> list[str]:
    """Non-empty, non-comment lines, as written."""
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _read_queue(path: Path) -> list[str]:
    """Queue ideas as plain text (for --from-queue and the fallback)."""
    return [e.idea for e in parse_entries(_read_lines(path))]


def _rewrite_queue(path: Path, remaining: list[str], *, header: str) -> None:
    body = "\n".join(remaining)
    text = f"{header.rstrip()}\n\n{body}\n" if body else f"{header.rstrip()}\n"
    path.write_text(text, encoding="utf-8")


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


def _used_header(profile: str) -> str:
    return (
        "# Ideas already posted by tools.daily_post "
        f"({_queue_key(profile)} queue; newest at the bottom).\n"
        "# Format: [kind|character|premise] idea. Lines without a prefix are older posts."
    )


def _history(profile: str) -> list[Entry]:
    return parse_entries(_read_lines(_used_path(profile)))


def _classics() -> list[Classic]:
    if not CLASSICS_FILE.exists():
        return []
    return load_classics(CLASSICS_FILE)


def _consume_idea(idea: str, plan: Plan, *, profile: str) -> None:
    """Drop the idea from the queue file if present, and record it as used."""
    ideas_path = _ideas_path(profile)
    used_path = _used_path(profile)

    queue = _read_lines(ideas_path)
    remaining = [line for line in queue if norm(line) != norm(idea)]
    if len(remaining) != len(queue):
        _rewrite_queue(ideas_path, remaining, header=_ideas_header(profile))

    used = _read_lines(used_path)
    if norm(idea) not in {norm(e.idea) for e in parse_entries(used)}:
        used.append(plan.record(idea))
    _rewrite_queue(used_path, used, header=_used_header(profile))


def _banned_ideas(profile: str) -> list[str]:
    """Ideas we must not invent again (already used + still queued)."""
    seen: set[str] = set()
    out: list[str] = []
    for idea in [e.idea for e in _history(profile)] + _read_queue(_ideas_path(profile)):
        key = norm(idea)
        if key and key not in seen:
            seen.add(key)
            out.append(idea)
    return out


def _scores(cfg: Config, profile: str) -> Scores:
    """Planner weights from past performance. Only Shorts are tracked."""
    if _queue_key(profile) != "short":
        return Scores()
    return performance.scores(cfg)


def _plan_for_override(idea: str) -> Plan:
    """A plan for an idea we did not plan: family from its text, premise unknown."""
    return Plan("new", detect_character(idea) or "", "")


def _record_upload(idea: str, plan: Plan) -> None:
    """Remember what was posted so the next run can weigh how it did."""
    result_path = work_dir(slugify(idea)) / "publish_result.json"
    if not result_path.exists():
        log.warning("no publish result found for %r; not recording the upload", idea)
        return
    result = read_json(result_path, PublishResult)
    if not result.youtube_video_id:
        return
    performance.record_upload(
        video_id=result.youtube_video_id,
        character=plan.character or detect_character(idea) or "",
        premise=plan.premise,
        kind=plan.kind,
        published_at=datetime.now(timezone.utc),
    )
    log.info("recorded upload %s in %s", result.youtube_video_id, performance.UPLOADS_PATH.name)


def _invent_prompt(plan: Plan, banned_block: str, retry_note: str) -> str:
    who = DISPLAY_NAMES[plan.character]
    return (
        f"Invent ONE original one-line story idea for a Hindi moral short featuring {who}.\n"
        f"Premise: {_PREMISE_HELP[plan.premise]}.\n"
        "The idea needs a concrete stake and a question the viewer wants answered, "
        "like 'how will he escape?', or else it must be genuinely funny or emotional.\n"
        "Do NOT write lawsuits, disputes, verdicts, questions of treason or virtue, "
        "or 'is it a sin' debates. Do NOT write 'Betaal asks Vikram if'.\n"
        "Return ONLY the idea sentence in English, no quotes, no numbering, no "
        "explanation. Keep it under 20 words."
        f"{banned_block}{retry_note}"
    )


def _invent_idea(cfg: Config, plan: Plan, *, profile: str) -> str:
    kind = _queue_key(profile)
    log.info("inventing a new %s idea (%s, %s)", kind, plan.character, plan.premise)
    client = genai.Client(api_key=cfg.gemini_api_key)
    banned = _banned_ideas(profile)
    banned_block = ""
    if banned:
        listed = "\n".join(f"- {idea}" for idea in banned[-40:])
        banned_block = (
            "\n\nDo NOT repeat, paraphrase, or lightly remix any of these "
            f"already-used ideas:\n{listed}\n"
        )

    retry_note = ""
    for attempt in range(1, _MAX_INVENT_ATTEMPTS + 1):
        prompt = _invent_prompt(plan, banned_block, retry_note)

        def _one(model: str, _prompt: str = prompt) -> object:
            return client.models.generate_content(model=model, contents=_prompt)

        resp, model = generate_with_fallback(
            models=list(cfg.story.models),
            call=_one,
            attempts_per_model=2,
            label="Gemini invent",
        )
        log.debug("invent used model %s", model)

        idea = (getattr(resp, "text", None) or "").strip().strip('"').strip("'")
        idea = " ".join(idea.split())
        if not idea:
            log.warning("Gemini returned an empty idea (attempt %d)", attempt)
            retry_note = "\n\nYour last reply was empty. Write one idea sentence."
            continue

        problem = idea_problem(idea)
        if problem is None and norm(idea) in {norm(b) for b in banned}:
            problem = "duplicate of a used idea"
        if problem:
            log.warning("rejected idea (%s), retrying: %s", problem, idea)
            retry_note = (
                f"\n\nYour last idea was rejected ({problem}): '{idea}'. "
                "Write a different one that follows every rule above."
            )
            continue

        log.info("invented idea: %s", idea)
        return idea

    raise RuntimeError("Gemini kept returning empty, duplicate or banned story ideas")


def _choose(cfg: Config, profile: str) -> tuple[str, Plan]:
    """Plan the next post and return (idea text, plan). Classics need no Gemini call."""
    history = _history(profile)
    classics = _classics() if _queue_key(profile) == "short" else []
    plan = choose_plan(history, classics, random.Random(), _scores(cfg, profile))
    if plan.kind == "classic":
        log.info("planned classic: %s (%s, %s)", plan.idea, plan.character, plan.premise)
        return plan.idea or "", plan
    log.info("planned new story: %s, %s", plan.character, plan.premise)
    return "", plan


def next_idea(
    cfg: Config, override: str | None, *, from_queue: bool, profile: str
) -> tuple[str, Plan]:
    if override and override.strip():
        text = override.strip()
        return text, _plan_for_override(text)

    if from_queue:
        queue = _read_queue(_ideas_path(profile))
        if queue:
            return queue[0], _plan_for_override(queue[0])
        log.info("%s is empty; falling back to invent", _ideas_path(profile).name)

    classic_idea, plan = _choose(cfg, profile)
    if classic_idea:
        return classic_idea, plan

    try:
        return _invent_idea(cfg, plan, profile=profile), plan
    except Exception as exc:  # noqa: BLE001 - backlog is the fallback for CI
        queue = _read_queue(_ideas_path(profile))
        if queue:
            log.warning("Gemini invent unavailable (%s); using queued idea: %s", exc, queue[0])
            return queue[0], _plan_for_override(queue[0])
        open_classics = _open_classics(profile)
        if open_classics:
            fallback = open_classics[0]
            log.warning("Gemini invent unavailable (%s); using classic: %s", exc, fallback.idea)
            return fallback.idea, Plan("classic", fallback.character, fallback.premise, fallback.idea)
        raise


def _open_classics(profile: str) -> list[Classic]:
    if _queue_key(profile) != "short":
        return []
    done = {norm(e.idea) for e in _history(profile)}
    return [c for c in _classics() if norm(c.idea) not in done]


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

    idea, plan = next_idea(cfg, args.idea, from_queue=args.from_queue, profile=profile)
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
        _consume_idea(idea, plan, profile=profile)
        log.info("recorded idea in %s", _used_path(profile).name)
    if _queue_key(profile) == "short":
        _record_upload(idea, plan)

    log.info("next scheduled slot: %s", next_publish_time(datetime.now(timezone.utc)).isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
