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
from importlib.metadata import PackageNotFoundError, version as package_version
from yt_dlp.postprocessor.common import PostProcessor

import google_login
import git_auth_sync

logger = logging.getLogger("video_downloader.downloader")


def log_downloader_runtime() -> None:
    """Log safe runtime diagnostics without printing cookies or signed URLs."""
    try:
        yt_version = package_version("yt-dlp")
    except PackageNotFoundError:
        yt_version = getattr(yt_dlp.version, "__version__", "unknown")
    try:
        ejs_version = package_version("yt-dlp-ejs")
    except PackageNotFoundError:
        ejs_version = "missing"
    logger.info(
        "YouTube runtime yt_dlp=%s yt_dlp_ejs=%s deno_installed=%s "
        "ffmpeg_installed=%s player_client_override=%s",
        yt_version,
        ejs_version,
        bool(shutil.which("deno")),
        HAS_FFMPEG,
        ",".join(_PLAYER_CLIENTS) if _PLAYER_CLIENTS else "none",
    )
    if ejs_version == "missing":
        logger.warning(
            "yt-dlp-ejs is not installed; the current YouTube player "
            "JavaScript challenges may fail. Rebuild yt-dlp[default]."
        )
    if not shutil.which("deno"):
        logger.warning(
            "Deno is not available; yt-dlp may be unable to solve "
            "YouTube JavaScript challenges."
        )



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

# Only 1 concurrent yt-dlp+FFmpeg process at a time (512MB RAM hosting).
# threading.Semaphore because _run() executes inside asyncio.to_thread().
_DL_SEM = threading.Semaphore(1)

# TLS certificate verification is kept enabled by default; the legacy bypass
# (some corporate networks use MITM proxies with custom CAs) can be re-enabled
# by setting YTDLP_NO_CHECK_CERTIFICATE=1.
_NO_CHECK_CERT = os.getenv("YTDLP_NO_CHECK_CERTIFICATE", "").strip().lower() in ("1", "true", "yes")

# Optional: override yt-dlp's YouTube player clients, comma separated
# (e.g. YTDLP_PLAYER_CLIENTS=default,web_embedded). Leave unset for yt-dlp's defaults.
_PLAYER_CLIENTS = [c.strip() for c in os.getenv("YTDLP_PLAYER_CLIENTS", "").split(",") if c.strip()]



def _cooldown_seconds_config():
    try:
        return max(60, min(int(os.getenv("YOUTUBE_429_COOLDOWN_SECONDS", "900")), 3600))
    except ValueError:
        return 900


_YT_COOLDOWN_SECONDS = _cooldown_seconds_config()
_YT_COOLDOWN_LOCK = threading.Lock()
_YT_COOLDOWN_UNTIL = 0.0


class YouTubeRateLimited(RuntimeError):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f"YouTube temporarily rate-limited this server. Try again in {retry_after} seconds.")


def youtube_retry_after():
    with _YT_COOLDOWN_LOCK:
        remaining = _YT_COOLDOWN_UNTIL - time.monotonic()
    return max(0, int(remaining + 0.999))


def _record_youtube_429(message):
    if "HTTP Error 429" not in message and "429: Too Many Requests" not in message:
        return
    global _YT_COOLDOWN_UNTIL
    with _YT_COOLDOWN_LOCK:
        _YT_COOLDOWN_UNTIL = max(_YT_COOLDOWN_UNTIL, time.monotonic() + _YT_COOLDOWN_SECONDS)
    logger.warning("YouTube HTTP 429: pausing requests for %ss", _YT_COOLDOWN_SECONDS)


def _check_youtube_cooldown(platform):
    if platform == 'youtube':
        seconds = youtube_retry_after()
        if seconds:
            raise YouTubeRateLimited(seconds)


class _YtDlpLog:
    """Observe yt-dlp HTTP warnings without logging signed media URLs."""
    def debug(self, message):
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("%s", message)

    def warning(self, message):
        _record_youtube_429(message)
        logger.warning("yt-dlp: %s", re.sub(r'https?://\S+', '[url]', message))

    def error(self, message):
        logger.error("yt-dlp: %s", re.sub(r'https?://\S+', '[url]', message))


def _base_opts() -> dict:
    """Return a fresh copy of base yt-dlp options (never mutate the global)."""
    opts = {
        'quiet': True,
        # Important for diagnosing extractor failures: missing JavaScript
        # challenge scripts and rejected player clients are warning-only.
        'no_warnings': False,
        'logger': _YtDlpLog(),
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


def _set_cookie_options(opts: dict, platform: str, cookie_tmp: str | None) -> None:
    """Use supported player clients for logged-in YouTube sessions."""
    if not cookie_tmp:
        return
    opts['cookiefile'] = cookie_tmp
    if platform == 'youtube' and not _PLAYER_CLIENTS:
        # yt-dlp issue #17389: the default authenticated tv_downgraded
        # client can fail to return playable player responses.
        opts['extractor_args'] = {
            'youtube': {'player_client': ['default', 'web_embedded']}
        }


def _extract_video_info(url: str, platform: str) -> tuple[dict | None, bool]:
    """Try stored cookies first, then guest access for public YouTube videos.

    A loaded cookie file is not evidence that YouTube will accept that session.
    Only retry an anonymous extraction when the authenticated player did not
    return any player response; never silently retry login-required content.
    """
    _check_youtube_cooldown(platform)
    with _cookie_copy(platform) as cookie_tmp:
        opts = _base_opts()
        _set_cookie_options(opts, platform, cookie_tmp)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False), bool(cookie_tmp)
        except Exception as exc:
            _record_youtube_429(str(exc))
            _check_youtube_cooldown(platform)
            if not (
                platform == 'youtube'
                and cookie_tmp
                and 'Failed to extract any player response' in str(exc)
            ):
                raise
            logger.warning(
                "Authenticated YouTube player returned no response; "
                "retrying public metadata without account cookies"
            )

    _check_youtube_cooldown(platform)
    with yt_dlp.YoutubeDL(_base_opts()) as ydl:
        return ydl.extract_info(url, download=False), False


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
        # The file is produced by re-encoding MP4 audio to 320 kbps; source
        # audio size is not a useful estimate of the resulting MP3 size.
        "filesize": int((info.get('duration') or 0) * 320_000 / 8) or None,
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

    try:
        info, using_cookies = _extract_video_info(url, platform)
        if not info:
            return None
        formats = _build_formats(info, label_map[platform], is_short=is_short)
        logger.info(
            "yt-dlp analysis completed platform=%s formats=%s used_cookies=%s",
            platform, len(formats), using_cookies,
        )
        return {
            "platform": platform,
            "contentType": content_type,
            "title": info.get('title'),
            "thumbnailUrl": info.get('thumbnail'),
            "durationSeconds": info.get('duration'),
            "formats": formats,
        }
    except YouTubeRateLimited:
        raise
    except Exception as e:
        _record_youtube_429(str(e))
        _check_youtube_cooldown(platform)
        logger.exception("yt-dlp analysis failed platform=%s url=%s", platform, _url_for_log(url))
        raise Exception(f"Failed to analyze URL: {e}") from e


# ─── Download ─────────────────────────────────────────────────────────

MAX_RETRIES = 3
RETRY_DELAY = 3   # seconds between retries

# MP3 requests intentionally download the *video* in MP4 first.
# Try a small progressive MP4 to minimize RAM/disk/network overhead.
# If that is not offered, download separate MP4 video + M4A audio and
# merge them to MP4 before extracting the MP3.
MP3_MP4_SOURCES = (
    "best[ext=mp4][height<=360][vcodec!=none][acodec!=none]/best[ext=mp4][vcodec!=none][acodec!=none]",
    "bestvideo[ext=mp4][height<=360][vcodec^=avc1]+bestaudio[ext=m4a]",
    "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]",
)

async def download_video(url: str, format_id: str, output_dir: str):
    _check_youtube_cooldown(detect_platform(url)["platform"])
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
        if not HAS_FFMPEG:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise RuntimeError("FFmpeg is required to extract MP3 from MP4 video.")
        opts.update({
            'format': MP3_MP4_SOURCES[0],
            'merge_output_format': 'mp4',
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

    # Let yt-dlp resolve MP4 format selectors on the current request. This
    # avoids a separate YouTube metadata probe for every MP3 download.
    def _download_once(selected_format, attempt):
        logger.info("yt-dlp download waiting for worker slot job=%s attempt=%s", job_id, attempt)
        with _DL_SEM:
            with _cookie_copy(platform) as cookie_tmp:
                run_opts = dict(opts)
                if selected_format:
                    run_opts['format'] = selected_format
                _set_cookie_options(run_opts, platform, cookie_tmp)
                with yt_dlp.YoutubeDL(run_opts) as ydl:
                    if is_audio_only and HAS_FFMPEG:
                        ydl.add_post_processor(_CleanTagsPP(), when='pre_process')
                    ydl.download([url])

    last_err = None
    saw_forbidden = False
    # Each alternative produces an MP4 container before FFmpeg extracts MP3.
    # For ordinary video downloads, retain the existing retry behavior.
    attempts = MP3_MP4_SOURCES if is_audio_only else (None,) * MAX_RETRIES
    for attempt, selected_format in enumerate(attempts, start=1):
        if is_audio_only:
            logger.info(
                "MP3 from MP4 selected job=%s attempt=%s source=%s",
                job_id, attempt,
                "progressive MP4" if attempt == 1 else "MP4 video + M4A merge",
            )
        try:
            await asyncio.to_thread(_download_once, selected_format, attempt)
            # After postprocessing, only serve the requested final file.
            # Never accidentally return a leftover MP4 instead of an MP3.
            if is_audio_only:
                filepath = os.path.join(job_dir, f"{job_id}.mp3")
                if os.path.isfile(filepath):
                    logger.info("MP4 to MP3 conversion completed job=%s", job_id)
                    return filepath
                raise FileNotFoundError("MP4 downloaded, but MP3 extraction did not finish")
            for fname in os.listdir(job_dir):
                full = os.path.join(job_dir, fname)
                if (os.path.isfile(full)
                        and not fname.endswith(('.part', '.ytdl', '.temp', '.jpg', '.jpeg', '.png', '.webp'))):
                    logger.info("yt-dlp completed job=%s extension=%s", job_id, os.path.splitext(fname)[1])
                    return full
            raise FileNotFoundError("Downloaded file not found in job directory")
        except Exception as exc:
            last_err = exc
            message = str(exc)
            _record_youtube_429(message)
            if platform == 'youtube' and youtube_retry_after():
                logger.warning('Stopping download retries due to YouTube HTTP 429')
                break
            forbidden = "HTTP Error 403" in message or "403: Forbidden" in message
            saw_forbidden = saw_forbidden or forbidden
            unavailable = "Requested format is not available" in message
            logger.warning(
                "Download attempt failed job=%s attempt=%s format=%s reason=%s",
                job_id, attempt, "mp4-video" if is_audio_only else "default",
                "HTTP 403" if forbidden else "format unavailable" if unavailable else type(exc).__name__,
            )
            # Remove partial files before attempting an alternative.
            for fname in os.listdir(job_dir):
                try:
                    os.remove(os.path.join(job_dir, fname))
                except OSError:
                    pass
            if platform == 'youtube' and is_audio_only:
                # Try another MP4 source only if the previous stream is
                # rejected or missing. Do not repeatedly retry other errors.
                if not (forbidden or unavailable):
                    break
                continue
            if forbidden or unavailable:
                break
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)

    shutil.rmtree(job_dir, ignore_errors=True)
    _check_youtube_cooldown(platform)
    if saw_forbidden:
        raise RuntimeError(
            "YouTube refused the selected MP4 video/audio stream(s) (HTTP 403). "
            "This is an upstream access restriction, not an MP3 conversion problem."
        ) from last_err
    if platform == 'youtube' and is_audio_only and last_err and (
        "Requested format is not available" in str(last_err)
    ):
        raise RuntimeError(
            "YouTube did not offer a compatible MP4 source for MP3 conversion."
        ) from last_err
    raise RuntimeError(f"Download failed: {last_err}") from last_err
