"""Small, request-based FastAPI backend for Vercel's Python runtime.

Keeps /api/analyze and /api/download compatible with the existing React UI.
No Render queue, persistent local files, Docker image or browser dependency.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

import imageio_ffmpeg
import yt_dlp
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

import git_auth_sync

logger = logging.getLogger("vercel_downloader")
logging.basicConfig(level=logging.INFO)

ROOT = Path(__file__).resolve().parent
DENO = ROOT / "vendor" / "deno"
if DENO.is_file():
    os.environ["PATH"] = str(DENO.parent) + os.pathsep + os.getenv("PATH", "")
os.environ.setdefault("DENO_DIR", "/tmp/vercel-deno-cache")

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
# Vercel Hobby has 1 vCPU and 500 MB shared scratch space per function
# instance. Two simultaneous MP4 -> MP3 downloads is a starting point.
# This is *per warm instance*, not a global limit across Vercel.
def _configured_download_slots():
    try:
        return max(1, min(2, int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))))
    except ValueError:
        return 2


MAX_CONCURRENT_DOWNLOADS = _configured_download_slots()
DOWNLOAD_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)
ANALYZE_LOCK = threading.BoundedSemaphore(1)
MAX_SOURCE_MB = 120  # per media file, not a total scratch-space guarantee
MIN_FREE_TMP_MB = 90  # reserve space for MP3 conversion, thumbnail and Deno
SUPPORTED_HOSTS = ("youtube.com", "youtu.be", "facebook.com", "fb.watch", "fb.com", "instagram.com")

app = FastAPI(title="Video Downloader — Vercel")
origins = [x.strip().rstrip("/") for x in os.getenv("FRONTEND_URL", "*").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length", "Retry-After"],
)


class AnalyzeRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    formatId: str
    title: str = ""


class CookiesRequest(BaseModel):
    cookies: str


def validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        raise HTTPException(400, "Invalid URL")
    if parsed.scheme != "https" or not any(
        host == allowed or host.endswith("." + allowed) for allowed in SUPPORTED_HOSTS
    ):
        raise HTTPException(400, "Only HTTPS YouTube, Facebook and Instagram links are supported.")


def is_youtube(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def failure(exc: Exception) -> HTTPException:
    message = str(exc)
    if "HTTP Error 429" in message or "Too Many Requests" in message:
        return HTTPException(503, "YouTube is rate-limiting this server. Try again later.", headers={"Retry-After": "900"})
    if "HTTP Error 403" in message or "403: Forbidden" in message:
        return HTTPException(502, "YouTube refused the media request (HTTP 403).")
    if "Requested format is not available" in message:
        return HTTPException(422, "This video does not offer the requested MP4 stream.")
    logger.warning("Upstream extraction/download failed: %s", type(exc).__name__)
    return HTTPException(502, "Could not fetch this video. Check function logs for details.")


@contextlib.contextmanager
def youtube_cookie_file():
    """Credentials come from encrypted Git storage or a Vercel env var.

    Only temporary copies live in /tmp. Do not ship cookies in the repo.
    """
    with tempfile.TemporaryDirectory(prefix="yt-cookies-", dir="/tmp") as folder:
        path = Path(folder) / "cookies.txt"
        if git_auth_sync.enabled():
            try:
                has_cookies = git_auth_sync.pull(path)
            except git_auth_sync.GitAuthError as exc:
                raise RuntimeError("Could not restore encrypted YouTube cookies") from exc
            if has_cookies:
                yield str(path)
                return
        raw = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
        if raw:
            try:
                data = base64.b64decode(raw, validate=True)
            except ValueError as exc:
                raise RuntimeError("Invalid YOUTUBE_COOKIES_B64 setting") from exc
            if len(data) > 512 * 1024:
                raise RuntimeError("YouTube cookies are too large")
            path.write_bytes(data)
            path.chmod(0o600)
            yield str(path)
        else:
            yield None


def ydl_options(cookiefile=None):
    result = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 1,
        "fragment_retries": 2,
        "ffmpeg_location": FFMPEG,
    }
    if cookiefile:
        result["cookiefile"] = cookiefile
    return result


def format_choices(info):
    source = info.get("formats", [])
    heights = sorted({
        f.get("height") for f in source if f.get("height") and f.get("vcodec", "none") != "none"
    }, reverse=True)
    formats = []
    for height in heights:
        sizes = [f.get("filesize") or f.get("filesize_approx") or 0 for f in source if f.get("height") == height]
        formats.append({
            "formatId": (
                f"bestvideo[vcodec^=avc1][ext=mp4][height<={height}]+bestaudio[ext=m4a]/"
                f"best[ext=mp4][height<={height}]"
            ),
            "ext": "mp4",
            "resolution": f"{height}p",
            "fps": max((f.get("fps") or 0 for f in source if f.get("height") == height), default=30),
            "filesize": max(sizes, default=0) or None,
            "hasVideo": True,
            "hasAudio": True,
            "label": f"{height}p Video + Audio",
        })
    formats.append({
        "formatId": "bestaudio/best",
        "ext": "mp3",
        "resolution": "audio only",
        "fps": None,
        "filesize": int((info.get("duration") or 0) * 40000) or None,
        "hasVideo": False,
        "hasAudio": True,
        "label": "Audio Only (MP3 320kbps)",
    })
    return formats


def analyze_sync(url):
    with ANALYZE_LOCK, youtube_cookie_file() as cookies:
        with yt_dlp.YoutubeDL(ydl_options(cookies)) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError("Video metadata unavailable")
        host_type = "Video"
        return {
            "platform": "youtube" if is_youtube(url) else "other",
            "contentType": "video",
            "title": info.get("title"),
            "thumbnailUrl": info.get("thumbnail"),
            "durationSeconds": info.get("duration"),
            "formats": format_choices(info),
        }


MP4_FOR_MP3 = (
    "best[ext=mp4][vcodec!=none][acodec!=none][height<=360]/"
    "best[ext=mp4][vcodec!=none][acodec!=none]/"
    "bestvideo[ext=mp4][height<=360]+bestaudio[ext=m4a]/"
    "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
)


def download_sync(url, format_id, folder: Path):
    mp3 = ("bestaudio" in format_id and "bestvideo" not in format_id) or format_id == "mp3"
    opts = ydl_options()
    opts.update({
        "format": MP4_FOR_MP3 if mp3 else format_id,
        "outtmpl": str(folder / "source.%(ext)s"),
        "merge_output_format": "mp4",
        "writethumbnail": mp3,
        # Reject unreasonably large sources before filling serverless disk.
        "max_filesize": MAX_SOURCE_MB * 1024 * 1024,
    })
    with youtube_cookie_file() as cookies:
        if cookies:
            opts["cookiefile"] = cookies
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}

        possible_mp4 = folder / "source.mp4"
        if not possible_mp4.is_file():
            files = list(folder.glob("source*.mp4"))
            if not files:
                raise RuntimeError("The selected MP4 video was not created")
            possible_mp4 = files[0]

        if not mp3:
            return possible_mp4, "video/mp4", "mp4"

        target = folder / "output.mp3"
        title = str(info.get("track") or info.get("title") or "")
        artist = str(info.get("artist") or info.get("creator") or info.get("uploader") or "")
        artist = re.sub(r"\s*-\s*Topic$", "", artist, flags=re.I).strip()
        album = str(info.get("album") or info.get("title") or "")

        covers = [p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
        cover = covers[0] if covers else None
        command = [
            FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(possible_mp4),
        ]
        if cover:
            command += ["-i", str(cover)]
        command += ["-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "320k", "-threads:a", "1"]
        if cover:
            command += ["-map", "1:v:0", "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
        command += [
            "-id3v2_version", "3",
            "-metadata", f"title={title}",
            "-metadata", f"artist={artist}",
            "-metadata", f"album={album}",
            str(target),
        ]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not target.is_file():
            raise RuntimeError("FFmpeg MP4-to-MP3 conversion failed: " + (proc.stderr or "")[-350:])
        # Free disk before streaming the finished file to the browser.
        for other in folder.iterdir():
            if other != target and other.is_file():
                other.unlink(missing_ok=True)
        return target, "audio/mpeg", "mp3"


class DownloadLease:
    """Hold a per-instance slot until the response finishes streaming."""
    def __init__(self, folder: Path):
        self.folder = folder
        self.closed = False
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        try:
            shutil.rmtree(self.folder, ignore_errors=True)
        finally:
            DOWNLOAD_SLOTS.release()


def file_stream(path: Path, lease: DownloadLease):
    try:
        with path.open("rb") as stream:
            while True:
                data = stream.read(256 * 1024)
                if not data:
                    break
                yield data
    finally:
        lease.close()


def safe_filename(title: str, suffix: str) -> str:
    basename = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title or "download").strip(" .")[:100]
    return (basename or "download") + "." + suffix


@app.get("/")
def health():
    return {
        "ok": True,
        "service": "vercel-backend",
        "ffmpeg": bool(FFMPEG),
        "deno": DENO.is_file(),
        "maxConcurrentDownloadsPerInstance": MAX_CONCURRENT_DOWNLOADS,
    }


@app.get("/api/health")
def api_health():
    return health()


@app.post("/api/analyze")
async def api_analyze(req: AnalyzeRequest):
    validate_url(req.url)
    try:
        return await asyncio.to_thread(analyze_sync, req.url)
    except Exception as exc:
        raise failure(exc) from exc


@app.post("/api/download")
async def api_download(req: DownloadRequest):
    validate_url(req.url)
    if not req.formatId or len(req.formatId) > 500:
        raise HTTPException(400, "Invalid formatId")
    # Don't queue unbounded requests in a serverless function: waiting would
    # consume the function's 5-minute budget without making progress.
    if not DOWNLOAD_SLOTS.acquire(blocking=False):
        raise HTTPException(
            503,
            f"This server instance is processing its maximum of {MAX_CONCURRENT_DOWNLOADS} downloads. Try again shortly.",
            headers={"Retry-After": "10"},
        )
    lease = None
    try:
        # A warm Vercel instance shares its 500 MB /tmp space across requests.
        if shutil.disk_usage("/tmp").free < MIN_FREE_TMP_MB * 1024 * 1024:
            raise HTTPException(
                503, "Not enough temporary disk space for another download.",
                headers={"Retry-After": "15"},
            )
        folder = Path(tempfile.mkdtemp(prefix="yt-job-", dir="/tmp"))
        lease = DownloadLease(folder)
        path, mime, extension = await asyncio.to_thread(download_sync, req.url, req.formatId, folder)
        filename = safe_filename(req.title, extension)
        # Keep the slot for the entire streamed response, not only FFmpeg.
        # Both cleanup paths are idempotent for disconnects and normal responses.
        return StreamingResponse(
            file_stream(path, lease),
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            background=BackgroundTask(lease.close),
        )
    except Exception as exc:
        if lease is not None:
            lease.close()
        else:
            DOWNLOAD_SLOTS.release()
        if isinstance(exc, HTTPException):
            raise
        raise failure(exc) from exc


def require_admin(token: str | None):
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected or not token or not hmac.compare_digest(expected, token):
        raise HTTPException(401, "Invalid or missing ADMIN_TOKEN")


def validate_cookies(text: str) -> int:
    if len(text.encode("utf-8")) > 512 * 1024:
        raise HTTPException(400, "Cookies file is too large")
    lines = [line for line in text.splitlines() if line.strip() and
             (not line.startswith("#") or line.startswith("#HttpOnly_"))]
    if not lines or any(len(line.split("\t")) != 7 for line in lines):
        raise HTTPException(400, "Upload a Netscape cookies.txt file")
    return len(lines)


@app.get("/api/cookies/status")
def cookie_status():
    # On Vercel, the file itself is ephemeral. These are the persistent sources.
    loaded = git_auth_sync.enabled() or bool(os.getenv("YOUTUBE_COOKIES_B64"))
    return {"loaded": loaded, "authenticated": loaded,
            "status": "configured" if loaded else "not_authenticated"}


@app.post("/api/cookies")
async def upload_cookies(req: CookiesRequest, x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    if not git_auth_sync.enabled():
        raise HTTPException(503, "Persistent cookie uploads require encrypted Git auth settings on Vercel.")
    count = validate_cookies(req.cookies)
    with tempfile.TemporaryDirectory(prefix="yt-admin-", dir="/tmp") as temporary:
        path = Path(temporary) / "cookies.txt"
        path.write_text(req.cookies, encoding="utf-8")
        path.chmod(0o600)
        try:
            await asyncio.to_thread(git_auth_sync.push, path, "sync YouTube auth")
        except git_auth_sync.GitAuthError as exc:
            raise HTTPException(502, "Could not save encrypted cookies to Git") from exc
    return {"ok": True, "count": count}


@app.delete("/api/cookies")
async def delete_cookies(x_admin_token: str | None = Header(default=None)):
    require_admin(x_admin_token)
    if not git_auth_sync.enabled():
        raise HTTPException(503, "Persistent cookie deletion requires encrypted Git auth settings.")
    try:
        await asyncio.to_thread(git_auth_sync.remove_remote)
    except git_auth_sync.GitAuthError as exc:
        raise HTTPException(502, "Could not delete encrypted cookies from Git") from exc
    return {"ok": True}
