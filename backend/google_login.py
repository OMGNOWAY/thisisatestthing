"""Supported, one-time YouTube authentication setup.

This module deliberately never submits a Google username, password, TOTP code,
or challenge response. An administrator signs in interactively in a dedicated,
persistent Chrome profile, then exports a Netscape cookies file to the configured
auth directory. The downloader reuses that file until it expires or is revoked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path


class LoginError(Exception):
    """Compatibility name for expected authentication setup errors."""


_setup_lock = threading.Lock()


def diagnose_google_rejection(message: str) -> str:
    """Return a precise, non-evasive diagnostic for Google's browser rejection."""
    lowered = (message or "").lower()
    if "/v3/signin/rejected" in lowered or "this browser or app may not be secure" in lowered:
        return "Google rejected this browser/session before authentication. Use the supported interactive authentication setup."
    return message


def auth_dir() -> Path:
    return Path(os.getenv("YOUTUBE_AUTH_DIR", "/var/lib/myapp/youtube/auth"))


def cookie_file() -> Path:
    configured = os.getenv("YOUTUBE_COOKIE_FILE") or os.getenv("YOUTUBE_COOKIES_PATH")
    return Path(configured) if configured else auth_dir() / "cookies.txt"


def profile_dir() -> Path:
    return auth_dir() / "chrome-profile"


def state_file() -> Path:
    return auth_dir() / "state.json"


def ensure_storage() -> None:
    """Create private auth storage without placing credentials in the repo."""
    for directory in (auth_dir(), profile_dir()):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass  # Windows does not support POSIX modes.


def _write_state(**values: object) -> None:
    ensure_storage()
    target = state_file()
    temp = target.with_suffix(".tmp")
    payload = {"lastAuthenticated": int(time.time()), **values}
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.write("\n")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, target)


def record_authenticated(cookie_count: int) -> None:
    """Record non-secret setup metadata after an administrator imports cookies."""
    _write_state(status="authenticated", cookieCount=cookie_count)


def mark_expired() -> None:
    """Record an observed authentication failure; never attempt a login."""
    _write_state(status="expired")


def status() -> dict:
    """Return safe status only; tokens, cookies, and account details stay private."""
    exists = cookie_file().is_file()
    state: dict = {}
    try:
        state = json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {
        "authenticated": exists and state.get("status") != "expired",
        "status": state.get("status", "authenticated" if exists else "not_authenticated"),
        "updatedAt": cookie_file().stat().st_mtime if exists else None,
        "lastAuthenticated": state.get("lastAuthenticated"),
        "profileDir": str(profile_dir()),
    }


def is_configured() -> bool:
    """Legacy compatibility: true means usable persisted authentication exists."""
    return bool(status()["authenticated"])


def start_interactive_setup() -> dict:
    """Launch normal Chrome with a persistent profile for an admin-controlled login.

    The administrator must connect to this display (for example through VNC) and
    perform Google's normal interactive authentication. This does not automate
    or evade Google security controls, and it does not create cookies.txt.
    """
    if not _setup_lock.acquire(blocking=False):
        raise LoginError("Interactive YouTube authentication setup is already running.")
    try:
        ensure_storage()
        if os.name != "nt" and not os.environ.get("DISPLAY"):
            raise LoginError("No graphical display is available. Start an administrator-controlled VNC, remote-desktop, or SSH X11 session before starting interactive YouTube authentication.")
        chrome = next((shutil.which(name) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser") if shutil.which(name)), None)
        if not chrome:
            raise LoginError("Chrome/Chromium was not found. Install it and start a VNC or desktop session, then use the dedicated profile directory shown in status.")
        command = [chrome, f"--user-data-dir={profile_dir()}", "https://www.youtube.com/"]
        # Intentionally no automation, fingerprint, user-agent, or webdriver flags.
        process = subprocess.Popen(command, start_new_session=True)
        return {"started": True, "pid": process.pid, "profileDir": str(profile_dir()), "message": "Chrome was started with the persistent profile. Complete Google sign-in interactively, export cookies.txt, and import it with the admin endpoint."}
    finally:
        _setup_lock.release()


def fetch_cookies(cooldown: int | None = None) -> str:
    """Removed unsafe legacy API; retained only to avoid silently automating login."""
    raise LoginError("Automatic Google password login is disabled. Use the supported interactive authentication setup and import its cookies.txt once.")
