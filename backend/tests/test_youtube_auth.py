"""Regression tests for persistent, non-automated YouTube authentication."""

import importlib
import os
import stat
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


class YouTubeAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {"YOUTUBE_AUTH_DIR": self.temp.name, "YOUTUBE_COOKIE_FILE": str(Path(self.temp.name) / "cookies.txt")},
            clear=False,
        )
        self.environment.start()
        import google_login
        self.auth = importlib.reload(google_login)

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_missing_authentication(self):
        self.assertFalse(self.auth.status()["authenticated"])
        self.assertEqual(self.auth.status()["status"], "not_authenticated")

    def test_persisted_authentication_survives_reload(self):
        self.auth.ensure_storage()
        self.auth.cookie_file().write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        self.auth.record_authenticated(1)
        reloaded = importlib.reload(self.auth)
        self.assertTrue(reloaded.status()["authenticated"])

    def test_expired_authentication_is_reported(self):
        self.auth.ensure_storage()
        self.auth.cookie_file().write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        self.auth.record_authenticated(1)
        self.auth.mark_expired()
        self.assertFalse(self.auth.status()["authenticated"])
        self.assertEqual(self.auth.status()["status"], "expired")

    def test_storage_permissions_are_private_on_posix(self):
        self.auth.ensure_storage()
        self.auth.record_authenticated(1)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.auth.auth_dir().stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.auth.state_file().stat().st_mode), 0o600)

    def test_setup_lock_rejects_concurrent_setup(self):
        acquired = threading.Event()
        release = threading.Event()
        def blocking_which(name):
            acquired.set()
            release.wait(1)
            return None

        with patch.object(self.auth.shutil, "which", side_effect=blocking_which):
            worker = threading.Thread(target=lambda: self._ignore_setup_error())
            worker.start()
            acquired.wait(1)
            with self.assertRaises(self.auth.LoginError):
                self.auth.start_interactive_setup()
            release.set()
            worker.join(2)

    def _ignore_setup_error(self):
        try:
            self.auth.start_interactive_setup()
        except self.auth.LoginError:
            pass

    def test_downloader_reuses_persisted_cookie_state(self):
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
        import downloader
        downloader = importlib.reload(downloader)
        cookie = ".youtube.com\tTRUE\t/\tFALSE\t2147483647\tSID\tvalue"
        downloader.save_cookies(cookie)
        original = Path(downloader.COOKIES_PATH).read_text(encoding="utf-8")
        with downloader._cookie_copy("youtube") as copied:
            self.assertIsNotNone(copied)
            self.assertNotEqual(Path(copied), Path(downloader.COOKIES_PATH))
            self.assertEqual(Path(copied).read_text(encoding="utf-8"), original)
        self.assertEqual(Path(downloader.COOKIES_PATH).read_text(encoding="utf-8"), original)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(Path(downloader.COOKIES_PATH).stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
