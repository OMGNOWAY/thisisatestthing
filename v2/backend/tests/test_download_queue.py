"""Tests for the public status returned by queued download jobs."""

import asyncio
import sys
import types
import unittest
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# The project test environment does not install the downloader's optional
# runtime dependency; the queue tests never invoke yt-dlp itself.
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

import main  # noqa: E402


class DownloadQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_jobs_report_fifo_positions_without_an_eta(self):
        queue = main.DownloadQueue(workers=1)
        first = await queue.add(main.DownloadRequest(url="https://example.com/one", formatId="best"))
        second = await queue.add(main.DownloadRequest(url="https://example.com/two", formatId="best"))

        self.assertEqual(first, {"id": first["id"], "status": "queued", "queuePosition": 1})
        self.assertEqual(second, {"id": second["id"], "status": "queued", "queuePosition": 2})
        self.assertNotIn("estimatedTime", second)

    async def test_downloading_status_has_no_queue_position(self):
        queue = main.DownloadQueue(workers=1)
        started = asyncio.Event()
        release = asyncio.Event()

        async def download_stub(*_args):
            started.set()
            await release.wait()
            return "C:/temp/result.mp4"

        original_download = main.download_video
        main.download_video = download_stub
        try:
            await queue.start()
            job = await queue.add(main.DownloadRequest(url="https://example.com/one", formatId="best"))
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertEqual(queue.status(job["id"]), {"id": job["id"], "status": "downloading"})
        finally:
            release.set()
            await queue.stop()
            main.download_video = original_download


if __name__ == "__main__":
    unittest.main()
