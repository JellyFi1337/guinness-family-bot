# syntax=docker/dockerfile:1
# Guinness Bot — runtime and test images.
# Base digest pinned (do not float on :3.12-slim alone).
ARG PYTHON_BASE=python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

FROM ${PYTHON_BASE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    BOT_CONFIG_PATH=/app/bot_config.toml \
    DB_PATH=/data/guinness_bot.db \
    SHOWDOWN_STATE_PATH=/data/showdown_state.json \
    LOG_DIR=/data/logs

# ffmpeg: yt-dlp / music. ca-certificates + tzdata: TLS and Europe/Moscow.
# libopus0: Discord voice (PyNaCl / davey wheels cover sodium; opus is system).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tzdata \
        libopus0 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --user-group --shell /usr/sbin/nologin bot \
    && mkdir -p /data/logs \
    && chown -R bot:bot /data

WORKDIR /app

# ----- dependency layers (no app source yet; better layer cache) -----

FROM base AS runtime-deps
COPY requirements.lock ./
RUN python -m pip install --require-hashes -r requirements.lock

FROM base AS test-deps
COPY requirements-dev.lock ./
RUN python -m pip install --require-hashes -r requirements-dev.lock

# ----- runtime: production bot process -----

FROM runtime-deps AS runtime
# Application code as root-owned, not writable by the bot user.
COPY . /app
RUN chmod -R a-w /app

USER bot
VOLUME ["/data"]
# Secrets via TOKEN_FILE / MAJESTIC_API_KEY_FILE / DATABASE_*_FILE at runtime —
# never as build args. Public TOML via BOT_CONFIG_PATH or /app/bot_config.toml.
CMD ["python", "main.py"]

# ----- test: unit suite without Discord token or Discord network -----

FROM test-deps AS test
COPY . /app
RUN chmod -R a-w /app

USER bot
# tests/__init__.py pins SQLite; no TOKEN required.
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."]
