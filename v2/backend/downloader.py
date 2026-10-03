import yt_dlp
import os
import uuid
import asyncio
import shutil
import threading
import time
import contextlib
import tempfile
import re
import logging
from yt_dlp.postprocessor.common import PostProcessor

import google_login
import git_auth_sync

logger = logging.getLogger("video_downloader.downloader")


def _url_for_log(url: str) -> str:
    """Avoid logging URL query strings, which can contain temporary tokens."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.netloc else "invalid-url"

# ─── FFmpeg Detection ─────────────────────────────────────────────────

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_FFMPEG_BUNDLED = os.path.join(_BACKEND_DIR, 'ffmpeg-master-latest-win64-gpl', 'bin')
_FFMPEG_EXE = os.path.join(_FFMPEG_BUNDLED, 'ffmpeg.exe')

if os.path.isfile(_FFMPEG_EXE):
    FFMPEG_LOCATION = _FFMPEG_BUNDLED
    logger.info("Using bundled FFmpeg at %s", FFMPEG_LOCATION)
elif shutil.which('ffmpeg'):
    FFMPEG_LOCATION = os.path.dirname(shutil.which('ffmpeg'))
    logger.info("Using system FFmpeg at %s", FFMPEG_LOCATION)
else:
    FFMPEG_LOCATION = None
    logger.warning("FFmpeg not found; audio conversion and merging will not work")

HAS_FFMPEG = FFMPEG_LOCATION is not None

# Only 2 concurrent yt-dlp+FFmpeg processes at a time (Windows file lock prevention).
# threading.Semaphore because _run() executes inside asyncio.to_thread().
_DL_SEM = threading.Semaphore(2)

# TLS certificate verification is kept enabled by default; the legacy bypass
# (some corporate networks use MITM proxies with custom CAs) can be re-enabled
# by setting YTDLP_NO_CHECK_CERTIFICATE=1.
_NO_CHECK_CERT = os.getenv("YTDLP_NO_CHECK_CERTIFICATE", "").strip().lower() in ("1", "true", "yes")

# Optional: override yt-dlp's YouTube player clients, comma separated
# (e.g. YTDLP_PLAYER_CLIENTS=default,web_embedded). Leave unset for yt-dlp's defaults.
_PLAYER_CLIENTS = [c.strip() for c in os.getenv("YTDLP_PLAYER_CLIENTS", "").split(",") if c.strip()]


def _base_opts() -> dict:
    """Return a fresh copy of base yt-dlp options (never mutate the global)."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': _NO_CHECK_CERT,
        'ignoreerrors': False,
        'extract_flat': False,
        'noplaylist': True,
        'socket_timeout': 30,
        'retries': 3,           # yt-dlp internal HTTP retries
        'fragment_retries': 5,  # retry individual DASH/HLS fragments
    }
    if FFMPEG_LOCATION:
        opts['ffmpeg_location'] = FFMPEG_LOCATION
    if _PLAYER_CLIENTS:
        opts['extractor_args'] = {'youtube': {'player_client': _PLAYER_CLIENTS}}
    return opts


class _CleanTagsPP(PostProcessor):
    """Tidy up MP3 tags before they are written: artist name and album."""

    def run(self, info):
        artist = info.get('artist') or info.get('creator') or info.get('uploader') or ''
        artist = re.sub(r'\s*-\s*Topic$', '', artist, flags=re.I).strip()  # "Artist - Topic" -> "Artist"
        if artist:
            info['artist'] = artist
        if not info.get('album') and info.get('title'):
            info['album'] = info['title']
        return [], info


# ─── YouTube Cookies ──────────────────────────────────────────────────
# YouTube cookies are set up once by an administrator, outside the source tree.
# Never derive them by submitting a Google password from this service.
COOKIES_PATH = str(google_login.cookie_file())
_COOKIES_MAX_BYTES = 512 * 1024
_COOKIE_LOCK = threading.RLock()


def save_cookies(text: str) -> int:
    """Validate a Netscape cookies.txt and store it. Returns the cookie count."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("Cookies are empty.")
    if len(text.encode("utf-8")) > _COOKIES_MAX_BYTES:
        raise ValueError("Cookies file is too large.")

    count = 0
    has_google = False
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or (stripped.startswith("#") and not stripped.startswith("#HttpOnly_")):
            continue
        if len(line.split("\t")) != 7:
            raise ValueError("Not a Netscape cookies.txt (each line needs 7 tab-separated fields).")
        count += 1
        if "youtube.com" in line or "google.com" in line:
            has_google = True
    if count == 0:
        raise ValueError("No cookies found in that file.")
    if not has_google:
        raise ValueError("No youtube.com or google.com cookies in that file.")

    if not text.startswith("# Netscape HTTP Cookie File") and not text.startswith("# HTTP Cookie File"):
        text = "# Netscape HTTP Cookie File\n" + text

    google_login.ensure_storage()
    os.makedirs(os.path.dirname(COOKIES_PATH), mode=0o700, exist_ok=True)
    with _COOKIE_LOCK:
        tmp = COOKIES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, COOKIES_PATH)
        try:
            os.chmod(COOKIES_PATH, 0o600)
        except OSError:
            pass
        google_login.record_authenticated(count)
        if git_auth_sync.enabled():
            try:
                git_auth_sync.push(COOKIES_PATH, "sync YouTube auth")
            except git_auth_sync.GitAuthError as exc:
                logger.warning("Git sync auth upload failed: %s", exc)
    return count


def clear_cookies():
    with _COOKIE_LOCK:
        try:
            os.remove(COOKIES_PATH)
        except FileNotFoundError:
            pass
        if git_auth_sync.enabled():
            try:
                git_auth_sync.remove_remote()
            except git_auth_sync.GitAuthError as exc:
                logger.warning("Git sync auth delete failed: %s", exc)


def cookies_info() -> dict:
    return {"loaded": os.path.isfile(COOKIES_PATH), **google_login.status()}


def mark_auth_expired() -> None:
    google_login.mark_expired()


@contextlib.contextmanager
def _cookie_copy(platform: str):
    """Yield a throwaway copy of the cookies file for YouTube calls (or None).

    yt-dlp rewrites its cookie file on exit, so each call gets its own copy
    to avoid concurrent calls clobbering the stored one.
    """
    if platform != "youtube" or not os.path.isfile(COOKIES_PATH):
        yield None
        return
    tmp = os.path.join(tempfile.gettempdir(), f"ytc_{uuid.uuid4().hex}.txt")
    try:
        with _COOKIE_LOCK:
            shutil.copyfile(COOKIES_PATH, tmp)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        yield tmp
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ─── Platform Detection ───────────────────────────────────────────────

def detect_platform(url: str) -> dict:
    url_lower = url.lower()
    if any(d in url_lower for d in ['youtube.com', 'youtu.be']):
        return {"platform": "youtube", "type": "short" if '/shorts/' in url_lower else "video"}
    elif 'facebook.com' in url_lower or 'fb.watch' in url_lower or 'fb.com' in url_lower:
        return {"platform": "facebook", "type": "reel" if '/reel' in url_lower else "video"}
    elif 'instagram.com' in url_lower:
        return {"platform": "instagram", "type": "reel" if '/reel' in url_lower else "post"}
    else:
        return {"platform": "other", "type": "video"}


# ─── Format Building ──────────────────────────────────────────────────

def _best_stream_size(info: dict, kind: str = 'video') -> int:
    best = 0
    for f in info.get('formats', []):
        fs = f.get('filesize') or f.get('filesize_approx') or 0
        if kind == 'video' and f.get('vcodec', 'none') != 'none':
            best = max(best, fs)
        elif kind == 'audio' and f.get('acodec', 'none') != 'none':
            best = max(best, fs)
    return best

def _build_formats(info: dict, label_suffix: str, is_short: bool = False):
    video_size = _best_stream_size(info, 'video')
    audio_size = _best_stream_size(info, 'audio')
    best_total = video_size + audio_size if video_size else audio_size

    heights = info.get('formats', [])
    available_heights = sorted(set(
        f.get('height') for f in heights
        if f.get('height') and f.get('vcodec', 'none') != 'none'
    ), reverse=True)

    best_fps = max(
        (f.get('fps') or 0 for f in heights if f.get('vcodec', 'none') != 'none'),
        default=30
    )

    formats = []
    # Prefer H.264 (avc1) for maximum compatibility (WhatsApp, iPhone, all players).
    # Falls back to any codec if H.264 isn't available at that resolution.
    formats.append({
        "formatId": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=avc1]+bestaudio/bestvideo+bestaudio/best",
        "ext": "mp4",
        "resolution": f"{available_heights[0]}p" if available_heights else "best",
        "fps": best_fps,
        "filesize": best_total if best_total > 0 else None,
        "hasVideo": True,
        "hasAudio": True,
        "label": f"Best Quality {label_suffix} + Audio (Compatible)",
    })

    for h in available_heights:
        est = 0
        for f in info.get('formats', []):
            if f.get('height') == h and f.get('vcodec', 'none') != 'none':
                est = max(est, f.get('filesize') or f.get('filesize_approx') or 0)
        est += audio_size

        fps_for_h = max(
            (f.get('fps') or 0 for f in heights
             if f.get('height') == h and f.get('vcodec', 'none') != 'none'),
            default=30
        )

        formats.append({
            "formatId": f"bestvideo[vcodec^=avc1][height<={h}]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=avc1][height<={h}]+bestaudio/bestvideo[height<={h}]+bestaudio/best[height<={h}]",
            "ext": "mp4",
            "resolution": f"{h}p",
            "fps": fps_for_h,
            "filesize": est if est > 0 else None,
            "hasVideo": True,
            "hasAudio": True,
            "label": f"{h}p {label_suffix} + Audio",
        })

    formats.append({
        "formatId": "bestaudio/best",
        "ext": "mp3",
        "resolution": "audio only",
        "fps": None,
        "filesize": audio_size if audio_size > 0 else None,
        "hasVideo": False,
        "hasAudio": True,
        "label": "Audio Only (MP3 320kbps)",
    })

    return formats


# ─── Analyze ──────────────────────────────────────────────────────────

def analyze_url(url: str):
    platform_info = detect_platform(url)
    platform = platform_info["platform"]
    content_type = platform_info["type"]
    logger.info("yt-dlp analysis started platform=%s type=%s url=%s", platform, content_type, _url_for_log(url))

    label_map = {
        "youtube": "Short" if content_type == "short" else "Video",
        "facebook": "Reel" if content_type == "reel" else "Video",
        "instagram": "Reel" if content_type == "reel" else "Post",
        "other": "Video",
    }
    is_short = platform == "youtube" and content_type == "short"

    with _cookie_copy(platform) as cookie_tmp:
        opts = _base_opts()
        if cookie_tmp:
            opts['cookiefile'] = cookie_tmp

        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                formats = _build_formats(info, label_map[platform], is_short=is_short)
                logger.info("yt-dlp analysis completed platform=%s formats=%s", platform, len(formats))
                return {
                    "platform": platform,
                    "contentType": content_type,
                    "title": info.get('title'),
                    "thumbnailUrl": info.get('thumbnail'),
                    "durationSeconds": info.get('duration'),
                    "formats": formats,
                }
            except Exception as e:
                logger.exception("yt-dlp analysis failed platform=%s url=%s", platform, _url_for_log(url))
                raise Exception(f"Failed to analyze URL: {e}")


# ─── Download ─────────────────────────────────────────────────────────

MAX_RETRIES = 3
RETRY_DELAY = 3   # seconds between retries

async def download_video(url: str, format_id: str, output_dir: str):
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(output_dir, job_id)
    os.makedirs(job_dir, exist_ok=True)

    output_template = os.path.join(job_dir, f"{job_id}.%(ext)s")

    is_audio_only = (
        'bestaudio' in format_id and 'bestvideo' not in format_id
    )
    logger.info(
        "yt-dlp download prepared job=%s platform=%s audio_only=%s url=%s",
        job_id, detect_platform(url)["platform"], is_audio_only, _url_for_log(url)
    )

    opts = _base_opts()

    if is_audio_only:
        opts.update({
            'format': 'bestaudio/best',
            'outtmpl': output_template,
        })
        if HAS_FFMPEG:
            opts['writethumbnail'] = True
            opts['postprocessors'] = [
                # Convert the thumbnail to a square-cropped jpg so players show it as cover art
                {'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg', 'when': 'before_dl'},
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '320',
                },
                {'key': 'FFmpegMetadata', 'add_metadata': True, 'add_chapters': False, 'add_infojson': None},
                {'key': 'EmbedThumbnail'},
            ]
            opts['postprocessor_args'] = {
                'thumbnailsconvertor+ffmpeg_o': [
                    '-c:v', 'mjpeg',
                    '-vf', "crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'",
                ],
            }
    else:
        opts.update({
            'format': format_id,
            'outtmpl': output_template,
        })
        if HAS_FFMPEG:
            opts['merge_output_format'] = 'mp4'
            # Force audio to AAC for universal player support.
            # Key 'merger' targets the FFmpegMergerPP specifically.
            opts['postprocessor_args'] = {
                'merger': ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k']
            }

    platform = detect_platform(url)["platform"]

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            def _run():
                logger.info("yt-dlp download waiting for worker slot job=%s attempt=%s", job_id, attempt)
                with _DL_SEM:
                    logger.info("yt-dlp download started job=%s attempt=%s", job_id, attempt)
                    with _cookie_copy(platform) as cookie_tmp:
                        run_opts = dict(opts)
                        if cookie_tmp:
                            run_opts['cookiefile'] = cookie_tmp
                        with yt_dlp.YoutubeDL(run_opts) as ydl:
                            if is_audio_only and HAS_FFMPEG:
                                ydl.add_post_processor(_CleanTagsPP(), when='pre_process')
                            ydl.download([url])
                logger.info("yt-dlp download finished processing job=%s attempt=%s", job_id, attempt)

            await asyncio.to_thread(_run)

            # Find the produced file
            for fname in os.listdir(job_dir):
                full = os.path.join(job_dir, fname)
                if os.path.isfile(full) and not fname.endswith(('.part', '.ytdl', '.temp', '.jpg', '.jpeg', '.png', '.webp')):
                    logger.info("yt-dlp output found job=%s extension=%s", job_id, os.path.splitext(fname)[1])
                    return full

            raise FileNotFoundError("Downloaded file not found in job directory")

        except Exception as e:
            last_err = e
            logger.exception("yt-dlp download failed job=%s attempt=%s/%s url=%s", job_id, attempt, MAX_RETRIES, _url_for_log(url))
            if attempt < MAX_RETRIES:
                # Clean up partial files before retry
                for fname in os.listdir(job_dir):
                    try:
                        os.remove(os.path.join(job_dir, fname))
                    except Exception:
                        pass
                await asyncio.sleep(RETRY_DELAY * attempt)  # progressive backoff

    logger.error("yt-dlp download exhausted retries job=%s url=%s", job_id, _url_for_log(url))
    raise Exception(f"Download failed after {MAX_RETRIES} attempts: {last_err}")
