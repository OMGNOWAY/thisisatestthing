from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import os
import time
import asyncio
import shutil
import re
import socket
import ipaddress
import traceback
import threading
import urllib.parse
import hmac
from starlette.background import BackgroundTask

from downloader import (
    analyze_url, download_video, save_cookies, clear_cookies, cookies_info,
    mark_auth_expired,
)
from google_login import LoginError, start_interactive_setup
import git_auth_sync

app = FastAPI(title="Video Downloader API")

def get_allowed_origins(frontend_url: str | None = None) -> list[str]:
    configured = frontend_url if frontend_url is not None else os.getenv("FRONTEND_URL", "http://localhost:5173")
    origins = [origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()]
    return origins or ["http://localhost:5173"]

ALLOWED_ORIGINS = get_allowed_origins()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp_downloads")

@app.on_event("startup")
async def restore_youtube_auth():
    if not git_auth_sync.enabled():
        return
    try:
        restored = await asyncio.to_thread(git_auth_sync.pull, __import__("downloader").COOKIES_PATH)
        if restored:
            print("[auth] Restored encrypted YouTube auth from Git.")
    except git_auth_sync.GitAuthError as exc:
        # Do not prevent the API from starting if the remote auth store is unavailable.
        print(f"[auth] Git sync restore failed: {exc}")

class AnalyzeRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    formatId: str
    title: str = ""

rate_limits = {}
rate_limits_lock = threading.Lock()

def check_rate_limit(request: Request):
    client = request.client
    ip = client.host if client else "unknown"
    now = time.time()
    with rate_limits_lock:
        if ip not in rate_limits:
            rate_limits[ip] = []
        rate_limits[ip] = [t for t in rate_limits[ip] if now - t < 60]
        if len(rate_limits[ip]) >= 60:
            raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")
        rate_limits[ip].append(now)
        # Prevent unbounded growth of the rate-limit map.
        if len(rate_limits) > 5000:
            for stale_ip in [k for k, v in rate_limits.items() if not v]:
                del rate_limits[stale_ip]

# Hosts that must never be fetched (cloud metadata, cluster-internal, etc.)
BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "metadata.google.internal.",
    "kubernetes.default.svc",
    "kubernetes.default",
}

def validate_public_url(url: str) -> str:
    """Reject non-http(s) schemes and hosts that are not publicly routable.

    Prevents the download API from being abused as an SSRF proxy against
    internal services and cloud metadata endpoints.
    """
    url = (url or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http/https URLs are supported.")
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="Invalid URL: missing host.")

    hostname_lower = hostname.lower().rstrip(".")
    if hostname_lower in BLOCKED_HOSTS:
        raise HTTPException(status_code=400, detail="This URL host is not allowed.")

    try:
        address = ipaddress.ip_address(hostname)
        _assert_public_ip(address)
    except ValueError:
        # Hostname: resolve and reject if any address is non-public.
        try:
            records = socket.getaddrinfo(hostname, None)
        except OSError:
            raise HTTPException(status_code=400, detail="URL host could not be resolved.")
        if not records:
            raise HTTPException(status_code=400, detail="URL host could not be resolved.")
        for record in records:
            try:
                address = ipaddress.ip_address(record[4][0])
            except ValueError:
                continue
            _assert_public_ip(address)
    return url

def _assert_public_ip(address):
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise HTTPException(status_code=400, detail="URL host is not publicly reachable.")

def safe_filename(title: str, ext: str) -> str:
    if not title:
        return f"download.{ext}"
    # Strip non-ASCII (emoji, CJK, etc.) — HTTP headers are latin-1 only
    safe = re.sub(r'[\\/*?:"<>|]', '', title)
    safe = safe.encode('ascii', 'ignore').decode('ascii')
    safe = re.sub(r'[\r\n\t]', '', safe)
    safe = safe.strip().replace(' ', '_')[:80]
    if not safe:
        safe = 'download'
    return f"{safe}.{ext}"

def cleanup_job_dir(job_dir: str):
    import time as _time
    _time.sleep(1)
    try:
        shutil.rmtree(job_dir, ignore_errors=True)
    except Exception:
        pass

MIME_MAP = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "ogg": "audio/ogg",
}

def needs_signin(err: Exception) -> bool:
    """True when YouTube is demanding a signed-in session (bot check / dead cookies)."""
    msg = str(err).lower()
    return "not a bot" in msg or "cookies are no longer valid" in msg

async def run_analyze(url: str):
    return await asyncio.wait_for(asyncio.to_thread(analyze_url, url), timeout=45)

@app.post("/api/analyze")
async def api_analyze(req: AnalyzeRequest, request: Request):
    check_rate_limit(request)
    validate_public_url(req.url)
    try:
        data = await run_analyze(req.url)
        if not data:
            raise HTTPException(status_code=400, detail="Could not extract metadata from this URL")
        return data
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="Analysis timed out. Please try again.")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[api] Analyze error for {req.url[:60]}: {e}")
        traceback.print_exc()
        if needs_signin(e):
            mark_auth_expired()
            raise HTTPException(status_code=401, detail="YouTube authentication has expired. Re-authentication is required.")
        raise HTTPException(status_code=400, detail="Could not analyze the provided URL.")

@app.post("/api/download")
async def api_download(req: DownloadRequest, request: Request):
    check_rate_limit(request)
    if not req.formatId:
        raise HTTPException(status_code=400, detail="URL and formatId are required")
    validate_public_url(req.url)

    try:
        os.makedirs(TEMP_DIR, exist_ok=True)
        filepath = await asyncio.wait_for(
            download_video(req.url, req.formatId, TEMP_DIR),
            timeout=300
        )

        ext = os.path.splitext(filepath)[1].lstrip('.') or "bin"
        mime = MIME_MAP.get(ext, "application/octet-stream")
        friendly_name = safe_filename(req.title, ext)

        job_dir = os.path.dirname(filepath)

        return FileResponse(
            path=filepath,
            media_type=mime,
            filename=friendly_name,
            background=BackgroundTask(cleanup_job_dir, job_dir),
            headers={
                "Content-Disposition": f'attachment; filename="{friendly_name}"'
            }
        )

    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="Download timed out (5 min limit). Try a lower quality.")
    except Exception as e:
        print(f"[api] Download error for {req.url[:60]}: {e}")
        traceback.print_exc()
        if needs_signin(e):
            mark_auth_expired()
            raise HTTPException(status_code=401, detail="YouTube authentication has expired. Re-authentication is required.")
        raise HTTPException(status_code=500, detail="Download failed. Please try again.")


# ─── YouTube cookies (for servers YouTube blocks) ─────────────────────
# Authentication setup and cookie import are locked behind ADMIN_TOKEN.

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

class CookiesRequest(BaseModel):
    cookies: str

async def require_admin(token: str | None):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Cookie upload is off. Set ADMIN_TOKEN on the server.")
    if not token or not hmac.compare_digest(token.encode(), ADMIN_TOKEN.encode()):
        await asyncio.sleep(1)
        raise HTTPException(status_code=401, detail="Wrong token.")

@app.get("/api/cookies/status")
async def api_cookies_status():
    return cookies_info()

@app.post("/api/cookies")
async def api_cookies_save(
    req: CookiesRequest,
    request: Request,
    x_admin_token: str | None = Header(default=None),
):
    check_rate_limit(request)
    await require_admin(x_admin_token)
    try:
        count = save_cookies(req.cookies)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "count": count}

@app.post("/admin/youtube/auth/start")
async def api_youtube_auth_start(
    request: Request,
    x_admin_token: str | None = Header(default=None),
):
    """Start a one-time interactive Chrome setup using a persistent profile."""
    check_rate_limit(request)
    await require_admin(x_admin_token)
    try:
        return await asyncio.to_thread(start_interactive_setup)
    except LoginError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/cookies")
async def api_cookies_clear(
    request: Request,
    x_admin_token: str | None = Header(default=None),
):
    check_rate_limit(request)
    await require_admin(x_admin_token)
    clear_cookies()
    return {"ok": True}
