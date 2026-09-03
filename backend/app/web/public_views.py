"""Публичный просмотр по ссылке: страница плеера и embed."""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth import ratelimit
from ..auth.deps import DbSession
from ..auth.passwords import verify_password
from ..config import get_settings
from ..crypto import hash_token, tokens_equal
from ..internal.authz import VIEW_COOKIE, count_viewers, grant, ip_allowed, new_viewer_id
from ..logging_setup import get_logger
from ..middleware import client_ip
from ..models import Camera, ShareLink, ViewSession
from .templating import render

log = get_logger("public")
router = APIRouter(tags=["public"])

#: Общая формулировка на все отказы: посторонний не должен по тексту ошибки
#: понимать, существует ли ссылка, истекла ли она или отозвана.
GENERIC_DENIED = "Ссылка недействительна или срок её действия истёк."


class Denied(Exception):
    def __init__(self, message: str = GENERIC_DENIED, status_code: int = 403) -> None:
        self.message = message
        self.status_code = status_code


async def _resolve_link(
    db: AsyncSession, slug: str, token: str, ip: str
) -> tuple[ShareLink, Camera]:
    link = await db.scalar(select(ShareLink).where(ShareLink.slug == slug))
    if link is None:
        raise Denied(status_code=404)

    if not token or not tokens_equal(hash_token(token), link.token_hash):
        raise Denied()

    if link.revoked_at is not None:
        raise Denied()
    if link.expires_at is not None and link.expires_at <= dt.datetime.now(dt.UTC):
        raise Denied()
    if not ip_allowed(ip, list(link.allowed_cidrs or [])):
        raise Denied("Доступ с этого IP-адреса запрещён владельцем ссылки.")

    camera = await db.get(Camera, link.camera_id)
    if camera is None or not camera.is_enabled:
        raise Denied("Камера недоступна.")

    if link.max_concurrent and await count_viewers(link.id) >= link.max_concurrent:
        raise Denied("Достигнут лимит одновременных зрителей этой ссылки.", status_code=429)

    return link, camera


async def _open_stream(
    request: Request,
    db: AsyncSession,
    link: ShareLink,
    camera: Camera,
    *,
    embed: bool,
) -> HTMLResponse:
    """Выдаёт зрителю доступ и рендерит плеер."""
    settings = get_settings()
    ip = client_ip(request)
    ttl = settings.view_cookie_ttl_minutes * 60

    viewer_id = request.cookies.get(VIEW_COOKIE) or new_viewer_id()
    await grant(viewer_id, camera.mtx_path, link.id, ttl)

    link.view_count += 1
    link.last_viewed_at = dt.datetime.now(dt.UTC)
    db.add(
        ViewSession(
            link_id=link.id,
            session_key=secrets.token_urlsafe(16),
            ip=ip,
            user_agent=request.headers.get("user-agent", "")[:400],
        )
    )
    await audit.record(
        db, audit.LINK_VIEWED, target_type="link", target_id=str(link.id), ip=ip,
        user_agent=request.headers.get("user-agent", ""),
        meta={"camera_id": str(camera.id), "embed": embed},
    )
    await db.commit()

    response = render(
        request,
        "embed.html" if embed else "player.html",
        camera_name=camera.name,
        whep_url=f"/whep/{camera.mtx_path}/whep",
        hls_url=f"/hls/{camera.mtx_path}/index.m3u8",
        audio_enabled=camera.audio_enabled,
    )
    response.set_cookie(
        VIEW_COOKIE,
        viewer_id,
        max_age=ttl,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


async def _already_granted(request: Request, camera: Camera) -> bool:
    """Страницу можно перезагрузить без токена в адресной строке."""
    from ..redis_client import get_redis

    viewer_id = request.cookies.get(VIEW_COOKIE)
    if not viewer_id:
        return False
    granted = await get_redis().hget(f"viewer:{viewer_id}", camera.mtx_path)
    return bool(granted)


# ─── Страница просмотра ──────────────────────────────────────────────────────
@router.get("/v/{slug}", response_class=HTMLResponse)
async def view(request: Request, db: DbSession, slug: str, t: str = "") -> HTMLResponse:
    return await _view(request, db, slug, t, embed=False)


@router.get("/embed/{slug}", response_class=HTMLResponse)
async def view_embed(request: Request, db: DbSession, slug: str, t: str = "") -> HTMLResponse:
    return await _view(request, db, slug, t, embed=True)


async def _view(
    request: Request, db: DbSession, slug: str, token: str, *, embed: bool
) -> HTMLResponse:
    ip = client_ip(request)
    limited = await ratelimit.hit("view", ip, ratelimit.PUBLIC_VIEW_BY_IP)
    if not limited.allowed:
        return render(
            request, "denied.html", status_code=429,
            message="Слишком много запросов. Попробуйте через минуту.", embed=embed,
        )

    link = await db.scalar(select(ShareLink).where(ShareLink.slug == slug))
    camera = await db.get(Camera, link.camera_id) if link is not None else None

    # Перезагрузка страницы без ?t= — доступ уже выдан этому браузеру.
    if link is not None and camera is not None and not token:
        if await _already_granted(request, camera):
            return await _open_stream(request, db, link, camera, embed=embed)

    try:
        link, camera = await _resolve_link(db, slug, token, ip)
    except Denied as denied:
        if link is not None:
            await audit.record(
                db, audit.LINK_DENIED, target_type="link", target_id=str(link.id), ip=ip,
                meta={"reason": denied.message},
            )
            await db.commit()
        return render(
            request, "denied.html", status_code=denied.status_code,
            message=denied.message, embed=embed,
        )

    if link.password_hash:
        return render(
            request, "link_password.html", slug=slug, token=token, embed=embed,
            camera_name=link.label or "Просмотр камеры",
        )

    return await _open_stream(request, db, link, camera, embed=embed)


@router.post("/v/{slug}", response_class=HTMLResponse)
async def view_with_password(
    request: Request,
    db: DbSession,
    slug: str,
    token: Annotated[str, Form()],
    link_password: Annotated[str, Form()],
    embed: Annotated[str, Form()] = "",
) -> HTMLResponse:
    ip = client_ip(request)
    is_embed = embed == "1"

    limited = await ratelimit.hit("link_pw", ip, ratelimit.LINK_PASSWORD_BY_IP)
    if not limited.allowed:
        return render(
            request, "denied.html", status_code=429,
            message="Слишком много попыток. Попробуйте позже.", embed=is_embed,
        )

    try:
        link, camera = await _resolve_link(db, slug, token, ip)
    except Denied as denied:
        return render(
            request, "denied.html", status_code=denied.status_code,
            message=denied.message, embed=is_embed,
        )

    if link.password_hash and not verify_password(link.password_hash, link_password):
        await audit.record(
            db, audit.LINK_DENIED, target_type="link", target_id=str(link.id), ip=ip,
            meta={"reason": "bad_link_password"},
        )
        await db.commit()
        return render(
            request, "link_password.html", status_code=401, slug=slug, token=token,
            embed=is_embed, camera_name=link.label or "Просмотр камеры",
            error="Неверный пароль.",
        )

    return await _open_stream(request, db, link, camera, embed=is_embed)
