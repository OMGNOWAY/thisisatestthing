"""Local, network-free tests for Vercel download slot lifecycle."""

import asyncio
import tempfile
import threading
import unittest
import sys
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as backend


class EnvironmentLimitTests(unittest.TestCase):
    def test_env_value_100_is_not_capped(self):
        with patch.dict(backend.os.environ, {"MAX_CONCURRENT_DOWNLOADS": "100"}):
            self.assertEqual(backend._configured_download_slots(), 100)

    def test_env_value_1(self):
        with patch.dict(backend.os.environ, {"MAX_CONCURRENT_DOWNLOADS": "1"}):
            self.assertEqual(backend._configured_download_slots(), 1)

    def test_invalid_env_values_use_default(self):
        for value in ("0", "-5", "invalid", ""):
            with self.subTest(value=value), patch.dict(
                backend.os.environ, {"MAX_CONCURRENT_DOWNLOADS": value}
            ):
                self.assertEqual(backend._configured_download_slots(), 2)



class DownloadConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.slots_patch = patch.object(backend, "DOWNLOAD_SLOTS", threading.BoundedSemaphore(2))
        self.slots_patch.start()
        self.addCleanup(self.slots_patch.stop)
        self.free_patch = patch.object(backend, "MIN_FREE_TMP_MB", 0)
        self.free_patch.start()
        self.addCleanup(self.free_patch.stop)

    async def test_two_downloads_can_run_before_third_is_rejected(self):
        def fake_download(url, format_id, folder):
            target = folder / "output.mp3"
            target.write_bytes(b"test mp3")
            return target, "audio/mpeg", "mp3"

        request = backend.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            formatId="bestaudio/best",
            title="test",
        )
        with patch.object(backend, "download_sync", side_effect=fake_download):
            first = await backend.api_download(request)
            second = await backend.api_download(request)
            try:
                with self.assertRaises(HTTPException) as caught:
                    await backend.api_download(request)
                self.assertEqual(caught.exception.status_code, 503)
                self.assertIn("Retry-After", caught.exception.headers)
                await first.background()
                third = await backend.api_download(request)
                await third.background()
            finally:
                # Idempotent cleanup even when already called.
                await first.background()
                await second.background()

    async def test_slot_released_after_failed_conversion(self):
        request = backend.DownloadRequest(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            formatId="bestaudio/best",
        )
        with patch.object(backend, "download_sync", side_effect=RuntimeError("Conversion failed")):
            for _ in range(3):
                with self.assertRaises(HTTPException):
                    await backend.api_download(request)
            # If slots leaked, the third request would hit the busy error.
            self.assertTrue(backend.DOWNLOAD_SLOTS.acquire(blocking=False))
            self.assertTrue(backend.DOWNLOAD_SLOTS.acquire(blocking=False))
            backend.DOWNLOAD_SLOTS.release()
            backend.DOWNLOAD_SLOTS.release()

    async def test_lease_cleanup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "job"
            root.mkdir()
            token = root / "output.mp3"
            token.write_bytes(b"result")
            self.assertTrue(backend.DOWNLOAD_SLOTS.acquire(blocking=False))
            lease = backend.DownloadLease(root)
            chunks = list(backend.file_stream(token, lease))
            self.assertEqual(chunks, [b"result"])
            lease.close()
            self.assertFalse(root.exists())
            # Two further slots should be available after the stream closes.
            self.assertTrue(backend.DOWNLOAD_SLOTS.acquire(blocking=False))
            self.assertTrue(backend.DOWNLOAD_SLOTS.acquire(blocking=False))
            backend.DOWNLOAD_SLOTS.release()
            backend.DOWNLOAD_SLOTS.release()


if __name__ == "__main__":
    unittest.main()
