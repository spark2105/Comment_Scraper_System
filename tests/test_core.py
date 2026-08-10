from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.cleaner import DescriptionCleaner, DescriptionCleaningError
from app.database import (
    get_video,
    save_video_with_comments,
    statistics,
    update_description_clean,
    videos_requiring_description_cleaning,
)
from app.job_defaults import load_job_defaults, save_job_defaults
from app.scraper import extract_video_id, parse_channel_identifier


class CoreTests(unittest.TestCase):
    def test_video_url_and_channel_parsing(self) -> None:
        self.assertEqual(
            extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(
            extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=10"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(parse_channel_identifier("@example"), ("handle", "@example"))

    def test_video_unit_is_saved_incrementally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comments.sqlite3"
            video = {
                "id": "video123",
                "channel_id": "channel123",
                "channel_title": "Channel",
                "title": "Title",
                "description": "Raw description",
                "published_at": "2026-01-01T00:00:00Z",
                "scraped_at": "2026-08-09T00:00:00Z",
            }
            thread = {
                "id": "thread123",
                "top_level_comment_id": "comment123",
                "total_reply_count": 0,
            }
            comment = {
                "id": "comment123",
                "thread_id": "thread123",
                "video_id": "video123",
                "text": "A comment",
                "depth": 0,
            }
            save_video_with_comments(video, [thread], [comment], path)
            update_description_clean("video123", "", "cleaned", path)
            self.assertIn("video123", videos_requiring_description_cleaning(path))
            update_description_clean("video123", "Clean description", "cleaned", path)
            self.assertEqual(statistics(path), {"videos": 1, "threads": 1, "comments": 1})
            stored = get_video("video123", path)
            self.assertEqual(stored["description_clean"], "Clean description")
            self.assertEqual(
                set(stored["comments"][0]),
                {"comment_id", "thread_id", "video_id", "parent_id", "published_at", "updated_at", "text", "depth"},
            )
            self.assertNotIn("video123", videos_requiring_description_cleaning(path))

    def test_cleaner_uses_model_and_rejects_empty_output(self) -> None:
        cleaner = DescriptionCleaner(model="llama3.2")
        cleaner.client = Mock()
        cleaner.client.chat.return_value = {"message": {"content": " Cleaned description "}}
        self.assertEqual(cleaner.clean("Raw description", "Clean it."), "Cleaned description")
        self.assertEqual(cleaner.model, "llama3.2")

        cleaner.client.chat.return_value = {"message": {"content": "   "}}
        with self.assertRaises(DescriptionCleaningError):
            cleaner.clean("Raw description", "Clean it.")

    def test_job_defaults_are_persisted_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job_defaults.json"
            with patch("app.job_defaults.DEFAULTS_PATH", path):
                save_job_defaults(
                    {
                        "channels": ["@example"],
                        "video_urls": ["https://youtu.be/dQw4w9WgXcQ"],
                        "max_videos_per_channel": 4,
                        "max_comment_threads_per_video": 30,
                        "include_replies": False,
                        "description_prompt": "Keep facts only.",
                        "api_key": "must-not-be-stored",
                    }
                )
                loaded = load_job_defaults()
            self.assertEqual(loaded["channels"], ["@example"])
            self.assertEqual(loaded["max_videos_per_channel"], 4)
            self.assertNotIn("api_key", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
