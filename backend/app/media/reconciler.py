"""Приведение MediaMTX к состоянию, описанному в БД.

Это сердце отказоустойчивости. MediaMTX стартует с пустым списком путей, и
через один цикл реконсиляции они восстанавливаются из PostgreSQL. Поэтому
перезапуск, падение или обновление образа медиа-сервера не требуют ручных
действий и не теряют настройки.

Панель дополнительно пушит изменения сразу (push_camera / drop_path), чтобы
камера появлялась мгновенно, а не через интервал цикла. Реконсиляция — это
страховка, а не основной путь.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..crypto import DecryptionError, get_cipher
from ..logging_setup import get_logger
from ..models import Camera, CameraStatus
from .mtx_client import MediaMTXClient, MediaMTXError, PathNotFound
from .paths import build_path_conf, conf_diff

log = get_logger("reconciler")


@dataclass(slots=True)
class ReconcileReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


async def desired_state(
    session: AsyncSession, node_id: str = "default"
) -> dict[str, dict[str, Any]]:
    """Какие пути должны существовать по данным БД."""
    cipher = get_cipher()
    cameras = await session.scalars(
        select(Camera).where(Camera.is_enabled.is_(True), Camera.node_id == node_id)
    )
    desired: dict[str, dict[str, Any]] = {}
    for camera in cameras:
        try:
            rtsp_url = cipher.decrypt(camera.rtsp_url_enc)
        except DecryptionError:
            # Ключ сменили, а rotate-key не прогнали: камеру пропускаем, но
            # громко сообщаем — иначе она молча исчезнет из эфира.
            log.error("decrypt_failed", camera_id=str(camera.id), mtx_path=camera.mtx_path)
            continue
        desired[camera.mtx_path] = build_path_conf(camera, rtsp_url)
    return desired


async def reconcile(session: AsyncSession, mtx: MediaMTXClient) -> ReconcileReport:
    settings = get_settings()
    report = ReconcileReport()

    desired = await desired_state(session)
    current_items = await mtx.list_config_paths()
    current = {item["name"]: item for item in current_items if item.get("name")}

    for name, conf in desired.items():
        try:
            if name not in current:
                await mtx.add_path(name, conf)
                report.added.append(name)
            elif diff := conf_diff(current[name], conf):
                await mtx.replace_path(name, conf)
                report.updated.append(name)
                # Каждая перезапись заставляет MediaMTX перечитать конфигурацию
                # целиком, поэтому фиксируем, ради чего это сделано: «путь
                # обновлён» без причины не отличить от перезаписи вхолостую.
                log.info(
                    "path_updated",
                    mtx_path=name,
                    changed={k: {"mtx": v[0], "want": v[1]} for k, v in diff.items()},
                )
        except MediaMTXError as exc:
            report.failed.append((name, str(exc)))

    for name in current:
        if name in desired:
            continue
        # Всё, чего нет в БД, — лишнее: конфиг MediaMTX не является источником
        # истины, руками туда пути не добавляют (см. mediamtx/mediamtx.yml).
        try:
            await mtx.delete_path(name)
            report.removed.append(name)
        except PathNotFound:
            pass
        except MediaMTXError as exc:
            report.failed.append((name, str(exc)))

    if report.changed or report.failed:
        log.info(
            "reconciled",
            added=len(report.added),
            updated=len(report.updated),
            removed=len(report.removed),
            failed=report.failed or None,
            interval=settings.reconcile_interval_seconds,
        )
    return report


# ─── Немедленное применение изменений из панели ──────────────────────────────
async def push_camera(camera: Camera, mtx: MediaMTXClient) -> None:
    rtsp_url = get_cipher().decrypt(camera.rtsp_url_enc)
    await mtx.upsert_path(camera.mtx_path, build_path_conf(camera, rtsp_url))


async def drop_path(mtx_path: str, mtx: MediaMTXClient) -> None:
    try:
        await mtx.delete_path(mtx_path)
    except PathNotFound:
        pass


# ─── Обновление статусов (watchdog) ──────────────────────────────────────────
async def refresh_statuses(session: AsyncSession, mtx: MediaMTXClient) -> None:
    """Переносит состояние путей MediaMTX в БД, чтобы оно было видно в панели."""
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)

    active = {item["name"]: item for item in await mtx.list_active_paths() if item.get("name")}
    cameras = list(await session.scalars(select(Camera).where(Camera.is_enabled.is_(True))))

    for camera in cameras:
        item = active.get(camera.mtx_path)
        if item is None:
            # Путь ещё не создан — следующий цикл reconcile его добавит.
            camera.status = CameraStatus.unknown
            camera.status_detail = "путь ещё не создан в MediaMTX"
            continue

        ready = bool(item.get("ready"))
        readers = len(item.get("readers") or [])

        if ready:
            camera.status = CameraStatus.online
            camera.status_detail = ""
            camera.last_ready_at = now
            camera.failure_streak = 0
            continue

        if camera.on_demand and readers == 0:
            # Норма: соединение с камерой намеренно закрыто до первого зрителя.
            camera.status = CameraStatus.idle
            camera.status_detail = ""
            camera.failure_streak = 0
            continue

        camera.failure_streak += 1
        camera.status = CameraStatus.offline
        camera.status_detail = "поток не поднимается"

        stale_for = (now - camera.last_ready_at).total_seconds() if camera.last_ready_at else None
        if stale_for is not None and stale_for > settings.stream_unhealthy_after_seconds:
            camera.status = CameraStatus.error

        # Экспоненциальный по сути backoff: пересоздаём путь редко и только
        # если он давно не поднимается — долбить мёртвую камеру бессмысленно.
        if camera.failure_streak in (5, 20, 80, 320):
            try:
                await push_camera(camera, mtx)
                log.info(
                    "path_recreated",
                    mtx_path=camera.mtx_path,
                    streak=camera.failure_streak,
                )
            except (MediaMTXError, DecryptionError) as exc:
                log.warning("path_recreate_failed", mtx_path=camera.mtx_path, error=str(exc))
