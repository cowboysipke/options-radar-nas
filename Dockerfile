# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

ARG TARGETARCH
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium \
    DATA_DIR=/data \
    CONFIG_PATH=/data/config.yaml \
    SETUP_HOST=0.0.0.0 \
    SETUP_PORT=8787

# Debian publishes Chromium for both amd64 and arm64. Using the distribution
# browser avoids architecture-specific Playwright browser bundles.
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium dumb-init fonts-noto-cjk fonts-noto-color-emoji \
      tesseract-ocr tesseract-ocr-chi-sim tzdata ca-certificates \
      libgl1 libglib2.0-0 libgomp1 libatomic1 libssl3 procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml requirements.txt requirements.nas.txt ./
COPY options_radar ./options_radar
RUN python -m pip install --no-cache-dir -r requirements.nas.txt \
    && python -m pip install --no-cache-dir --no-deps .

RUN mkdir -p /data /data/browser-profile /data/evidence /data/strategies /data/backups

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-m", "options_radar.healthcheck"]

ENTRYPOINT ["/usr/bin/dumb-init", "--"]
CMD ["python", "-m", "options_radar.nas_runtime"]
