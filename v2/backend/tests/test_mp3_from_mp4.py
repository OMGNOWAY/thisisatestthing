"""MP3 requests must download MP4 video and convert it, not fetch audio-only media."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
try:
    import yt_dlp  # noqa: F401
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


class FakeYoutubeDL:
    options_used = []
    failures_before_success = 0
    only_mp4 = False

    def __init__(self, opts):
        self.opts = opts
        self.options_used.append(opts.copy())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def add_post_processor(self, *_args, **_kwargs):
        pass

    def download(self, _urls):
        if self.failures_before_success > 0:
            FakeYoutubeDL.failures_before_success -= 1
            raise RuntimeError("Requested format is not available")
        ext = "mp4" if self.only_mp4 else "mp3"
        Path(self.opts["outtmpl"].replace("%(ext)s", ext)).write_bytes(b"test")


class Mp3FromMp4Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        FakeYoutubeDL.options_used = []
        FakeYoutubeDL.failures_before_success = 0
        FakeYoutubeDL.only_mp4 = False
        self.patches = [
            patch.object(downloader.yt_dlp, "YoutubeDL", FakeYoutubeDL),
            patch.object(downloader, "HAS_FFMPEG", True),
            patch.object(downloader, "_check_youtube_cooldown"),
            patch.object(downloader, "youtube_retry_after", return_value=0),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_mp3_download_uses_video_mp4_then_ffmpeg(self):
        target = await downloader.download_video(
            "https://www.youtube.com/watch?v=sample",
            "bestaudio/best",
            self.tempdir.name,
        )
        self.assertTrue(target.endswith(".mp3"))
        self.assertTrue(os.path.isfile(target))
        opts = FakeYoutubeDL.options_used[0]
        self.assertEqual(opts["merge_output_format"], "mp4")
        self.assertIn("[ext=mp4]", opts["format"])
        self.assertIn("[vcodec!=none]", opts["format"])
        processors = opts["postprocessors"]
        self.assertTrue(any(p["key"] == "FFmpegExtractAudio" and
                            p["preferredcodec"] == "mp3" and
                            p["preferredquality"] == "320" for p in processors))
        self.assertTrue(any(p["key"] == "EmbedThumbnail" for p in processors))
        self.assertTrue(any(p["key"] == "FFmpegMetadata" for p in processors))

    async def test_format_fallback_still_selects_mp4_video(self):
        FakeYoutubeDL.failures_before_success = 1
        target = await downloader.download_video(
            "https://www.youtube.com/watch?v=sample",
            "bestaudio/best",
            self.tempdir.name,
        )
        self.assertTrue(target.endswith(".mp3"))
        self.assertEqual(len(FakeYoutubeDL.options_used), 2)
        self.assertIn("bestvideo[ext=mp4]", FakeYoutubeDL.options_used[1]["format"])

    async def test_never_returns_leftover_video_as_mp3(self):
        FakeYoutubeDL.only_mp4 = True
        with self.assertRaisesRegex(RuntimeError, "MP3 extraction"):
            await downloader.download_video(
                "https://www.youtube.com/watch?v=sample",
                "bestaudio/best",
                self.tempdir.name,
            )


if __name__ == "__main__":
    unittest.main()
