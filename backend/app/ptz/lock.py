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
from typing import Any

from ..redis_client import get_redis

_PREFIX = "ptz:lock:"

#: Захват или продление — одним шагом.
#:
#: Раньше продление шло как GET, а следом EXPIRE, и снятие — как GET, а следом
#: DELETE. Между двумя командами ключ успевает истечь по TTL и достаться
#: другому зрителю: первый вариант продлевал чужую блокировку, второй — снимал
#: её. Окно узкое, но оно ровно там, где идёт борьба за камеру.
#:
#: Возвращает {получилось, через сколько миллисекунд пробовать снова}.
_ACQUIRE = """
local ttl = tonumber(ARGV[2])
if redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ttl) then
    return {1, 0}
end
if redis.call('get', KEYS[1]) == ARGV[1] then
    redis.call('expire', KEYS[1], ttl)
    return {1, 0}
end
local remaining = redis.call('pttl', KEYS[1])
if remaining < 0 then
    remaining = 1000
end
return {0, remaining}
"""

#: Снятие блокировки, только если она всё ещё за этим владельцем.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

async def _run(source: str, key: str, *args: object) -> Any:
    """EVAL, а не register_script: объект скрипта запоминает клиента, которым
    его создали, а клиент здесь ленивый и в тестах подменяется. Пульт шлёт
    команду раз в 700 мс — лишние полсотни байт на запрос ничего не стоят."""
    return await get_redis().eval(source, 1, key, *args)


def panel_holder(user_id: uuid.UUID) -> str:
    return f"u:{user_id}"


def viewer_holder(viewer_id: str) -> str:
    return "v:" + hashlib.sha256(viewer_id.encode("utf-8")).hexdigest()[:16]


async def acquire(camera_id: uuid.UUID, holder: str, ttl_seconds: int) -> tuple[bool, int]:
    """Захватывает или продлевает управление.

    Возвращает (получилось, через сколько миллисекунд пробовать снова).
    Второе значение осмысленно только при отказе.
    """
    # Скрипт уже подставляет разумную паузу вместо отрицательного pttl,
    # а при успехе возвращает ровно 0 — здесь ничего не поправляем.
    ok, retry_ms = await _run(_ACQUIRE, f"{_PREFIX}{camera_id}", holder, ttl_seconds)
    return bool(ok), int(retry_ms)


async def release(camera_id: uuid.UUID, holder: str) -> None:
    """Отпускает управление, если оно всё ещё за этим владельцем."""
    await _run(_RELEASE, f"{_PREFIX}{camera_id}", holder)


async def owner(camera_id: uuid.UUID) -> str | None:
    value = await get_redis().get(f"{_PREFIX}{camera_id}")
    return str(value) if value else None
