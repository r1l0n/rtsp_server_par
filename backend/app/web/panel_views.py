"""Панель: камеры и публичные ссылки."""

from __future__ import annotations

import base64
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
from ..crypto import DecryptionError, generate_token, get_cipher, hash_token
from ..internal.authz import drop_link_viewers, invalidate_link
from ..logging_setup import get_logger
from ..media.diagnose import diagnose
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


def _camera_form_values(
    name: str, description: str, on_demand: str, audio_enabled: str, profile: str
) -> dict[str, object]:
    """Значения формы для повторного показа. Сам URL не возвращаем никогда:
    в HTML он утёк бы вместе с паролем в кэш браузера и в историю."""
    return {
        "name": name,
        "description": description,
        "on_demand": on_demand == "on",
        "audio_enabled": audio_enabled == "on",
        "profile": profile,
    }


# Маршрут объявлен раньше POST /cameras/{camera_id}: иначе «preview» попал бы
# в него как camera_id и развалился бы на разборе UUID.
@router.post("/cameras/preview", response_class=HTMLResponse)
async def camera_preview(
    request: Request,
    user: CurrentUser,
    _: CsrfProtected,
    rtsp_url: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    on_demand: Annotated[str, Form()] = "on",
    audio_enabled: Annotated[str, Form()] = "",
    profile: Annotated[str, Form()] = StreamProfile.passthrough.value,
) -> HTMLResponse:
    """Проверка ссылки до сохранения камеры: кадр, кодеки, совместимость.

    Камера здесь создаётся временно, только в памяти, и в БД не попадает —
    смысл кнопки в том, чтобы не заводить запись ради проверки и не удалять
    её потом. Шаги с MediaMTX пропускаются: пути ещё нет и быть не должно.
    """
    form = _camera_form_values(name, description, on_demand, audio_enabled, profile)

    try:
        target = await validate_rtsp_url(rtsp_url)
    except UnsafeCameraUrl as exc:
        return render(
            request, "camera_form.html", status_code=400, user=user, camera=None,
            error=str(exc), form=form,
        )

    draft = Camera(
        name=name.strip() or "проверка",
        host=target.host,
        port=target.port,
        mtx_path=new_mtx_path(),
        profile=StreamProfile(profile),
        on_demand=on_demand == "on",
        audio_enabled=audio_enabled == "on",
        is_enabled=True,
    )
    report = await diagnose(draft, target.url, None)

    log.info(
        "camera_previewed",
        actor_id=str(user.id), host=target.host, port=target.port,
        verdict=report.verdict_state,
    )
    return render(
        request, "camera_form.html", user=user, camera=None, form=form,
        preview=report,
        preview_frame=_frame_data_uri(report.frame),
        normalized_url=target.display_url if target.normalized else "",
    )


def _frame_data_uri(frame: bytes | None) -> str:
    """JPEG прямо в разметку. Отдельный маршрут-картинка потребовал бы хранить
    кадр непроверенной камеры на сервере — ради одного показа это лишнее."""
    if not frame:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii")


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
            error="Укажите название камеры",
            form=_camera_form_values(name, description, on_demand, audio_enabled, profile),
        )

    try:
        target = await validate_rtsp_url(rtsp_url)
    except UnsafeCameraUrl as exc:
        return render(
            request, "camera_form.html", status_code=400, user=user, camera=None,
            error=str(exc),
            form=_camera_form_values(name, description, on_demand, audio_enabled, profile),
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


async def _render_detail(
    request: Request, db: AsyncSession, user: User, camera: Camera, **extra: object
) -> HTMLResponse:
    """Страница камеры. Собрана в одном месте: её рендерят четыре обработчика,
    и разъехавшийся контекст молча ломал бы то список ссылок, то диагностику."""
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
        **extra,
    )


@router.get("/cameras/{camera_id}", response_class=HTMLResponse)
async def camera_detail(
    request: Request, db: DbSession, user: CurrentUser, camera_id: uuid.UUID
) -> HTMLResponse:
    camera = await _get_camera(db, camera_id, user)
    return await _render_detail(
        request, db, user, camera, notice=notice(request.query_params.get("notice"))
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


@router.post("/cameras/{camera_id}/diagnose", response_class=HTMLResponse)
async def camera_diagnose(
    request: Request, db: DbSession, user: CurrentUser, _: CsrfProtected, camera_id: uuid.UUID
) -> HTMLResponse:
    """Полная проверка камеры по кнопке — синхронно, с показом отчёта.

    Синхронно намеренно: оператор нажал «проверить» и должен увидеть ответ,
    а не «результат появится в течение минуты». Проверка идёт до 40 секунд
    (два запуска ffprobe и TCP-таймаут) — это нормальная цена за ответ на
    вопрос «почему чёрный экран».
    """
    camera = await _get_camera(db, camera_id, user)

    try:
        rtsp_url = get_cipher().decrypt(camera.rtsp_url_enc)
    except DecryptionError:
        return await _render_detail(
            request, db, user, camera,
            error="Не удалось расшифровать адрес камеры: ключ шифрования сменился. "
                  "Введите RTSP-ссылку заново.",
        )

    report = await diagnose(camera, rtsp_url, get_mtx())

    # Результат пробы кладём в БД: worker больше не будет перепробовать камеру,
    # а список камер сразу покажет актуальный статус.
    if report.probe is not None:
        camera.probe = report.probe.as_dict()
        camera.probed_at = dt.datetime.now(dt.UTC)
        if not report.probe.ok:
            camera.status = CameraStatus.error
            camera.status_detail = report.probe.error
    await db.commit()

    log.info(
        "camera_diagnosed",
        camera_id=str(camera.id),
        verdict=report.verdict_state,
        failed=[step.key for step in report.failed],
    )
    return await _render_detail(
        request, db, user, camera,
        diagnosis=report,
        diagnosis_frame=_frame_data_uri(report.frame),
    )


@router.post("/cameras/{camera_id}/profile")
async def camera_set_profile(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _: CsrfProtected,
    camera_id: uuid.UUID,
    profile: Annotated[str, Form()],
) -> RedirectResponse:
    """Переключение профиля одной кнопкой прямо из отчёта диагностики.

    Отдельный обработчик, а не форма настроек: диагностика говорит «нужен
    транскод», и заставлять оператора искать нужный select в другой карточке —
    ровно тот случай, когда совет не выполняют.
    """
    camera = await _get_camera(db, camera_id, user)
    try:
        camera.profile = StreamProfile(profile)
    except ValueError:
        raise Forbidden("Неизвестный профиль потока") from None

    await audit.record(
        db, audit.CAMERA_UPDATED, actor_id=user.id, target_type="camera",
        target_id=str(camera.id), ip=client_ip(request), meta={"profile": profile},
    )
    await db.commit()

    try:
        await push_camera(camera, get_mtx())
    except MediaMTXError as exc:
        log.warning("push_failed", camera_id=str(camera.id), error=str(exc))

    return redirect(f"/cameras/{camera.id}?notice=profile_changed")


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

    # Токен показываем ровно один раз и прямо здесь: в БД лежит только его хеш,
    # а редирект утащил бы токен в историю браузера и в логи прокси.
    return await _render_detail(
        request, db, user, camera,
        new_link=link,
        new_link_url=f"{get_settings().base_url}/v/{link.slug}?t={token}",
    )


@router.post("/links/{link_id}/rotate", response_class=HTMLResponse)
async def link_rotate(
    request: Request, db: DbSession, user: CurrentUser, _: CsrfProtected, link_id: uuid.UUID
) -> HTMLResponse:
    """Перевыпуск токена существующей ссылки.

    Токен в БД не хранится, поэтому «покажите адрес, который я выдал в прошлый
    раз» технически невозможно, и единственный честный ответ на это —
    перевыпустить. Старый адрес перестаёт работать: иначе операция была бы
    способом бесконтрольно размножать действующие ссылки.
    """
    link = await db.get(ShareLink, link_id)
    if link is None:
        raise Forbidden("Ссылка не найдена")
    camera = await _get_camera(db, link.camera_id, user)

    token = generate_token()
    link.token_hash = hash_token(token)
    link.revoked_at = None

    await audit.record(
        db, audit.LINK_ROTATED, actor_id=user.id, target_type="link", target_id=str(link.id),
        ip=client_ip(request), meta={"camera_id": str(camera.id)},
    )
    await db.commit()

    # Зрителям со старым адресом доступ обрываем сразу: cookie просмотра живёт
    # своей жизнью и о смене токена сама не узнает.
    await invalidate_link(link.id)
    await drop_link_viewers(link.id)
    await _kick_active_sessions(camera)

    return await _render_detail(
        request, db, user, camera,
        new_link=link,
        new_link_url=f"{get_settings().base_url}/v/{link.slug}?t={token}",
        rotated=True,
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
