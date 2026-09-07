"""Фоновый процесс: реконсиляция, статусы, проба камер, снапшоты, уборка.

Запускается ровно в одном экземпляре (сервис `worker` в compose). Панель
(`api`) фоновых циклов не крутит — иначе при масштабировании uvicorn каждый
процесс начал бы конкурировать за одни и те же пути MediaMTX.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import signal

from sqlalchemy import delete, or_, select, update

from .config import get_settings
from .crypto import DecryptionError, get_cipher
from .db import dispose_engine, get_sessionmaker
from .logging_setup import configure_logging, get_logger
from .media.mtx_client import MediaMTXError, close_mtx, get_mtx
from .media.probe import probe_rtsp
from .media.reconciler import reconcile, refresh_statuses
from .media.snapshot import capture_snapshot
from .models import Camera, CameraStatus, Invitation, ViewSession
from .redis_client import close_redis

log = get_logger("worker")

SNAPSHOT_REFRESH_MINUTES = 30
#: Через сколько после открытия ссылки считаем сеанс просмотра завершённым.
#: Именно оценка: зритель не шлёт heartbeat, и когда он закрыл вкладку, мы не
#: знаем. Сколько человек смотрит прямо сейчас — знает Redis (internal/authz),
#: и метрика берётся оттуда, а не отсюда.
VIEW_SESSION_CLOSE_AFTER_MINUTES = 5
#: Сколько журнал просмотров хранится. Строка появляется на каждое открытие и
#: каждую перезагрузку публичной ссылки, и до сих пор эту таблицу не подрезал
#: никто — она была самой быстрорастущей в схеме.
VIEW_SESSION_RETENTION_DAYS = 90
#: Через сколько дней отработавшее приглашение удаляется из таблицы.
#: История остаётся в журнале аудита, а сама строка больше ни на что не влияет.
INVITE_RETENTION_DAYS = 30


async def _reconcile_cycle() -> None:
    mtx = get_mtx()
    async with get_sessionmaker()() as session:
        await reconcile(session, mtx)
        await refresh_statuses(session, mtx)
        await session.commit()


async def _probe_cycle(limit: int = 3) -> None:
    """Пробует камеры, которых ещё не пробовали, — по нескольку за цикл."""
    cipher = get_cipher()
    async with get_sessionmaker()() as session:
        cameras = list(
            await session.scalars(
                select(Camera)
                .where(Camera.probed_at.is_(None), Camera.is_enabled.is_(True))
                .limit(limit)
            )
        )
        for camera in cameras:
            try:
                rtsp_url = cipher.decrypt(camera.rtsp_url_enc)
            except DecryptionError:
                log.error("probe_decrypt_failed", camera_id=str(camera.id))
                continue

            result = await probe_rtsp(rtsp_url)
            camera.probe = result.as_dict()
            camera.probed_at = dt.datetime.now(dt.UTC)
            if not result.ok:
                camera.status = CameraStatus.error
                camera.status_detail = result.error
            log.info(
                "camera_probed",
                camera_id=str(camera.id),
                ok=result.ok,
                video=result.video_codec,
                audio=result.audio_codec,
                profile=result.recommended_profile,
            )
            await capture_snapshot(camera.id, rtsp_url)

        if cameras:
            await session.commit()


async def _snapshot_cycle() -> None:
    """Обновляет превью у камер, которые сейчас в эфире.

    Камеры в режиме on-demand намеренно не трогаем: снимок разбудил бы поток
    и держал бы соединение с камерой без единого зрителя. У них превью
    делается один раз при добавлении.
    """
    cipher = get_cipher()
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=SNAPSHOT_REFRESH_MINUTES)
    async with get_sessionmaker()() as session:
        cameras = list(
            await session.scalars(
                select(Camera).where(
                    Camera.is_enabled.is_(True),
                    Camera.status == CameraStatus.online,
                    Camera.probed_at.is_not(None),
                )
            )
        )
    for camera in cameras:
        path = get_settings().snapshot_dir / f"{camera.id}.jpg"
        if path.exists() and dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC) > cutoff:
            continue
        try:
            await capture_snapshot(camera.id, cipher.decrypt(camera.rtsp_url_enc))
        except DecryptionError:
            continue


async def _cleanup_cycle() -> None:
    """Закрывает старые сеансы просмотра и убирает то, что отжило свой срок."""
    now = dt.datetime.now(dt.UTC)
    stale_before = now - dt.timedelta(minutes=VIEW_SESSION_CLOSE_AFTER_MINUTES)
    sessions_before = now - dt.timedelta(days=VIEW_SESSION_RETENTION_DAYS)
    invites_before = now - dt.timedelta(days=INVITE_RETENTION_DAYS)

    async with get_sessionmaker()() as session:
        await session.execute(
            update(ViewSession)
            .where(ViewSession.ended_at.is_(None), ViewSession.started_at < stale_before)
            .values(ended_at=now)
        )
        # Журнал просмотров старше срока хранения. Кто и когда открывал ссылку,
        # остаётся в audit_log — там это и положено искать.
        await session.execute(
            delete(ViewSession).where(ViewSession.started_at < sessions_before)
        )
        # Принятые, отозванные и давно просроченные приглашения. Действующие
        # не трогаем никогда — по ним человек ещё может прийти.
        await session.execute(
            delete(Invitation).where(
                or_(
                    Invitation.accepted_at < invites_before,
                    Invitation.revoked_at < invites_before,
                    Invitation.expires_at < invites_before,
                )
            )
        )
        await session.commit()


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    _ = settings.secret_key

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("worker_started", interval=settings.reconcile_interval_seconds)
    tick = 0
    try:
        while not stop.is_set():
            tick += 1
            for name, coro in (
                ("reconcile", _reconcile_cycle()),
                ("probe", _probe_cycle()),
            ):
                try:
                    await coro
                except (MediaMTXError, OSError) as exc:
                    log.warning("cycle_failed", cycle=name, error=str(exc))
                except Exception:
                    log.exception("cycle_crashed", cycle=name)

            # Тяжёлые задачи — не на каждом тике.
            if tick % 20 == 0:
                for name, coro in (("snapshot", _snapshot_cycle()), ("cleanup", _cleanup_cycle())):
                    try:
                        await coro
                    except Exception:
                        log.exception("cycle_crashed", cycle=name)

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.reconcile_interval_seconds)
    finally:
        await close_mtx()
        await close_redis()
        await dispose_engine()
        log.info("worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
