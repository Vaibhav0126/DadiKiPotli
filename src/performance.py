"""Feedback loop: record each Short's plan, read its views back, weight the next plan.

- record_upload() runs after a successful YouTube upload and writes data/uploads.json.
- refresh_stats() reads viewCount and likeCount for uploads at least 48h old. It needs
  the youtube.readonly scope. Without it, it skips with a message and changes nothing.
- scores_from() turns the stats into weights for src/ideas.py. Each option's average
  views is shrunk toward the global average, so one lucky video cannot dominate.
  Videos younger than 48h are ignored because their numbers are still climbing.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from googleapiclient.discovery import build

from .config import ROOT, Config
from .ideas import Scores
from .publish_engine import youtube_credentials

log = logging.getLogger(__name__)

UPLOADS_PATH = ROOT / "data" / "uploads.json"
MIN_AGE = timedelta(hours=48)
# Shrinkage strength, in videos: an option with few uploads is pulled this many
# pseudo-videos toward the global average.
PRIOR_WEIGHT = 3
READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
_STATS_BATCH = 50


def load(path: Path = UPLOADS_PATH) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("uploads", [])


def save(uploads: list[dict], path: Path = UPLOADS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"uploads": uploads}, indent=2, ensure_ascii=False)
    path.write_text(body + "\n", encoding="utf-8")


def record_upload(
    *,
    video_id: str,
    character: str,
    premise: str,
    kind: str,
    published_at: datetime,
    path: Path = UPLOADS_PATH,
) -> None:
    uploads = load(path)
    if any(u["video_id"] == video_id for u in uploads):
        return
    uploads.append(
        {
            "video_id": video_id,
            "character": character,
            "premise": premise,
            "kind": kind,
            "published_at": published_at.astimezone(timezone.utc).isoformat(),
            "views": None,
            "likes": None,
            "stats_at": None,
        }
    )
    save(uploads, path)


def _age(upload: dict, now: datetime) -> timedelta:
    return now - datetime.fromisoformat(upload["published_at"])


def shrunk_multipliers(views_by_option: dict[str, list[int]], global_mean: float) -> dict[str, float]:
    """Each option's expected views as a multiple of the global average.

    An option with no data would score exactly 1.0, so it is neither favoured
    nor punished. An option with many good videos rises above 1.0 gradually.
    """
    out: dict[str, float] = {}
    for option, views in views_by_option.items():
        n = len(views)
        mean = sum(views) / n
        shrunk = (n * mean + PRIOR_WEIGHT * global_mean) / (n + PRIOR_WEIGHT)
        out[option] = shrunk / global_mean if global_mean > 0 else 1.0
    return out


def scores_from(uploads: list[dict], now: datetime) -> Scores:
    """Planner weights from uploads that are old enough and have stats."""
    ready = [
        u for u in uploads
        if u.get("views") is not None and _age(u, now) >= MIN_AGE
    ]
    if not ready:
        return Scores()

    global_mean = sum(u["views"] for u in ready) / len(ready)

    def group(key: str) -> dict[str, list[int]]:
        grouped: dict[str, list[int]] = defaultdict(list)
        for u in ready:
            if u.get(key):
                grouped[u[key]].append(u["views"])
        return dict(grouped)

    return Scores(
        kind=shrunk_multipliers(group("kind"), global_mean),
        character=shrunk_multipliers(group("character"), global_mean),
        premise=shrunk_multipliers(group("premise"), global_mean),
    )


def _token_has_scope(token_path: Path, scope: str) -> bool:
    if not token_path.exists():
        return False
    scopes = json.loads(token_path.read_text(encoding="utf-8")).get("scopes", [])
    return scope in scopes


def refresh_stats(cfg: Config, *, now: datetime | None = None, path: Path = UPLOADS_PATH) -> str:
    """Update view counts for uploads at least MIN_AGE old. Returns a one-line status."""
    now = now or datetime.now(timezone.utc)
    uploads = load(path)
    due = [u for u in uploads if _age(u, now) >= MIN_AGE]
    if not due:
        return "no uploads old enough to read yet"

    token = cfg.youtube_token_path()
    if not _token_has_scope(token, READONLY_SCOPE):
        return (
            f"skipped: {token.name} lacks youtube.readonly. Run "
            "python -m tools.auth_youtube --reauth to turn the feedback loop on"
        )

    creds = youtube_credentials(cfg, interactive=False)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    ids = [u["video_id"] for u in due]
    stats: dict[str, tuple[int, int]] = {}
    for start in range(0, len(ids), _STATS_BATCH):
        batch = ",".join(ids[start:start + _STATS_BATCH])
        resp = youtube.videos().list(part="statistics", id=batch).execute()
        for item in resp.get("items", []):
            counts = item.get("statistics", {})
            stats[item["id"]] = (
                int(counts.get("viewCount", 0)),
                int(counts.get("likeCount", 0)),
            )

    stamp = now.isoformat()
    for upload in due:
        if upload["video_id"] in stats:
            upload["views"], upload["likes"] = stats[upload["video_id"]]
            upload["stats_at"] = stamp
    save(uploads, path)
    return f"updated {len(stats)} of {len(due)} uploads"


def scores(cfg: Config, *, now: datetime | None = None, path: Path = UPLOADS_PATH) -> Scores:
    """Refresh what can be refreshed, then return the planner weights.

    A failed refresh is logged and does not stop the day's post. The weights
    simply come from the stats already on disk.
    """
    now = now or datetime.now(timezone.utc)
    try:
        log.info("performance: %s", refresh_stats(cfg, now=now, path=path))
    except Exception as exc:  # noqa: BLE001 - feedback is optional
        log.warning("performance: stats refresh failed (%s); using saved stats", exc)
    return scores_from(load(path), now)
