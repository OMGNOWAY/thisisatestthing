# Vercel backend (experimental)

A separate FastAPI backend for Vercel. The existing `v2/backend` Render deployment is **not changed**.

## Vercel project setup

1. Go to [Vercel New Project](https://vercel.com/new) and import `OMGNOWAY/thisisatestthing`.
2. Set **Root Directory** to **`v2/vercel-backend`**.
3. Let Vercel detect FastAPI / Python. Leave output directory blank.
4. Enable **Fluid Compute**. `vercel.json` sets a 300-second maximum duration for `app.py` and runs `python build.py` during build.
5. Deploy. Test `https://<your-project>.vercel.app/api/health` and `/docs`.
6. In your **frontend** Vercel project, set `VITE_API_URL=https://<your-project>.vercel.app` and redeploy the frontend. The checked-in `frontend/.env.production` still points to Render; change its value if you build directly from that file.

## What it supports

- `POST /api/analyze` — same URL/title/formats response fields as the React UI expects.
- `POST /api/download` — MP3 requests download an MP4 video stream, then FFmpeg encodes 320 kbps MP3 with title/artist/album and optional cover art.
- Normal MP4 requests download a video.
- File responses are **streamed**, avoiding Vercel's normal buffered response-size limit.
- `GET /api/cookies/status`, `POST /api/cookies`, `DELETE /api/cookies` when encrypted Git-backed storage is configured.

**Unlike Render, this is a request-based function.** It does not run an always-on queue, background jobs, Playwright or the interactive browser-login endpoint. It uses `/tmp` for transient files and removes them after streaming; you cannot rely on `/tmp` persisting across invocations or instances.

## Concurrent downloads

The backend accepts **up to 2 simultaneous MP4 / MP3 download requests per warm
Vercel function instance**. The analysis endpoint has a separate
single-analysis lock. The limit also counts response streaming, so temporary
files stay reserved until delivered.

- Default: `MAX_CONCURRENT_DOWNLOADS=2` (no environment variable required).
- For smaller loads, set `MAX_CONCURRENT_DOWNLOADS=1`. Values above 2 are capped at 2 in this deployment because Hobby has 1 vCPU and 500 MB of `/tmp` scratch storage.
- If both local slots are busy, `POST /api/download` returns HTTP **503** and a **`Retry-After: 10`** header. Clients should retry later instead of continuously resubmitting requests.
- You can see the configured limit in `GET /api/health`, under `maxConcurrentDownloadsPerInstance`.
- Vercel **automatically starts additional function instances** as needed. This limit does **not** cap downloads across the entire deployment, nor is it a server-side global rate limit. A database-backed distributed queue or limiter would be needed for a hard global limit.
- MP4 source files are limited to 120 MiB each, and requests are rejected when `/tmp` has insufficient available space. Source size checks are estimates and cannot guarantee every conversion will fit.

More concurrent conversions consume extra CPU, temporary disk space and bandwidth.
They may also increase YouTube 429/403 rate-limiting; start small and monitor.

## Credentials

Set these in **Vercel → Project → Settings → Environment Variables**:

| Variable | Purpose |
| --- | --- |
| `FRONTEND_URL` | Your frontend's exact origin, e.g. `https://your-frontend.vercel.app` (defaults to `*` for testing) |
| `ADMIN_TOKEN` | Required to upload/delete cookies through the existing React cookie modal |
| `YOUTUBE_AUTH_GIT_REPO` | Optional encrypted cookie repo, `owner/repo` |
| `YOUTUBE_AUTH_GIT_TOKEN` | Optional GitHub token with Contents read/write permission |
| `YOUTUBE_AUTH_ENCRYPTION_KEY` | Fernet key used to encrypt the Git cookie payload (must match Render if sharing the store) |
| `YOUTUBE_AUTH_GIT_BRANCH` | Optional branch; defaults to `main` |
| `YOUTUBE_AUTH_GIT_PATH` | Optional path; defaults to `youtube-auth/cookies.enc` |
| `YOUTUBE_COOKIES_B64` | Alternative to Git sync: base64 of Netscape `cookies.txt` text. Read-only from the API. |

For cookie uploads to survive new Vercel instances, use the encrypted Git settings above. If you reuse the Render encryption key, token, repo and path, both deployments will use the same encrypted cookie file. If the `YOUTUBE_COOKIES_B64` fallback is used, update that environment variable yourself to change the cookies; API uploads require Git sync.

**Never commit unencrypted cookies, Git tokens or your encryption key.**

## How the binaries get installed

Vercel doesn't use the Render Dockerfile:

- `imageio-ffmpeg` supplies a bundled FFmpeg executable for media conversion.
- `build.py` downloads a pinned, SHA256-verified **Deno** Linux binary from official Deno GitHub releases during the Vercel build. Deno is bundled at `vendor/deno` for yt-dlp's JavaScript challenges.
- `yt-dlp[default]` brings in `yt-dlp-ejs`.

If the build fails fetching Deno, review the build logs. There is **no automatic fallback to an unverified binary**.

## Limits and troubleshooting

- Hobby functions have a 5-minute execution cap with Fluid Compute. Long video downloads or conversion can time out.
- Temporary disk space is finite; individual source files are restricted to 120 MiB (an approximate yt-dlp source-size limit, not a guaranteed total-disk cap). Two concurrent conversions may still exhaust the 500 MiB scratch space when merging separate video/audio tracks.
- Vercel can also receive YouTube `HTTP 403` and `HTTP 429`; changing hosting providers does **not** guarantee that these errors disappear.
- This folder uses direct API calls. `/api/download/jobs` (Render's persistent queue) is intentionally not implemented.
- Test single short videos and a small MP3 before switching production traffic.
- The Vercel function runtime and deployment have not yet been verified against the live YouTube service.

See [Vercel FastAPI](https://vercel.com/docs/frameworks/backend/fastapi), [Vercel function limits](https://vercel.com/docs/functions/limitations) and [yt-dlp EJS setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS).
