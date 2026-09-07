"""Общая настройка тестов.

Переменные окружения выставляются до первого импорта app.config: настройки
кэшируются lru_cache, и подменить их потом уже нельзя.
"""

from __future__ import annotations

import base64
import os

os.environ.setdefault("APP_SECRET_KEY", base64.b64encode(b"\x01" * 32).decode())
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("DOMAIN", "cam.test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

# Адреса MediaMTX — обязательно на 127.0.0.1, а не на имя `mediamtx` из
# docker-сети. Вне контейнера такого имени нет, и Windows выясняет это не
# мгновенно: перебор DNS, LLMNR и NetBIOS занимает больше десяти секунд на
# каждый запрос. Тесты при этом проходят, просто прогон растягивается с
# нескольких секунд до полутора минут. Порты заведомо никем не заняты —
# соединение отвергается сразу, а это ровно то, что проверкам и нужно.
os.environ.setdefault("MTX_API_URL", "http://127.0.0.1:59997")
os.environ.setdefault("MTX_HLS_URL", "http://127.0.0.1:58888")
os.environ.setdefault("MTX_WEBRTC_URL", "http://127.0.0.1:58889")

import fakeredis.aioredis
import pytest

from app import redis_client


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch):
    """Подменяет общий клиент Redis на in-memory реализацию."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "_client", client)
    yield client
    monkeypatch.setattr(redis_client, "_client", None)
