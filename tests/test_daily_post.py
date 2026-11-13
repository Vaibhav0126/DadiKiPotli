"""One Short per UTC day. No network, no Gemini.

    .venv/bin/python -m unittest tests.test_daily_post
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tools.daily_post import _posted_today


class OneShortADayTests(unittest.TestCase):
    def test_same_utc_day_counts_as_posted(self) -> None:
        uploads = [{"video_id": "abc", "published_at": "2026-11-13T06:11:55+00:00"}]
        now = datetime(2026, 11, 13, 13, 30, tzinfo=timezone.utc)
        self.assertEqual(_posted_today(uploads, now), "abc")

    def test_previous_day_does_not_block(self) -> None:
        uploads = [{"video_id": "abc", "published_at": "2026-11-12T13:30:00+00:00"}]
        now = datetime(2026, 11, 13, 13, 30, tzinfo=timezone.utc)
        self.assertIsNone(_posted_today(uploads, now))

    def test_offset_time_is_compared_in_utc(self) -> None:
        # 01:00 IST on Nov 14 is still Nov 13 in UTC.
        uploads = [{"video_id": "abc", "published_at": "2026-11-14T01:00:00+05:30"}]
        now = datetime(2026, 11, 13, 13, 30, tzinfo=timezone.utc)
        self.assertEqual(_posted_today(uploads, now), "abc")

    def test_no_uploads(self) -> None:
        self.assertIsNone(_posted_today([], datetime(2026, 11, 13, tzinfo=timezone.utc)))


if __name__ == "__main__":
    unittest.main()
