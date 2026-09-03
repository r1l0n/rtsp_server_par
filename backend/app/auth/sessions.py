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

    @property
    def authenticated(self) -> bool:
        return not self.pending_2fa


def _key(sid: str) -> str:
    return f"{_PREFIX}{sid}"


def _index_key(user_id: str) -> str:
    return f"{_USER_INDEX}{user_id}"


def _ttl_seconds() -> int:
    return get_settings().session_ttl_minutes * 60


async def create(
    user_id: uuid.UUID | str,
    *,
    ip: str = "",
    user_agent: str = "",
    pending_2fa: bool = False,
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
    )
    redis = get_redis()
    ttl = _ttl_seconds()
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
        },
    )
    pipe.expire(_key(data.sid), ttl)
    pipe.sadd(_index_key(data.user_id), data.sid)
    pipe.expire(_index_key(data.user_id), ttl)
    await pipe.execute()
    return data


async def load(sid: str | None) -> SessionData | None:
    if not sid:
        return None
    raw = await get_redis().hgetall(_key(sid))
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
    )


async def touch(sid: str) -> None:
    """Продлевает скользящее окно жизни сессии."""
    redis = get_redis()
    pipe = redis.pipeline()
    pipe.hset(_key(sid), "last_seen", str(time.time()))
    pipe.expire(_key(sid), _ttl_seconds())
    await pipe.execute()


async def delete(sid: str) -> None:
    session = await load(sid)
    redis = get_redis()
    pipe = redis.pipeline()
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
    )
    await delete(session.sid)
    return fresh


async def list_for_user(user_id: uuid.UUID | str) -> list[SessionData]:
    redis = get_redis()
    index = _index_key(str(user_id))
    sids = await redis.smembers(index)
    sessions: list[SessionData] = []
    stale: list[str] = []
    for sid in sids:
        session = await load(sid)
        if session is None:
            stale.append(sid)
        else:
            sessions.append(session)
    if stale:
        await redis.srem(index, *stale)
    return sorted(sessions, key=lambda s: s.created_at, reverse=True)


async def delete_all_for_user(user_id: uuid.UUID | str, *, except_sid: str | None = None) -> int:
    removed = 0
    for session in await list_for_user(user_id):
        if session.sid == except_sid:
            continue
        await delete(session.sid)
        removed += 1
    return removed
