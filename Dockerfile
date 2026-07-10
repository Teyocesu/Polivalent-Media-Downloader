FROM node:24-bookworm-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp's YouTube EJS solver requires Node 22+. Copy the known-compatible
# runtime from the frontend stage instead of Debian's older nodejs package.
COPY --from=frontend-build /usr/local/bin/node /usr/local/bin/node
RUN node --version \
    && ffmpeg -version >/dev/null \
    && ffprobe -version >/dev/null

WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade --force-reinstall pip==26.1.2 \
    && python -m pip install --no-cache-dir -r backend/requirements.txt \
    && test -s /usr/local/bin/pip \
    && python -c "import pip; assert pip.__version__ == '26.1.2'" \
    && python -m pip check

COPY backend ./backend
COPY --from=frontend-build /app/frontend/dist ./backend/static

# Render secret files are readable by group 1000. Running as this unprivileged
# user keeps the app non-root while preserving runtime access to Secret Files.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home app \
    && mkdir -p /tmp/media-downloads \
    && chown -R app:app /tmp/media-downloads \
    && chmod 0700 /tmp/media-downloads

WORKDIR /app/backend
USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health', timeout=3)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
