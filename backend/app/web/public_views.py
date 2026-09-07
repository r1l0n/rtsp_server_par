"""Публичный просмотр по ссылке: страница плеера и embed."""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth import ratelimit
from ..auth.deps import DbSession
from ..auth.passwords import verify_password
from ..config import get_settings
from ..crypto import hash_token, tokens_equal
from ..internal.authz import (
    VIEW_COOKIE,
    count_viewers,
    grant,
    ip_allowed,
    link_is_valid,
    new_viewer_id,
    viewer_counted,
)
from ..logging_setup import get_logger
from ..middleware import client_ip
from ..models import Camera, ShareLink, ViewSession
from ..ptz import service as ptz
from ..ptz.lock import viewer_holder
from ..redis_client import get_redis
from .panel_views import ptz_response
from .templating import render, set_view_cookie

log = get_logger("public")
router = APIRouter(tags=["public"])

#: Общая формулировка на все отказы: посторонний не должен по тексту ошибки
#: понимать, существует ли ссылка, истекла ли она или отозвана.
GENERIC_DENIED = "Ссылка недействительна или срок её действия истёк."


class Denied(Exception):
    def __init__(self, message: str = GENERIC_DENIED, status_code: int = 403) -> None:
        self.message = message
        self.status_code = status_code


async def _find_link(db: AsyncSession, slug: str) -> tuple[ShareLink | None, Camera | None]:
    """Ссылка по slug и её камера — без единой проверки прав."""
    link = await db.scalar(select(ShareLink).where(ShareLink.slug == slug))
    if link is None:
        return None, None
    return link, await db.get(Camera, link.camera_id)


def _assert_link_live(link: ShareLink, camera: Camera | None, ip: str) -> Camera:
    """Проверки, не зависящие от токена: срок, отзыв, адрес, камера.

    Вынесены отдельно намеренно. Раньше они жили внутри проверки токена, и
    перезагрузка страницы без `?t=` шла мимо них целиком: ограничение по
    адресу и пароль ссылки обходились простым F5 с другой сети.
    """
    if link.revoked_at is not None:
        raise Denied()
    if link.expires_at is not None and link.expires_at <= dt.datetime.now(dt.UTC):
        raise Denied()
    if not ip_allowed(ip, list(link.allowed_cidrs or [])):
        raise Denied("Доступ с этого IP-адреса запрещён владельцем ссылки.")
    if camera is None or not camera.is_enabled:
        raise Denied("Камера недоступна.")
    return camera


async def _assert_capacity(link: ShareLink, viewer_id: str) -> None:
    """Лимит одновременных зрителей.

    Зритель, уже учтённый в этой ссылке, лимит не занимает повторно — иначе
    ссылка с лимитом 1 отказывала бы собственному зрителю при перезагрузке.
    """
    if not link.max_concurrent:
        return
    if await viewer_counted(link.id, viewer_id):
        return
    if await count_viewers(link.id) >= link.max_concurrent:
        raise Denied("Достигнут лимит одновременных зрителей этой ссылки.", status_code=429)


async def _resolve_link(
    db: AsyncSession, slug: str, token: str, ip: str, viewer_id: str = ""
) -> tuple[ShareLink, Camera]:
    link, camera = await _find_link(db, slug)
    if link is None:
        raise Denied(status_code=404)

    if not token or not tokens_equal(hash_token(token), link.token_hash):
        raise Denied()

    camera = _assert_link_live(link, camera, ip)
    await _assert_capacity(link, viewer_id)
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

    # Считает база, а не Python. `link.view_count += 1` — это чтение, сложение
    # и запись тремя шагами: два одновременных открытия ссылки читают одно и
    # то же значение и записывают одно и то же n+1, и один просмотр пропадает.
    # У ссылки из рассылки, которую открывают разом, расхождение заметное.
    #
    # synchronize_session=False: объект `link` после этого места не читается,
    # а обновлять его в памяти ради одного счётчика — лишний запрос.
    await db.execute(
        update(ShareLink)
        .where(ShareLink.id == link.id)
        .values(
            view_count=ShareLink.view_count + 1,
            last_viewed_at=dt.datetime.now(dt.UTC),
        )
        .execution_options(synchronize_session=False)
    )
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
        # Пульт появляется только когда сошлось и то, и другое: камера умеет
        # поворачиваться и владелец разрешил это конкретной ссылкой.
        ptz_url=(
            f"/v/{link.slug}/ptz" if camera.ptz_enabled and link.ptz_allowed else ""
        ),
    )
    set_view_cookie(response, viewer_id, ttl)
    return response


async def _already_granted(viewer_id: str, camera: Camera, link: ShareLink) -> bool:
    """Страницу можно перезагрузить без токена в адресной строке.

    Сверяем именно с идентификатором ЭТОЙ ссылки, а не с фактом наличия ключа:
    иначе грант, выданный соседней ссылкой на ту же камеру (или просмотром
    оператора из панели), открывал бы страницу под чужим slug — вместе с его
    паролем и ограничением по адресу.
    """
    if not viewer_id:
        return False
    granted = await get_redis().hget(f"viewer:{viewer_id}", camera.mtx_path)
    return bool(granted == str(link.id))


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

    viewer_id = request.cookies.get(VIEW_COOKIE) or ""
    link, camera = await _find_link(db, slug)

    # Перезагрузка страницы без ?t= — доступ уже выдан этому браузеру именно
    # этой ссылкой. Токен при этом не требуется, но всё остальное — срок,
    # отзыв, разрешённые адреса, лимит зрителей — проверяется как обычно.
    # Пароль повторно не спрашиваем: его уже вводили, когда выдавали грант.
    reused = (
        link is not None
        and camera is not None
        and await _already_granted(viewer_id, camera, link)
    )

    try:
        if reused and link is not None:
            camera = _assert_link_live(link, camera, ip)
            await _assert_capacity(link, viewer_id)
        else:
            link, camera = await _resolve_link(db, slug, token, ip, viewer_id)
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

    if link.password_hash and not reused:
        return render(
            request, "link_password.html", slug=slug, token=token, embed=embed,
            camera_name=link.label or "Просмотр камеры",
        )

    return await _open_stream(request, db, link, camera, embed=embed)


@router.post("/v/{slug}/ptz")
async def view_ptz(request: Request, db: DbSession, slug: str) -> JSONResponse:
    """Команда пульта у зрителя публичной ссылки.

    Токена в запросе нет — он участвует только в первом открытии страницы.
    Право на управление выводится из того же гранта в Redis, которым живут
    медиа-запросы: cookie зрителя → путь MediaMTX → идентификатор ссылки.

    CSRF-токена на публичной странице взяться неоткуда — сессии у зрителя
    нет. Межсайтовый вызов закрыт двумя условиями сразу: тело идёт как JSON,
    что заставляет браузер сначала спросить preflight, а CORS мы не разрешаем
    вовсе; плюс требуется заголовок X-Requested-With, который чужая страница
    без preflight поставить не может. Любого из них достаточно поодиночке:
    preflight на чужом домене не проходит, и запрос до нас не доходит.

    На SameSite здесь не рассчитываем. Cookie доступа помечена SameSite=None,
    иначе не работает встраивание в чужой сайт, ради которого заведён
    /embed/ (см. templating.set_view_cookie).
    """
    if request.headers.get("x-requested-with") != "fetch":
        return JSONResponse({"error": GENERIC_DENIED}, status_code=403)

    viewer_id = request.cookies.get(VIEW_COOKIE)
    if not viewer_id:
        return JSONResponse({"error": GENERIC_DENIED}, status_code=403)

    link = await db.scalar(select(ShareLink).where(ShareLink.slug == slug))
    if link is None or not link.ptz_allowed:
        return JSONResponse({"error": GENERIC_DENIED}, status_code=403)

    camera = await db.get(Camera, link.camera_id)
    if camera is None or not camera.is_enabled or not camera.ptz_enabled:
        return JSONResponse({"error": "Камера недоступна."}, status_code=409)

    granted = await get_redis().hget(f"viewer:{viewer_id}", camera.mtx_path)
    if granted != str(link.id):
        return JSONResponse({"error": GENERIC_DENIED}, status_code=403)
    if not await link_is_valid(link.id):
        return JSONResponse({"error": GENERIC_DENIED}, status_code=403)

    ip = client_ip(request)
    if not ip_allowed(ip, list(link.allowed_cidrs or [])):
        return JSONResponse({"error": GENERIC_DENIED}, status_code=403)

    holder = viewer_holder(viewer_id)
    limited = await ratelimit.hit("ptz", holder, ratelimit.PTZ_BY_HOLDER)
    if not limited.allowed:
        return JSONResponse(
            {"error": "Слишком много команд."}, status_code=429,
            headers={"Retry-After": str(limited.retry_after)},
        )

    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    action = str(payload.get("action", "move"))
    direction = str(payload.get("direction", ""))

    if action == "stop":
        await ptz.release(camera, holder)
        return JSONResponse({"ok": True})

    if await ptz.should_audit(camera.id, holder):
        await audit.record(
            db, audit.PTZ_CONTROL, target_type="link", target_id=str(link.id), ip=ip,
            user_agent=request.headers.get("user-agent", ""),
            meta={"camera_id": str(camera.id), "source": "link"},
        )
        await db.commit()

    return ptz_response(await ptz.press(camera, direction, holder))


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
