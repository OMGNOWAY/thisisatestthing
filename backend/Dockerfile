FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg for yt-dlp
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp needs a JS runtime to solve YouTube's challenges
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
