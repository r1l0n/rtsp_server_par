"""Серверные сессии панели в Redis.

Cookie содержит только случайный идентификатор — никаких данных о пользователе
и никаких подписанных полезных нагрузок. Выход из системы и «завершить сессию»
действуют мгновенно, потому что состояние живёт на сервере.
"""

from __future__ import annotations

import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..config import get_settings
from ..redis_client import get_redis

SESSION_COOKIE = "rtspgw_sid"
_PREFIX = "sess:"
_USER_INDEX = "user_sess:"

_SID_BYTES = 32


@dataclass(slots=True)
class SessionData:
    sid: str
    user_id: str
    csrf: str
    created_at: float
    last_seen: float
    ip: str
    user_agent: str
    #: True между вводом пароля и подтверждением второго фактора.
    #: Такая сессия не даёт доступа ни к чему, кроме страницы ввода кода.
    pending_2fa: bool
    #: Пользователь поставил галочку «Запомнить меня». Хранится отдельно от
    #: дедлайна: пока ждём второй фактор, сессия остаётся короткой, а флаг
    #: переживает rotate() и превращается в дедлайн после ввода кода.
    remember: bool = False
    #: Абсолютный предел жизни долгой сессии: сколько её ни продлевай, дальше
    #: этого момента она не живёт. 0 — обычная сессия со скользящим окном.
    expires_at: float = 0.0

    @property
    def authenticated(self) -> bool:
        return not self.pending_2fa


def _key(sid: str) -> str:
    return f"{_PREFIX}{sid}"


def _index_key(user_id: str) -> str:
    return f"{_USER_INDEX}{user_id}"


def _remember_seconds() -> int:
    return get_settings().remember_me_days * 86400


def _expire_index(pipe: Any, user_id: str, ttl: int) -> None:
    """Держит индекс сессий пользователя живым дольше самой долгой из них.

    NX ставит срок только что созданному индексу, GT поднимает существующий,
    если новая сессия живёт дольше. Без GT короткая сессия обрезала бы индекс
    под себя, и профиль вместе с «завершить остальные» переставали бы видеть
    сессию с галочкой «Запомнить меня».
    """
    key = _index_key(user_id)
    pipe.expire(key, ttl, nx=True)
    pipe.expire(key, ttl, gt=True)


def ttl_for(session: SessionData) -> int:
    """Сколько сессии осталось жить, секунд.

    Обычная сессия получает скользящее окно целиком, долгая — только остаток
    до своего дедлайна.
    """
    if session.expires_at:
        return max(1, int(session.expires_at - time.time()))
    return get_settings().session_ttl_minutes * 60


async def create(
    user_id: uuid.UUID | str,
    *,
    ip: str = "",
    user_agent: str = "",
    pending_2fa: bool = False,
    remember: bool = False,
) -> SessionData:
    now = time.time()
    data = SessionData(
        sid=secrets.token_urlsafe(_SID_BYTES),
        user_id=str(user_id),
        csrf=secrets.token_urlsafe(32),
        created_at=now,
        last_seen=now,
        ip=ip,
        user_agent=user_agent[:400],
        pending_2fa=pending_2fa,
        remember=remember,
        # Дедлайн ставится только полноценной сессии: окно ввода кода TOTP не
        # должно жить месяц, даже если галочка была отмечена.
        expires_at=now + _remember_seconds() if remember and not pending_2fa else 0.0,
    )
    redis = get_redis()
    ttl = ttl_for(data)
    pipe = redis.pipeline()
    pipe.hset(
        _key(data.sid),
        mapping={
            "user_id": data.user_id,
            "csrf": data.csrf,
            "created_at": str(data.created_at),
            "last_seen": str(data.last_seen),
            "ip": data.ip,
            "user_agent": data.user_agent,
            "pending_2fa": "1" if data.pending_2fa else "0",
            "remember": "1" if data.remember else "0",
            "expires_at": str(data.expires_at),
        },
    )
    pipe.expire(_key(data.sid), ttl)
    pipe.sadd(_index_key(data.user_id), data.sid)
    _expire_index(pipe, data.user_id, ttl)
    await pipe.execute()
    return data


async def load(sid: str | None) -> SessionData | None:
    if not sid:
        return None
    return _from_hash(sid, await get_redis().hgetall(_key(sid)))


def _from_hash(sid: str, raw: dict[str, str]) -> SessionData | None:
    """Сессия из хеша Redis. Пустой хеш — сессии нет (истекла или снята)."""
    if not raw:
        return None
    return SessionData(
        sid=sid,
        user_id=raw.get("user_id", ""),
        csrf=raw.get("csrf", ""),
        created_at=float(raw.get("created_at", 0) or 0),
        last_seen=float(raw.get("last_seen", 0) or 0),
        ip=raw.get("ip", ""),
        user_agent=raw.get("user_agent", ""),
        pending_2fa=raw.get("pending_2fa") == "1",
        remember=raw.get("remember") == "1",
        expires_at=float(raw.get("expires_at", 0) or 0),
    )


async def touch(session: SessionData) -> None:
    """Продлевает скользящее окно жизни сессии — но не дальше её дедлайна."""
    ttl = ttl_for(session)
    redis = get_redis()
    pipe = redis.pipeline()
    pipe.hset(_key(session.sid), "last_seen", str(time.time()))
    pipe.expire(_key(session.sid), ttl)
    _expire_index(pipe, session.user_id, ttl)
    await pipe.execute()


async def delete(sid: str) -> None:
    """Снимает одну сессию.

    Сначала читает её, чтобы узнать владельца и вычистить его индекс.
    Массовое снятие (`delete_all_for_user`) сюда не ходит: там владелец
    известен заранее, и это чтение было бы лишним обменом на каждую сессию.
    """
    session = await load(sid)
    pipe = get_redis().pipeline()
    pipe.delete(_key(sid))
    if session is not None:
        pipe.srem(_index_key(session.user_id), sid)
    await pipe.execute()


async def rotate(session: SessionData, *, pending_2fa: bool | None = None) -> SessionData:
    """Выдаёт новый идентификатор сессии, сохраняя пользователя.

    Вызывается после входа и после подтверждения второго фактора: смена уровня
    привилегий не должна оставлять в силе старый идентификатор (session fixation).
    """
    fresh = await create(
        session.user_id,
        ip=session.ip,
        user_agent=session.user_agent,
        pending_2fa=session.pending_2fa if pending_2fa is None else pending_2fa,
        # Галочка «Запомнить меня» пережидает второй фактор здесь: до сих пор
        # это было только намерение, дедлайн появляется у новой сессии.
        remember=session.remember,
    )
    await delete(session.sid)
    return fresh


async def list_for_user(user_id: uuid.UUID | str) -> list[SessionData]:
    """Все живые сессии пользователя.

    Читаются одним конвейером, а не запросом на каждый идентификатор: страницу
    профиля открывает человек с десятком сессий, и это была дюжина
    последовательных обменов с Redis там, где хватает двух.
    """
    redis = get_redis()
    index = _index_key(str(user_id))
    sids = sorted(await redis.smembers(index))
    if not sids:
        return []

    pipe = redis.pipeline()
    for sid in sids:
        pipe.hgetall(_key(sid))
    rows = await pipe.execute()

    sessions: list[SessionData] = []
    stale: list[str] = []
    for sid, raw in zip(sids, rows, strict=True):
        session = _from_hash(sid, raw)
        if session is None:
            stale.append(sid)
        else:
            sessions.append(session)
    if stale:
        await redis.srem(index, *stale)
    return sorted(sessions, key=lambda s: s.created_at, reverse=True)


async def delete_all_for_user(user_id: uuid.UUID | str, *, except_sid: str | None = None) -> int:
    """Гасит сессии пользователя, кроме указанной. Возвращает число снятых."""
    owner = str(user_id)
    doomed = [s.sid for s in await list_for_user(owner) if s.sid != except_sid]
    if not doomed:
        return 0

    redis = get_redis()
    pipe = redis.pipeline()
    for sid in doomed:
        pipe.delete(_key(sid))
    pipe.srem(_index_key(owner), *doomed)
    await pipe.execute()
    return len(doomed)
