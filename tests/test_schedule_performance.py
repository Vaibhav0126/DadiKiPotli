"""Publish-time calculation, the workflow cron, and performance weighting.

    .venv/bin/python -m unittest tests.test_schedule_performance
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import performance
from src.config import ROOT, Config
from src.schedule import next_publish_time, publish_cron

UTC = timezone.utc
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


class PublishTimeTests(unittest.TestCase):
    def test_before_todays_slot_gives_todays_slot(self) -> None:
        self.assertEqual(
            next_publish_time(datetime(2026, 11, 12, 6, 0, tzinfo=UTC)),
            datetime(2026, 11, 12, 13, 30, tzinfo=UTC),
        )

    def test_after_todays_slot_gives_tomorrows(self) -> None:
        self.assertEqual(
            next_publish_time(datetime(2026, 11, 12, 14, 0, tzinfo=UTC)),
            datetime(2026, 11, 13, 13, 30, tzinfo=UTC),
        )

    def test_exactly_on_the_slot_gives_the_next_day(self) -> None:
        self.assertEqual(
            next_publish_time(datetime(2026, 11, 12, 13, 30, tzinfo=UTC)),
            datetime(2026, 11, 13, 13, 30, tzinfo=UTC),
        )

    def test_slot_is_19_00_ist(self) -> None:
        ist = timezone(timedelta(hours=5, minutes=30))
        slot = next_publish_time(datetime(2026, 11, 12, 0, 0, tzinfo=UTC))
        self.assertEqual(slot.astimezone(ist).strftime("%H:%M"), "19:00")

    def test_naive_time_is_read_as_utc(self) -> None:
        self.assertEqual(
            next_publish_time(datetime(2026, 11, 12, 6, 0)),
            datetime(2026, 11, 12, 13, 30, tzinfo=UTC),
        )

    def test_workflow_cron_matches_the_slot(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "daily-post.yml").read_text(encoding="utf-8")
        self.assertIn(f'cron: "{publish_cron()}"', workflow)
        self.assertEqual(publish_cron(), "30 13 * * *")


def _upload(video_id: str, character: str, premise: str, kind: str, views: int | None, age_h: float) -> dict:
    return {
        "video_id": video_id,
        "character": character,
        "premise": premise,
        "kind": kind,
        "published_at": (NOW - timedelta(hours=age_h)).isoformat(),
        "views": views,
        "likes": None,
        "stats_at": None,
    }


class PerformanceTests(unittest.TestCase):
    def test_videos_younger_than_48_hours_are_ignored(self) -> None:
        uploads = [
            _upload("a", "Tenali Raman", "trick", "new", 10_000, age_h=10),
            _upload("b", "Akbar-Birbal", "trick", "new", 100, age_h=60),
        ]
        scores = performance.scores_from(uploads, NOW)
        self.assertNotIn("Tenali Raman", scores.character)
        self.assertIn("Akbar-Birbal", scores.character)

    def test_uploads_without_stats_are_ignored(self) -> None:
        uploads = [_upload("a", "Tenali Raman", "trick", "new", None, age_h=60)]
        scores = performance.scores_from(uploads, NOW)
        self.assertEqual(scores.character, {})
        self.assertEqual(scores.kind, {})

    def test_one_lucky_video_is_shrunk_toward_the_mean(self) -> None:
        # Global mean of 1000 views across two families. Family A has one video
        # at 10x the mean; family B has several at the mean.
        uploads = [
            _upload("a1", "Tenali Raman", "trick", "new", 10_000, age_h=60),
        ] + [
            _upload(f"b{i}", "Akbar-Birbal", "trick", "new", 1000, age_h=60)
            for i in range(4)
        ]
        global_mean = (10_000 + 4 * 1000) / 5  # 2800
        scores = performance.scores_from(uploads, NOW)

        # (1 * 10000 + 3 * 2800) / (1 + 3) = 4600 -> multiplier 4600 / 2800
        self.assertAlmostEqual(scores.character["Tenali Raman"], 4600 / global_mean)
        self.assertLess(scores.character["Tenali Raman"], 10_000 / global_mean)

    def test_option_with_no_data_is_neutral(self) -> None:
        uploads = [_upload("a", "Tenali Raman", "trick", "new", 500, age_h=60)]
        scores = performance.scores_from(uploads, NOW)
        # Only Tenali has data, so the other families are absent and planner
        # treats them as 1.0 (average).
        self.assertNotIn("Vikram-Betaal", scores.character)

    def test_record_upload_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "uploads.json"
            for _ in range(2):
                performance.record_upload(
                    video_id="vid1",
                    character="Tenali Raman",
                    premise="trick",
                    kind="new",
                    published_at=NOW,
                    path=path,
                )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(data["uploads"]), 1)
            self.assertEqual(data["uploads"][0]["views"], None)

    def test_refresh_skips_with_a_clear_message_when_scope_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            token = tmp_path / "token.json"
            token.write_text(json.dumps({"scopes": ["https://www.googleapis.com/auth/youtube.upload"]}))
            uploads_path = tmp_path / "uploads.json"
            performance.save([_upload("a", "Tenali Raman", "trick", "new", None, age_h=60)], uploads_path)

            old = os.environ.get("YOUTUBE_TOKEN_PATH")
            os.environ["YOUTUBE_TOKEN_PATH"] = str(token)
            try:
                status = performance.refresh_stats(Config(), now=NOW, path=uploads_path)
            finally:
                if old is None:
                    os.environ.pop("YOUTUBE_TOKEN_PATH", None)
                else:
                    os.environ["YOUTUBE_TOKEN_PATH"] = old

            self.assertIn("lacks youtube.readonly", status)
            self.assertIn("--reauth", status)

    def test_no_old_uploads_means_no_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            uploads_path = Path(tmp) / "uploads.json"
            performance.save([_upload("a", "Tenali Raman", "trick", "new", None, age_h=2)], uploads_path)
            status = performance.refresh_stats(Config(), now=NOW, path=uploads_path)
            self.assertEqual(status, "no uploads old enough to read yet")


if __name__ == "__main__":
    unittest.main()
