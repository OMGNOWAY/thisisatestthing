"""Tests for YouTube metadata extraction when account cookies are present."""

import contextlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

try:
    import yt_dlp  # noqa: F401
except ImportError:
    fake_ytdlp = types.ModuleType("yt_dlp")
    fake_ytdlp.YoutubeDL = object
    fake_postprocessor = types.ModuleType("yt_dlp.postprocessor")
    fake_common = types.ModuleType("yt_dlp.postprocessor.common")
    fake_common.PostProcessor = object
    sys.modules.update({
        "yt_dlp": fake_ytdlp,
        "yt_dlp.postprocessor": fake_postprocessor,
        "yt_dlp.postprocessor.common": fake_common,
    })

import downloader  # noqa: E402


@contextlib.contextmanager
def fake_cookie_copy(platform):
    yield "/tmp/test-cookie.txt" if platform == "youtube" else None


class FakeDL:
    calls = []
    auth_error = False
    always_error = None

    def __init__(self, options):
        self.options = options
        self.calls.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        if self.always_error:
            raise RuntimeError(self.always_error)
        if self.auth_error and self.options.get("cookiefile"):
            raise RuntimeError("Failed to extract any player response")
        return {"id": "sample", "title": "Sample audio", "formats": []}


class YouTubePlayerResponseFallbackTests(unittest.TestCase):
    def setUp(self):
        FakeDL.calls = []
        FakeDL.auth_error = False
        FakeDL.always_error = None
        self.patches = [
            patch.object(downloader, "_cookie_copy", fake_cookie_copy),
            patch.object(downloader.yt_dlp, "YoutubeDL", FakeDL),
            patch.object(downloader, "_PLAYER_CLIENTS", []),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_authenticated_session_uses_recommended_clients(self):
        info, used_cookies = downloader._extract_video_info(
            "https://www.youtube.com/watch?v=sample", "youtube"
        )
        self.assertEqual(info["title"], "Sample audio")
        self.assertTrue(used_cookies)
        self.assertEqual(len(FakeDL.calls), 1)
        self.assertEqual(
            FakeDL.calls[0]["extractor_args"]["youtube"]["player_client"],
            ["default", "web_embedded"],
        )

    def test_player_response_failure_retries_without_cookies(self):
        FakeDL.auth_error = True
        info, used_cookies = downloader._extract_video_info(
            "https://www.youtube.com/watch?v=sample", "youtube"
        )
        self.assertEqual(info["title"], "Sample audio")
        self.assertFalse(used_cookies)
        self.assertEqual(len(FakeDL.calls), 2)
        self.assertIn("cookiefile", FakeDL.calls[0])
        self.assertNotIn("cookiefile", FakeDL.calls[1])

    def test_unrelated_errors_are_not_retried_anonymously(self):
        FakeDL.always_error = "Sign in to confirm your age"
        with self.assertRaisesRegex(RuntimeError, "Sign in"):
            downloader._extract_video_info(
                "https://www.youtube.com/watch?v=sample", "youtube"
            )
        self.assertEqual(len(FakeDL.calls), 1)


if __name__ == "__main__":
    unittest.main()
