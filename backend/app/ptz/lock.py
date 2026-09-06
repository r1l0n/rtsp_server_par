"""Блокировка «камерой управляет тот, кто нажал первым».

Без неё два зрителя одной ссылки дёргают камеру в разные стороны, и оба
видят, что «управление сломано»: камера дрожит на месте. Поэтому первый
нажавший получает камеру в единоличное пользование на `ptz_hold_seconds`,
а остальные видят неактивный пульт с объяснением.

Блокировка живёт на камеру, а не на ссылку: иначе оператор из панели и
зритель по ссылке спорили бы за одну и ту же камеру, каждый со своей
блокировкой.

Владелец обозначается непрозрачной строкой: для панели это `u:<id>`, для
публичной ссылки — `v:<хеш cookie>`. Сырой идентификатор зрителя в Redis не
кладём: этой cookie достаточно для просмотра камеры.
"""

from __future__ import annotations

import hashlib
import uuid

from ..redis_client import get_redis

_PREFIX = "ptz:lock:"


def panel_holder(user_id: uuid.UUID) -> str:
    return f"u:{user_id}"


def viewer_holder(viewer_id: str) -> str:
    return "v:" + hashlib.sha256(viewer_id.encode("utf-8")).hexdigest()[:16]


async def acquire(camera_id: uuid.UUID, holder: str, ttl_seconds: int) -> tuple[bool, int]:
    """Захватывает или продлевает управление.

    Возвращает (получилось, через сколько миллисекунд пробовать снова).
    Второе значение осмысленно только при отказе.
    """
    redis = get_redis()
    key = f"{_PREFIX}{camera_id}"

    if await redis.set(key, holder, nx=True, ex=ttl_seconds):
        return True, 0

    current = await redis.get(key)
    if current == holder:
        await redis.expire(key, ttl_seconds)
        return True, 0

    # Ключ мог истечь между SET и GET — тогда просто предложим повторить.
    remaining = await redis.ttl(key)
    return False, max(1, int(remaining)) * 1000 if remaining and remaining > 0 else 1000


async def release(camera_id: uuid.UUID, holder: str) -> None:
    """Отпускает управление, если оно всё ещё за этим владельцем."""
    redis = get_redis()
    key = f"{_PREFIX}{camera_id}"
    if await redis.get(key) == holder:
        await redis.delete(key)


async def owner(camera_id: uuid.UUID) -> str | None:
    value = await get_redis().get(f"{_PREFIX}{camera_id}")
    return str(value) if value else None
