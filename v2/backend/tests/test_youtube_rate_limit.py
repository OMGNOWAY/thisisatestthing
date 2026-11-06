"""Regression tests for handling YouTube's upstream HTTP 429 cooldown."""

import contextlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

try:
    import yt_dlp
except ImportError:
    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = object
    pp = types.ModuleType("yt_dlp.postprocessor")
    common = types.ModuleType("yt_dlp.postprocessor.common")
    common.PostProcessor = object
    sys.modules.update({
        "yt_dlp": fake,
        "yt_dlp.postprocessor": pp,
        "yt_dlp.postprocessor.common": common,
    })

import downloader


class YtDlpRateLimitTests(unittest.TestCase):
    def setUp(self):
        original = downloader._YT_COOLDOWN_UNTIL
        downloader._YT_COOLDOWN_UNTIL = 0.0
        self.addCleanup(setattr, downloader, "_YT_COOLDOWN_UNTIL", original)

    def test_429_warning_triggers_cooldown(self):
        downstream = downloader._YtDlpLog()
        downstream.warning("[youtube] HTTP Error 429: Too Many Requests")
        self.assertGreater(downloader.youtube_retry_after(), 0)
        with self.assertRaises(downloader.YouTubeRateLimited):
            downloader._check_youtube_cooldown("youtube")
        downloader._check_youtube_cooldown("facebook")

    def test_403_warning_does_not_trigger_429_cooldown(self):
        downloader._YtDlpLog().warning("[youtube] HTTP Error 403: Forbidden")
        self.assertEqual(downloader.youtube_retry_after(), 0)

    def test_429_skips_anonymous_fallback(self):
        class FakeDL:
            calls = 0

            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, url, download=False):
                FakeDL.calls += 1
                self.opts["logger"].warning(
                    "[youtube] Unable to download webpage: HTTP Error 429: Too Many Requests"
                )
                raise RuntimeError("Failed to extract any player response")

        @contextlib.contextmanager
        def cookie_copy(platform):
            yield "/tmp/cookies.txt"

        FakeDL.calls = 0
        with patch.object(downloader.yt_dlp, "YoutubeDL", FakeDL), patch.object(
            downloader, "_cookie_copy", cookie_copy
        ):
            with self.assertRaises(downloader.YouTubeRateLimited):
                downloader._extract_video_info("https://www.youtube.com/watch?v=test", "youtube")
        self.assertEqual(FakeDL.calls, 1)


if __name__ == "__main__":
    unittest.main()
