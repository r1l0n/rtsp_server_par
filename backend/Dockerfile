# syntax=docker/dockerfile:1

# ── Сборка зависимостей в отдельный слой ─────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libffi-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml ./
# Ставим только зависимости: код копируется в финальный образ отдельно,
# чтобы правка исходников не инвалидировала кэш слоя с пакетами.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && mkdir -p app && touch app/__init__.py \
 && /opt/venv/bin/pip install .

# ── Финальный образ ──────────────────────────────────────────────────────────
FROM python:3.12-slim

# ffmpeg/ffprobe нужны worker'у: проба кодеков камеры и снапшоты-превью.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --create-home --home-dir /home/app app \
 && mkdir -p /var/lib/rtspgw/snapshots \
 && chown -R app:app /var/lib/rtspgw

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app

USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
