"""Проверка прав на медиа-запрос — эндпоинт для Caddy forward_auth.

Как это работает целиком:

1. Зритель открывает /v/<slug>?t=<token>. Страница просмотра проверяет токен по
   БД и выдаёт cookie со случайным viewer_id, а в Redis запоминает, какие пути
   MediaMTX этому зрителю разрешены (HASH viewer:<vid>: mtx_path -> link_id).
2. Дальше браузер ходит за медиа на /whep/... и /hls/.... Caddy на каждый такой
   запрос спрашивает у нас /internal/authz. Токена в этих URL уже нет — только
   cookie, поэтому токен не течёт в логи прокси и в Referer.
3. Решение по ссылке кэшируется в Redis на authz_cache_seconds: LL-HLS дёргает
   сегменты по нескольку раз в секунду, и ходить в БД на каждый запрос нельзя.
   Отзыв ссылки удаляет ключ кэша, поэтому задержка отзыва — нулевая.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import secrets
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from ..config import get_settings
from ..db import get_sessionmaker
from ..logging_setup import get_logger
from ..models import Camera, ShareLink
from ..redis_client import get_redis

log = get_logger("authz")
router = APIRouter(tags=["internal"])

VIEW_COOKIE = "rtspgw_view"

#: Значение вместо id ссылки, когда камеру смотрит оператор из панели.
OPERATOR_GRANT = "operator"
_VIEWER_PREFIX = "viewer:"
_LINK_CACHE_PREFIX = "authz:link:"
_LINK_VIEWERS_PREFIX = "link_viewers:"

#: /whep/<path>/... и /hls/<path>/... — те же префиксы, что разрешены в Caddyfile.
_MEDIA_URI = re.compile(r"^/(?:whep|hls)/([a-z0-9]{8,64})(?:/|$)")

_DENY = Response(status_code=403)


def _deny(reason: str, **context: object) -> Response:
    """Отказ всегда с причиной в логе.

    Наружу причина не уходит — снаружи 403 обязан быть неотличим от 403.
    Но раньше четыре отказа из пяти не писали вообще ничего, и «плеер молча
    показывает чёрный экран» было невозможно отличить от «камера не работает»:
    в логах не было ни строчки. Теперь `docker compose logs api | grep authz`
    отвечает на этот вопрос сразу.
    """
    log.info("authz_denied", reason=reason, **context)
    return _DENY


def new_viewer_id() -> str:
    return secrets.token_urlsafe(24)


# ─── выдача доступа (вызывается со страницы просмотра) ───────────────────────
async def grant(viewer_id: str, mtx_path: str, link_id: uuid.UUID, ttl_seconds: int) -> None:
    """Разрешает зрителю смотреть конкретный путь.

    HASH, а не одно значение: зритель может держать открытыми несколько камер
    одновременно, и вторая ссылка не должна отбирать доступ у первой.
    """
    redis = get_redis()
    viewer_key = f"{_VIEWER_PREFIX}{viewer_id}"
    link_viewers_key = f"{_LINK_VIEWERS_PREFIX}{link_id}"

    pipe = redis.pipeline()
    pipe.hset(viewer_key, mtx_path, str(link_id))
    pipe.expire(viewer_key, ttl_seconds)
    pipe.sadd(link_viewers_key, viewer_id)
    pipe.expire(link_viewers_key, ttl_seconds)
    await pipe.execute()


async def grant_operator(viewer_id: str, mtx_path: str, ttl_seconds: int) -> None:
    """Доступ оператора к своей камере из панели — без публичной ссылки.

    Значение в хеше не id ссылки, а OPERATOR_GRANT: проверять нечего, ссылки
    нет. Ключ живёт минуты и заводится только для камеры, которую оператору
    и так разрешено видеть.
    """
    redis = get_redis()
    key = f"{_VIEWER_PREFIX}{viewer_id}"
    pipe = redis.pipeline()
    pipe.hset(key, mtx_path, OPERATOR_GRANT)
    pipe.expire(key, ttl_seconds)
    await pipe.execute()


async def count_viewers(link_id: uuid.UUID) -> int:
    return int(await get_redis().scard(f"{_LINK_VIEWERS_PREFIX}{link_id}"))


async def invalidate_link(link_id: uuid.UUID) -> None:
    """Сбрасывает кэш решения — следующий же медиа-запрос пойдёт в БД."""
    await get_redis().delete(f"{_LINK_CACHE_PREFIX}{link_id}")


async def drop_link_viewers(link_id: uuid.UUID) -> None:
    """Отбирает доступ у всех, кто смотрит по этой ссылке прямо сейчас."""
    redis = get_redis()
    link_viewers_key = f"{_LINK_VIEWERS_PREFIX}{link_id}"
    viewers = await redis.smembers(link_viewers_key)

    async with get_sessionmaker()() as session:
        link = await session.get(ShareLink, link_id)
        mtx_path = None
        if link is not None:
            camera = await session.get(Camera, link.camera_id)
            mtx_path = camera.mtx_path if camera else None

    pipe = redis.pipeline()
    for viewer_id in viewers:
        if mtx_path:
            pipe.hdel(f"{_VIEWER_PREFIX}{viewer_id}", mtx_path)
    pipe.delete(link_viewers_key)
    pipe.delete(f"{_LINK_CACHE_PREFIX}{link_id}")
    await pipe.execute()


def ip_allowed(ip: str, cidrs: list[str]) -> bool:
    if not cidrs:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if address in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


# ─── проверка ────────────────────────────────────────────────────────────────
async def _link_is_valid(link_id: uuid.UUID) -> bool:
    """Действительна ли ссылка. Результат кэшируется на короткое время."""
    settings = get_settings()
    redis = get_redis()
    cache_key = f"{_LINK_CACHE_PREFIX}{link_id}"

    cached = await redis.get(cache_key)
    if cached is not None:
        return cached == "1"

    async with get_sessionmaker()() as session:
        link = await session.scalar(select(ShareLink).where(ShareLink.id == link_id))
        valid = link is not None and link.revoked_at is None
        if valid and link is not None and link.expires_at is not None:
            valid = link.expires_at > dt.datetime.now(dt.UTC)
        if valid and link is not None:
            camera = await session.get(Camera, link.camera_id)
            valid = camera is not None and camera.is_enabled

    # Отрицательный ответ кэшируем короче: ошибочный запрет должен уходить
    # быстро, а разрешение и так снимается явной инвалидацией.
    ttl = settings.authz_cache_seconds if valid else 5
    await redis.set(cache_key, "1" if valid else "0", ex=ttl)
    return valid


@router.get("/internal/authz", include_in_schema=False)
async def authz(request: Request) -> Response:
    """Вызывается Caddy на каждый медиа-запрос. Должно быть быстро."""
    uri = request.headers.get("x-forwarded-uri") or request.headers.get("x-original-uri") or ""
    match = _MEDIA_URI.match(urlsplit(uri).path)
    if match is None:
        # Сюда попадают и запросы без X-Forwarded-Uri: если Caddy почему-то
        # не прислал заголовок, uri будет пустым, и это надо видеть.
        return _deny("uri_not_matched", uri=uri[:200])
    mtx_path = match.group(1)

    viewer_id = request.cookies.get(VIEW_COOKIE)
    if not viewer_id:
        return _deny("no_view_cookie", path=mtx_path, cookies=sorted(request.cookies))

    raw_link_id = await get_redis().hget(f"{_VIEWER_PREFIX}{viewer_id}", mtx_path)
    if not raw_link_id:
        return _deny("no_grant_for_path", path=mtx_path)

    if raw_link_id == OPERATOR_GRANT:
        # Просмотр оператором из панели: публичной ссылки нет и проверять
        # нечего. Ключ живёт минуты и создаётся только для своей камеры.
        return await _allow(mtx_path, raw_link_id, viewer_id)

    try:
        link_id = uuid.UUID(raw_link_id)
    except ValueError:
        return _deny("grant_is_not_a_link_id", path=mtx_path)

    if not await _link_is_valid(link_id):
        return _deny("link_invalid", path=mtx_path, link_id=str(link_id))

    return await _allow(mtx_path, str(link_id), viewer_id)


async def _allow(mtx_path: str, link_id: str, viewer_id: str) -> Response:
    response = Response(status_code=200)
    # Caddy копирует эти заголовки в запрос к MediaMTX (copy_headers).
    response.headers["X-Mtx-Path"] = mtx_path
    response.headers["X-Link-Id"] = link_id
    # Пока зритель смотрит, доступ не должен протухать. Без продления он
    # обрывался ровно через VIEW_COOKIE_TTL после открытия ссылки — посреди
    # трансляции, и выглядело это как «плеер сломался сам по себе». Отзыв
    # ссылки по-прежнему мгновенный: он удаляет ключ целиком.
    ttl = get_settings().view_cookie_ttl_minutes * 60
    await get_redis().expire(f"{_VIEWER_PREFIX}{viewer_id}", ttl)
    return response
