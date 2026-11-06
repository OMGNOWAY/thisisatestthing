"""Encrypted Git-backed YouTube auth sync.

The repository only ever receives an encrypted cookies.txt payload. The
encryption key and GitHub token stay in environment variables.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

REMOTE_PATH = os.getenv("YOUTUBE_AUTH_GIT_PATH", "youtube-auth/cookies.enc")


class GitAuthError(Exception):
    pass


def _repo() -> str:
    return os.getenv("YOUTUBE_AUTH_GIT_REPO", "").strip()


def _token() -> str:
    return os.getenv("YOUTUBE_AUTH_GIT_TOKEN", "").strip()


def _branch() -> str:
    return os.getenv("YOUTUBE_AUTH_GIT_BRANCH", "main").strip() or "main"


def _key() -> bytes:
    value = os.getenv("YOUTUBE_AUTH_ENCRYPTION_KEY", "").strip()
    if not value:
        raise GitAuthError("YOUTUBE_AUTH_ENCRYPTION_KEY is not configured.")
    try:
        key = value.encode("ascii")
        Fernet(key)
        return key
    except Exception as exc:
        raise GitAuthError("YOUTUBE_AUTH_ENCRYPTION_KEY is not a valid Fernet key.") from exc


def enabled() -> bool:
    return bool(_repo() and _token() and os.getenv("YOUTUBE_AUTH_ENCRYPTION_KEY", "").strip())


def _api_url(path: str = "") -> str:
    return "https://api.github.com/repos/" + _repo().strip("/") + "/contents/" + path.lstrip("/")


def _request(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", "Bearer " + _token())
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "thisisatestthing-youtube-auth-sync")
    if data is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitAuthError(f"GitHub auth sync failed ({exc.code}): {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise GitAuthError(f"GitHub auth sync connection failed: {exc.reason}") from exc


def _encrypt(cookie_text: str) -> str:
    encrypted = Fernet(_key()).encrypt(cookie_text.encode("utf-8"))
    return encrypted.decode("ascii")


def _decrypt(encoded: str) -> str:
    try:
        return Fernet(_key()).decrypt(encoded.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise GitAuthError("The Git-backed YouTube auth could not be decrypted.") from exc


def pull(cookie_path: str | Path) -> bool:
    """Pull the encrypted cookie file into the local auth directory.

    Returns True when a remote auth file was found and restored.
    """
    if not enabled():
        return False

    url = _api_url(REMOTE_PATH) + "?" + urllib.parse.urlencode({"ref": _branch()})
    try:
        result = _request("GET", url)
    except GitAuthError as exc:
        # A missing remote file is normal on first setup.
        if "(404)" in str(exc):
            return False
        raise

    encoded = base64.b64decode(result["content"].replace("\n", "")).decode("utf-8")
    cookie_text = _decrypt(encoded)

    target = Path(cookie_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".git-tmp")
    temp.write_text(cookie_text, encoding="utf-8", newline="\n")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, target)
    return True


def push(cookie_path: str | Path, message: str = "sync YouTube auth") -> bool:
    """Encrypt and commit the local cookie file to the configured Git repo."""
    if not enabled():
        return False

    source = Path(cookie_path)
    if not source.is_file():
        return False

    encoded = base64.b64encode(_encrypt(source.read_text(encoding="utf-8")).encode("utf-8")).decode("ascii")

    # Get the existing blob SHA so GitHub updates it instead of creating
    # duplicate files. A 404 simply means this is the first upload.
    sha = None
    url = _api_url(REMOTE_PATH) + "?" + urllib.parse.urlencode({"ref": _branch()})
    try:
        current = _request("GET", url)
        sha = current.get("sha")
    except GitAuthError as exc:
        if "(404)" not in str(exc):
            raise

    payload = {
        "message": message,
        "content": encoded,
        "branch": _branch(),
    }
    if sha:
        payload["sha"] = sha

    _request("PUT", _api_url(REMOTE_PATH), payload)
    return True


def remove_remote() -> bool:
    """Delete the remote encrypted auth file."""
    if not enabled():
        return False

    url = _api_url(REMOTE_PATH) + "?" + urllib.parse.urlencode({"ref": _branch()})
    try:
        current = _request("GET", url)
    except GitAuthError as exc:
        if "(404)" in str(exc):
            return False
        raise

    _request(
        "DELETE",
        _api_url(REMOTE_PATH),
        {
            "message": "remove YouTube auth",
            "sha": current["sha"],
            "branch": _branch(),
        },
    )
    return True
