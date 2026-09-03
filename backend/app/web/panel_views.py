"""Панель: камеры и публичные ссылки."""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth.deps import CsrfProtected, CurrentUser, DbSession, Forbidden
from ..auth.passwords import hash_password
from ..config import get_settings
from ..crypto import generate_token, get_cipher, hash_token
from ..internal.authz import drop_link_viewers, invalidate_link
from ..logging_setup import get_logger
from ..media.mtx_client import MediaMTXError, get_mtx
from ..media.paths import new_mtx_path
from ..media.reconciler import drop_path, push_camera
from ..media.ssrf import UnsafeCameraUrl, validate_rtsp_url
from ..middleware import client_ip
from ..models import Camera, CameraStatus, Role, ShareLink, StreamProfile, User
from .templating import notice, redirect, render

log = get_logger("panel")
router = APIRouter(tags=["panel"])

TTL_CHOICES: dict[str, int] = {
    "1h": 1,
    "24h": 24,
    "7d": 168,
    "30d": 720,
    "never": 0,
}


def _visible_cameras(user: User):
    """Оператор видит свои камеры, администратор — все."""
    query = select(Camera).order_by(Camera.name)
    if user.role is not Role.admin:
        query = query.where(Camera.created_by_id == user.id)
    return query


async def _get_camera(db: AsyncSession, camera_id: uuid.UUID, user: User) -> Camera:
    camera = await db.get(Camera, camera_id)
    if camera is None:
        raise Forbidden("Камера не найдена")
    if user.role is not Role.admin and camera.created_by_id != user.id:
        raise Forbidden("Эта камера принадлежит другому пользователю")
    return camera


# ─── Список камер ────────────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: DbSession, user: CurrentUser) -> HTMLResponse:
    cameras = list(await db.scalars(_visible_cameras(user)))
    link_counts = dict(
        (
            await db.execute(
                select(ShareLink.camera_id, func.count(ShareLink.id))
                .where(ShareLink.revoked_at.is_(None))
                .group_by(ShareLink.camera_id)
            )
        ).all()
    )
    return render(
        request,
        "cameras.html",
        user=user,
        cameras=cameras,
        link_counts=link_counts,
        notice=notice(request.query_params.get("notice")),
    )


@router.get("/cameras/new", response_class=HTMLResponse)
async def camera_new_form(request: Request, user: CurrentUser) -> HTMLResponse:
    return render(request, "camera_form.html", user=user, camera=None)


@router.post("/cameras")
async def camera_create(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _: CsrfProtected,
    name: Annotated[str, Form()],
    rtsp_url: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    on_demand: Annotated[str, Form()] = "on",
    audio_enabled: Annotated[str, Form()] = "",
    profile: Annotated[str, Form()] = StreamProfile.passthrough.value,
) -> Response:
    name = name.strip()
    if not name:
        return render(
            request, "camera_form.html", status_code=400, user=user, camera=None,
            error="Укажите название камеры", form={"name": name, "rtsp_url": ""},
        )

    try:
        target = await validate_rtsp_url(rtsp_url)
    except UnsafeCameraUrl as exc:
        return render(
            request, "camera_form.html", status_code=400, user=user, camera=None,
            error=str(exc), form={"name": name, "description": description, "rtsp_url": ""},
        )

    camera = Camera(
        name=name,
        description=description.strip()[:2000],
        rtsp_url_enc=get_cipher().encrypt(target.url),
        host=target.host,
        port=target.port,
        mtx_path=new_mtx_path(),
        profile=StreamProfile(profile),
        on_demand=on_demand == "on",
        audio_enabled=audio_enabled == "on",
        status=CameraStatus.unknown,
        created_by_id=user.id,
    )
    db.add(camera)
    await db.flush()

    await audit.record(
        db, audit.CAMERA_CREATED, actor_id=user.id, target_type="camera",
        target_id=str(camera.id), ip=client_ip(request),
        meta={"host": target.host, "port": target.port, "profile": camera.profile.value},
    )
    await db.commit()

    # Пушим сразу, чтобы камера появилась в эфире не дожидаясь реконсиляции.
    # Если MediaMTX сейчас недоступен — worker доведёт состояние сам.
    try:
        await push_camera(camera, get_mtx())
    except MediaMTXError as exc:
        log.warning("push_failed", camera_id=str(camera.id), error=str(exc))

    return redirect(f"/cameras/{camera.id}?notice=camera_created")


@router.get("/cameras/{camera_id}", response_class=HTMLResponse)
async def camera_detail(
    request: Request, db: DbSession, user: CurrentUser, camera_id: uuid.UUID
) -> HTMLResponse:
    camera = await _get_camera(db, camera_id, user)
    links = list(
        await db.scalars(
            select(ShareLink)
            .where(ShareLink.camera_id == camera.id)
            .order_by(ShareLink.created_at.desc())
        )
    )
    return render(
        request,
        "camera_detail.html",
        user=user,
        camera=camera,
        links=links,
        ttl_choices=TTL_CHOICES,
        base_url=get_settings().base_url,
        now=dt.datetime.now(dt.UTC),
        notice=notice(request.query_params.get("notice")),
    )


@router.post("/cameras/{camera_id}")
async def camera_update(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _: CsrfProtected,
    camera_id: uuid.UUID,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    rtsp_url: Annotated[str, Form()] = "",
    on_demand: Annotated[str, Form()] = "",
    audio_enabled: Annotated[str, Form()] = "",
    profile: Annotated[str, Form()] = StreamProfile.passthrough.value,
    is_enabled: Annotated[str, Form()] = "",
) -> Response:
    camera = await _get_camera(db, camera_id, user)

    changed: dict[str, object] = {}
    camera.name = name.strip() or camera.name
    camera.description = description.strip()[:2000]
    camera.on_demand = on_demand == "on"
    camera.audio_enabled = audio_enabled == "on"
    camera.is_enabled = is_enabled == "on"
    if camera.profile.value != profile:
        camera.profile = StreamProfile(profile)
        changed["profile"] = profile

    # Пустое поле URL означает «оставить прежний» — так пароль камеры не нужно
    # вводить заново при каждом переименовании.
    if rtsp_url.strip():
        try:
            target = await validate_rtsp_url(rtsp_url)
        except UnsafeCameraUrl as exc:
            return render(
                request, "camera_form.html", status_code=400, user=user, camera=camera,
                error=str(exc),
            )
        camera.rtsp_url_enc = get_cipher().encrypt(target.url)
        camera.host = target.host
        camera.port = target.port
        camera.probed_at = None  # перепроба на следующем цикле worker'а
        changed["rtsp_url"] = target.display_url

    await audit.record(
        db, audit.CAMERA_UPDATED, actor_id=user.id, target_type="camera",
        target_id=str(camera.id), ip=client_ip(request), meta=changed,
    )
    await db.commit()

    try:
        if camera.is_enabled:
            await push_camera(camera, get_mtx())
        else:
            await drop_path(camera.mtx_path, get_mtx())
    except MediaMTXError as exc:
        log.warning("push_failed", camera_id=str(camera.id), error=str(exc))

    return redirect(f"/cameras/{camera.id}?notice=camera_updated")


@router.post("/cameras/{camera_id}/delete")
async def camera_delete(
    request: Request, db: DbSession, user: CurrentUser, _: CsrfProtected, camera_id: uuid.UUID
) -> RedirectResponse:
    camera = await _get_camera(db, camera_id, user)
    mtx_path = camera.mtx_path

    links = list(await db.scalars(select(ShareLink).where(ShareLink.camera_id == camera.id)))
    for link in links:
        await drop_link_viewers(link.id)

    await audit.record(
        db, audit.CAMERA_DELETED, actor_id=user.id, target_type="camera",
        target_id=str(camera.id), ip=client_ip(request), meta={"name": camera.name},
    )
    await db.delete(camera)
    await db.commit()

    try:
        await drop_path(mtx_path, get_mtx())
    except MediaMTXError as exc:
        log.warning("drop_failed", mtx_path=mtx_path, error=str(exc))

    return redirect("/?notice=camera_deleted")


@router.post("/cameras/{camera_id}/reprobe")
async def camera_reprobe(
    request: Request, db: DbSession, user: CurrentUser, _: CsrfProtected, camera_id: uuid.UUID
) -> RedirectResponse:
    camera = await _get_camera(db, camera_id, user)
    camera.probed_at = None
    await db.commit()
    return redirect(f"/cameras/{camera.id}?notice=camera_updated")


@router.get("/cameras/{camera_id}/snapshot.jpg")
async def camera_snapshot(db: DbSession, user: CurrentUser, camera_id: uuid.UUID) -> Response:
    camera = await _get_camera(db, camera_id, user)
    path = get_settings().snapshot_dir / f"{camera.id}.jpg"
    if not path.exists():
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=60"})


# ─── Публичные ссылки ────────────────────────────────────────────────────────
@router.post("/cameras/{camera_id}/links")
async def link_create(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _: CsrfProtected,
    camera_id: uuid.UUID,
    label: Annotated[str, Form()] = "",
    ttl: Annotated[str, Form()] = "24h",
    max_concurrent: Annotated[int, Form()] = 0,
    allowed_cidrs: Annotated[str, Form()] = "",
    link_password: Annotated[str, Form()] = "",
) -> HTMLResponse:
    camera = await _get_camera(db, camera_id, user)

    hours = TTL_CHOICES.get(ttl, get_settings().default_link_ttl_hours)
    expires_at = (
        dt.datetime.now(dt.UTC) + dt.timedelta(hours=hours) if hours else None
    )

    token = generate_token()
    link = ShareLink(
        camera_id=camera.id,
        label=label.strip()[:200],
        slug=secrets.token_urlsafe(9),
        token_hash=hash_token(token),
        expires_at=expires_at,
        max_concurrent=max(0, min(max_concurrent, 10_000)),
        allowed_cidrs=[c.strip() for c in allowed_cidrs.split(",") if c.strip()],
        password_hash=hash_password(link_password) if link_password else None,
        created_by_id=user.id,
    )
    db.add(link)
    await db.flush()

    await audit.record(
        db, audit.LINK_CREATED, actor_id=user.id, target_type="link", target_id=str(link.id),
        ip=client_ip(request),
        meta={
            "camera_id": str(camera.id),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "has_password": bool(link_password),
            "cidrs": link.allowed_cidrs,
        },
    )
    await db.commit()

    links = list(
        await db.scalars(
            select(ShareLink)
            .where(ShareLink.camera_id == camera.id)
            .order_by(ShareLink.created_at.desc())
        )
    )
    # Токен показываем ровно один раз и прямо здесь: в БД лежит только его хеш,
    # а редирект утащил бы токен в историю браузера и в логи прокси.
    return render(
        request,
        "camera_detail.html",
        user=user,
        camera=camera,
        links=links,
        ttl_choices=TTL_CHOICES,
        base_url=get_settings().base_url,
        now=dt.datetime.now(dt.UTC),
        new_link=link,
        new_link_url=f"{get_settings().base_url}/v/{link.slug}?t={token}",
    )


@router.post("/links/{link_id}/revoke")
async def link_revoke(
    request: Request, db: DbSession, user: CurrentUser, _: CsrfProtected, link_id: uuid.UUID
) -> RedirectResponse:
    link = await db.get(ShareLink, link_id)
    if link is None:
        raise Forbidden("Ссылка не найдена")
    camera = await _get_camera(db, link.camera_id, user)

    link.revoked_at = dt.datetime.now(dt.UTC)
    await audit.record(
        db, audit.LINK_REVOKED, actor_id=user.id, target_type="link", target_id=str(link.id),
        ip=client_ip(request),
    )
    await db.commit()

    await invalidate_link(link.id)
    await drop_link_viewers(link.id)
    await _kick_active_sessions(camera)

    return redirect(f"/cameras/{camera.id}?notice=link_revoked")


@router.post("/links/{link_id}/delete")
async def link_delete(
    request: Request, db: DbSession, user: CurrentUser, _: CsrfProtected, link_id: uuid.UUID
) -> RedirectResponse:
    link = await db.get(ShareLink, link_id)
    if link is None:
        raise Forbidden("Ссылка не найдена")
    camera = await _get_camera(db, link.camera_id, user)

    await drop_link_viewers(link.id)
    await audit.record(
        db, audit.LINK_DELETED, actor_id=user.id, target_type="link", target_id=str(link.id),
        ip=client_ip(request),
    )
    await db.delete(link)
    await db.commit()
    await _kick_active_sessions(camera)

    return redirect(f"/cameras/{camera.id}?notice=link_deleted")


async def _kick_active_sessions(camera: Camera) -> None:
    """Обрывает WebRTC-сессии на пути камеры.

    Установленное WebRTC-соединение идёт напрямую по UDP и forward_auth его
    больше не видит, поэтому отзыв ссылки обязан разорвать сессию явно.
    Обрываются все зрители этой камеры — те, у кого ссылка ещё действует,
    переподключатся автоматически за секунду.
    """
    try:
        mtx = get_mtx()
        for session in await mtx.list_webrtc_sessions():
            if session.get("path") == camera.mtx_path and session.get("id"):
                await mtx.kick_webrtc_session(str(session["id"]))
    except MediaMTXError as exc:
        log.warning("kick_failed", mtx_path=camera.mtx_path, error=str(exc))
