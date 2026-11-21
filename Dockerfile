FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DD_CONFIG_DIR=/config DD_MUSIC_DIR=/music

# ffmpeg: ffprobe validation, transcoding, ReplayGain; curl: healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

# deno: JS runtime yt-dlp uses to solve YouTube challenges
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt "yt-dlp[default]"

COPY dailydig ./dailydig

VOLUME ["/config", "/music"]
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:${DD_PORT:-8090}/api/health || exit 1

CMD ["python", "-m", "dailydig"]
