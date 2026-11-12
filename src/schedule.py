"""The daily posting slot.

13:30 UTC is 19:00 IST: the evening peak for Indian viewers. The workflow's cron
has to match PUBLISH_HOUR_UTC and PUBLISH_MINUTE_UTC; tests/test_schedule.py
checks that it does.

Note the old cron, `0 4 * * *`, was described as 13:00 JST. It was actually
09:30 IST, which is well before the evening audience.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

PUBLISH_HOUR_UTC = 13
PUBLISH_MINUTE_UTC = 30


def publish_cron() -> str:
    return f"{PUBLISH_MINUTE_UTC} {PUBLISH_HOUR_UTC} * * *"


def next_publish_time(now: datetime) -> datetime:
    """The first slot strictly after `now`. A naive `now` is read as UTC."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    slot = now.replace(
        hour=PUBLISH_HOUR_UTC, minute=PUBLISH_MINUTE_UTC, second=0, microsecond=0
    )
    if slot <= now:
        slot += timedelta(days=1)
    return slot
