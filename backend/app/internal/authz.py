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
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from ..config import get_settings
from ..db import get_sessionmaker
from ..logging_setup import get_logger
from ..middleware import client_ip
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
#:
#: Префикс необязателен намеренно. Caddy внутри `handle` исполняет директивы
#: по своему порядку, а не по порядку в файле, и `uri strip_prefix` легко
#: оказывается раньше `forward_auth` — тогда сюда приходит уже срезанный
#: «/<path>/whep». Один раз это уже стоило полной неработоспособности медиа,
#: поэтому принимаем обе формы: имя пути мы всё равно сверяем с выданными
#: зрителю правами, и безопасность держится на них, а не на префиксе.
_MEDIA_URI = re.compile(r"^(?:/(?:whep|hls))?/([a-z0-9]{8,64})(?:/|$)")

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


async def count_all_viewers() -> int:
    """Сколько зрителей смотрит по публичным ссылкам прямо сейчас.

    Считается по тем же множествам, на которых держится лимит одновременных
    зрителей, — это единственное место, где состояние настоящее. Метрика
    раньше бралась из таблицы view_sessions и показывала не зрителей, а число
    открытий страницы за последние минуты.
    """
    redis = get_redis()
    total = 0
    async for key in redis.scan_iter(match=f"{_LINK_VIEWERS_PREFIX}*", count=200):
        total += int(await redis.scard(key))
    return total


async def viewer_counted(link_id: uuid.UUID, viewer_id: str) -> bool:
    """Учтён ли этот зритель в лимите одновременных просмотров.

    Нужно, чтобы перезагрузка страницы не выглядела приходом ещё одного
    зрителя: он уже в множестве, и без этой проверки ссылка с лимитом 1
    отказывала бы собственному зрителю при первом же F5.
    """
    if not viewer_id:
        return False
    return bool(await get_redis().sismember(f"{_LINK_VIEWERS_PREFIX}{link_id}", viewer_id))


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
@dataclass(frozen=True, slots=True)
class LinkAccess:
    """Всё, что нужно знать о ссылке на каждом медиа-запросе."""

    valid: bool
    #: Подсети, которым владелец разрешил смотреть. Пусто — ограничения нет.
    cidrs: tuple[str, ...] = ()


async def link_access(link_id: uuid.UUID) -> LinkAccess:
    """Состояние ссылки. Результат кэшируется на короткое время.

    Список подсетей лежит здесь же, а не проверяется только при открытии
    страницы: без этого ограничение по адресу обходилось перезагрузкой —
    зритель, однажды открывший ссылку с разрешённого адреса, дальше смотрел
    её откуда угодно, потому что грант в Redis продлевался сам.
    """
    settings = get_settings()
    redis = get_redis()
    cache_key = f"{_LINK_CACHE_PREFIX}{link_id}"

    cached = await redis.get(cache_key)
    if cached is not None:
        return _decode_access(cached)

    cidrs: tuple[str, ...] = ()
    async with get_sessionmaker()() as session:
        link = await session.scalar(select(ShareLink).where(ShareLink.id == link_id))
        valid = link is not None and link.revoked_at is None
        if valid and link is not None and link.expires_at is not None:
            valid = link.expires_at > dt.datetime.now(dt.UTC)
        if valid and link is not None:
            camera = await session.get(Camera, link.camera_id)
            valid = camera is not None and camera.is_enabled
        if valid and link is not None:
            cidrs = tuple(link.allowed_cidrs or ())

    access = LinkAccess(valid=valid, cidrs=cidrs)
    # Отрицательный ответ кэшируем короче: ошибочный запрет должен уходить
    # быстро, а разрешение и так снимается явной инвалидацией.
    ttl = settings.authz_cache_seconds if valid else 5
    await redis.set(cache_key, _encode_access(access), ex=ttl)
    return access


def _encode_access(access: LinkAccess) -> str:
    return json.dumps({"v": access.valid, "c": list(access.cidrs)})


def _decode_access(raw: str) -> LinkAccess:
    """Разбор кэша. Битое значение считаем отсутствующим разрешением.

    Формат кэша менялся (раньше это была строка «1»/«0»), и после обновления
    в Redis какое-то время лежат записи обоих видов. Старую запись разбираем
    до json намеренно: `json.loads("1")` не падает, а возвращает число, и
    ссылка молча оказалась бы недействительной на весь срок жизни кэша.
    """
    if raw in ("0", "1"):
        return LinkAccess(valid=raw == "1")
    try:
        data = json.loads(raw)
    except ValueError:
        return LinkAccess(valid=False)
    if not isinstance(data, dict):
        return LinkAccess(valid=False)
    cidrs = data.get("c") or []
    return LinkAccess(
        valid=bool(data.get("v")),
        cidrs=tuple(str(item) for item in cidrs) if isinstance(cidrs, list) else (),
    )


async def link_is_valid(link_id: uuid.UUID) -> bool:
    """Действительна ли ссылка — без учёта ограничения по адресу."""
    return (await link_access(link_id)).valid


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

    access = await link_access(link_id)
    if not access.valid:
        return _deny("link_invalid", path=mtx_path, link_id=str(link_id))

    # Адрес приходит из X-Real-IP, который Caddy проставляет сам (см. Caddyfile).
    if not ip_allowed(client_ip(request), list(access.cidrs)):
        return _deny("ip_not_allowed", path=mtx_path, link_id=str(link_id))

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
