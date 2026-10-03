<div align="center">

# Universal Downloader

</div>


<!-- README polish: repository metadata badges -->
<p>
  <a href="https://github.com/vishnuskandha/Universal-Downloader"><img alt="GitHub stars" src="https://img.shields.io/github/stars/vishnuskandha/Universal-Downloader?style=for-the-badge&logo=github&label=Stars"></a>
  <a href="https://github.com/vishnuskandha/Universal-Downloader/fork"><img alt="GitHub forks" src="https://img.shields.io/github/forks/vishnuskandha/Universal-Downloader?style=for-the-badge&logo=github&label=Forks"></a>
  <a href="https://github.com/vishnuskandha/Universal-Downloader/issues"><img alt="GitHub issues" src="https://img.shields.io/github/issues/vishnuskandha/Universal-Downloader?style=for-the-badge&logo=github&label=Issues"></a>
  <a href="https://github.com/vishnuskandha/Universal-Downloader/commits"><img alt="Last commit" src="https://img.shields.io/github/last-commit/vishnuskandha/Universal-Downloader?style=for-the-badge&logo=git&label=Updated"></a>
</p>
<!-- End README polish -->

[![CI](https://github.com/vishnuskandha/Universal-Downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/vishnuskandha/Universal-Downloader/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A modern video and audio downloader for **YouTube**, **Facebook**, and
**Instagram** - including videos, shorts, reels, and MP3 audio extraction - with
a terminal-themed glassmorphism UI.

- **Frontend**: React, Vite, Tailwind CSS v4, Framer Motion, WebGL matrix background
- **Backend**: Python, FastAPI, yt-dlp, FFmpeg

Built by [Vishnu Skandha](https://github.com/vishnuskandha).

## Features

- Multi-platform support: YouTube (videos, shorts, up to 4K), Facebook
  (videos, reels), Instagram (reels, posts)
- Smart format selection with H.264/AAC compatibility (H.264 video + AAC audio,
  merged to MP4)
- MP3 audio extraction at up to 320kbps
- Single and batch download modes (batch uses a 3-worker concurrency pool with
  auto-retry)
- Auto platform detection and playlist protection (`noplaylist`)
- File size estimation before download
- 3 retry attempts with progressive backoff
- Rate limiting (60 requests/minute/IP) and request timeouts
- Optional auto-send of downloaded videos to a Telegram chat
- Responsive UI usable from mobile devices on the local network

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 18+
- FFmpeg available on `PATH` (or bundled in `backend/ffmpeg-master-latest-win64-gpl/bin/`)

### One-Click Start (Windows)

```bat
start_dev.bat
```

This installs dependencies on first run and launches the backend (port 8000)
and frontend (port 5173) in separate windows.

### Manual Setup

Backend:

```bash
cd backend
python -m venv venv
venv\Scripts\activate        # Windows: use source venv/bin/activate on macOS/Linux
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. API docs are available at http://localhost:8000/docs.

### Docker

```bash
docker compose up --build
```

Backend runs on port 8000 and frontend on port 5173.

### Telegram Auto-Send (Optional)

Set these environment variables (or in `backend/.env`) to send downloads to
Telegram:

```
TELEGRAM_BOT_TOKEN=<bot token from BotFather>
TELEGRAM_CHAT_ID=<chat, group, or channel ID>
```

### YouTube Authentication (Optional)

This service never submits a Google password, TOTP code, CAPTCHA response, or
challenge response. Google OAuth is useful for documented YouTube Data API
operations, but it does **not** supply the browser session cookies needed by
yt-dlp. For authenticated downloads, authenticate once in a normal interactive
browser session and reuse its exported Netscape-format `cookies.txt`.

On Linux, create storage that is outside the checkout and readable only by the
service account:

```bash
sudo install -d -o myapp -g myapp -m 0700 /var/lib/myapp/youtube/auth
export YOUTUBE_AUTH_DIR=/var/lib/myapp/youtube/auth
export YOUTUBE_COOKIE_FILE=/var/lib/myapp/youtube/auth/cookies.txt
export ADMIN_TOKEN='<long random secret>'
```

Use an administrator-controlled VNC/remote desktop/SSH desktop session and a
dedicated Chrome profile; never use a personal profile. The optional admin API
starts ordinary Chrome with that profile (it does not automate Google):

```bash
curl -X POST http://server:8000/admin/youtube/auth/start \
  -H "X-Admin-Token: $ADMIN_TOKEN"
```

Complete Google sign-in yourself in the visible browser, export YouTube cookies
to Netscape format, then import them once:

```bash
curl -X POST http://server:8000/api/cookies \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @cookies-payload.json
```

`cookies-payload.json` contains `{ "cookies": "...Netscape cookie text..." }`.
The server stores this at `YOUTUBE_COOKIE_FILE` with mode `0600`; directories
are mode `0700`. Do not commit, log, or share it. Check safe setup status with
`GET /api/cookies/status`. The same persistent cookie file is copied read-only
for every yt-dlp job so concurrent jobs cannot modify it. If YouTube rejects
the session, the API reports “YouTube authentication has expired.
Re-authentication is required.” It does not retry Google login automatically.

Migration from the old `GOOGLE_EMAIL`, `GOOGLE_PASSWORD`, and
`GOOGLE_TOTP_SECRET` workflow: remove those variables, move/export the valid
cookie file to the path above, and import it once through the protected admin
endpoint. Existing `YOUTUBE_COOKIES_PATH` is accepted as a compatibility alias.

## CLI Usage

```bash
# Download a single URL
python cli_download.py "https://youtube.com/watch?v=..."

# Batch download from a file (one URL per line, # for comments)
python batch_download.py urls.txt

# Batch download from stdin
echo -e "url1\nurl2" | python batch_download.py -
```

## API Endpoints

### `POST /api/analyze`

Analyze a URL and return available formats.

```json
{
  "url": "https://youtube.com/watch?v=..."
}
```

Response includes platform, title, thumbnail, duration, and a list of formats
with `formatId`, extension, resolution, fps, and estimated file size.

### `POST /api/download`

Download media in the selected format.

```json
{
  "url": "https://youtube.com/watch?v=...",
  "formatId": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/best",
  "title": "Optional friendly filename"
}
```

Returns the binary file (MP4/MP3) with a `Content-Disposition` header.

### Queued downloads

For clients that need queue updates before the file is ready, create a job with
`POST /api/download/jobs`. It returns `202 Accepted` and a JSON status object.
When a job is waiting, `queuePosition` is its one-based position among the
waiting jobs. No time estimate is returned. Poll `GET /api/download/jobs/{id}`
until its status is `ready`, then download the binary from the returned
`downloadUrl`. Statuses are `queued`, `downloading`, `ready`, and `failed`.

```js
const apiUrl = "http://localhost:8000";

const created = await fetch(`${apiUrl}/api/download/jobs`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    url: "https://youtube.com/watch?v=...",
    formatId: "bestvideo+bestaudio/best",
    title: "My video",
  }),
});

let job = await created.json();
while (job.status === "queued" || job.status === "downloading") {
  if (job.status === "queued") {
    console.log(`You are number ${job.queuePosition} in the download queue.`);
  } else {
    console.log("Your download is being prepared.");
  }
  await new Promise((resolve) => setTimeout(resolve, 1000));
  job = await fetch(`${apiUrl}/api/download/jobs/${job.id}`).then((r) => r.json());
}

if (job.status === "failed") throw new Error(job.error);
const file = await fetch(`${apiUrl}${job.downloadUrl}`).then((r) => r.blob());
// Use `file`, e.g. URL.createObjectURL(file), to save or play it in the browser.
```

### Security

- Only `http`/`https` URLs are accepted, and hosts resolving to private,
  loopback, or link-local addresses are rejected to prevent SSRF abuse.
- No secrets are stored in the repository; the Telegram bot token must be
  provided via environment variables.
- TLS certificate verification is enabled. To restore the legacy bypass
  (needed on networks with custom MITM proxies), set
  `YTDLP_NO_CHECK_CERTIFICATE=1`.

## Project Structure

```
Universal-Downloader/
├── backend/
│   ├── main.py              # FastAPI app, endpoints, rate limiting, SSRF protection
│   ├── downloader.py        # yt-dlp wrapper: analyze, download, retry, FFmpeg control
│   ├── telegram_sender.py   # Optional Telegram video auto-send
│   ├── requirements.txt
│   ├── Dockerfile
│   └── render.yaml          # Render deployment config
├── frontend/
│   ├── src/
│   │   ├── App.jsx          # Main application component
│   │   ├── api.js           # API client (analyze + download + blob error parsing)
│   │   └── components/      # FaultyTerminal, BatchMode, ProfileCard, SpotlightCard
│   ├── package.json
│   ├── vite.config.js
│   └── Dockerfile
├── cli_download.py          # CLI single-URL downloader
├── batch_download.py        # CLI batch downloader
├── start_dev.bat            # Windows one-click startup
├── docker-compose.yml
└── urls.txt                 # Sample URL list for batch mode
```

## License

[MIT](LICENSE) - Copyright (c) 2026 Vishnu Skandha

Download content only where you have the right to do so. Respect the Terms of
Service of every platform.
