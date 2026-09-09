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
from collections.abc import Coroutine
from typing import Any

from sqlalchemy import delete, or_, select, text, update

from .config import get_settings
from .crypto import DecryptionError, get_cipher
from .db import dispose_engine, get_sessionmaker
from .logging_setup import configure_logging, get_logger
from .media.mtx_client import MediaMTXError, close_mtx, get_mtx
from .media.probe import ProbeResult, probe_rtsp
from .media.reconciler import reconcile, refresh_statuses
from .media.snapshot import capture_snapshot
from .metrics import history as metrics_history
from .metrics.host import HostMetrics, MetricsUnavailable
from .models import AuditLog, Camera, CameraStatus, Invitation, ViewSession
from .redis_client import close_redis

log = get_logger("worker")

SNAPSHOT_REFRESH_MINUTES = 30
#: Проба и снапшоты крутятся отдельным циклом, а не вместе с реконсиляцией.
#:
#: Оба запускают внешний процесс — ffprobe и ffmpeg, до 20 и 25 секунд на
#: камеру, — и в общем цикле растягивали тик до двух минут при заявленных
#: пятнадцати секундах. Всё это время пути в MediaMTX не создавались, а
#: статусы камер в панели не обновлялись, то есть реконсиляция простаивала
#: ровно тогда, когда нужна больше всего: при массовом добавлении камер и
#: при восстановлении после перезапуска медиа-сервера.
PROBE_INTERVAL_SECONDS = 20
#: Сколько камер берём на пробу за заход и сколько пробуем одновременно.
#: Внешние процессы почти всё время ждут сеть, а не считают, поэтому партию
#: можно взять больше параллелизма — очередь разбирается быстрее, а число
#: одновременных ffmpeg остаётся ограниченным.
PROBE_BATCH = 6
PROBE_CONCURRENCY = 3
#: Через сколько заходов пробы обновляем превью. 15 x 20 с = те же пять минут,
#: что и раньше при двадцати тиках главного цикла.
SNAPSHOT_EVERY_TICKS = 15
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
#: Сколько хранится журнал аудита. Год — чтобы разбор инцидента годичной
#: давности ещё был возможен; это то значение, которое меняют под требования
#: организации. Удалять журнал умеет только этот модуль и только через дверь,
#: открытую миграцией 0007: триггер БД по-прежнему запрещает и UPDATE, и
#: DELETE всем остальным.
AUDIT_RETENTION_DAYS = 365
#: Запрос, открывающий эту дверь. Имя параметра продублировано в миграции
#: 0007 — менять только вместе. SET LOCAL, а не SET: параметр живёт до конца
#: транзакции и не может уехать в соседний запрос вместе с соединением из пула.
AUDIT_RETENTION_UNLOCK = "SET LOCAL rtspgw.audit_retention = 'on'"
#: Сколько строк журнала удаляем за одну транзакцию. Первый прогон на давно
#: работающей установке иначе удалял бы миллионы строк разом: долгая
#: блокировка, распухший WAL и риск не уложиться в таймаут.
AUDIT_DELETE_BATCH = 5000
#: И сколько таких транзакций за один заход уборки. Ограничение сверху нужно,
#: чтобы уборка не заняла собой весь цикл: отставание она наверстает за
#: несколько заходов, а не за один.
AUDIT_DELETE_MAX_BATCHES = 20


async def _reconcile_cycle() -> None:
    mtx = get_mtx()
    async with get_sessionmaker()() as session:
        await reconcile(session, mtx)
        await refresh_statuses(session, mtx)
        await session.commit()


async def _probe_cycle(limit: int = PROBE_BATCH) -> None:
    """Пробует камеры, которых ещё не пробовали, — по партии за заход.

    Камеры пробуются параллельно, но обращения к БД остаются
    последовательными: сессия SQLAlchemy не рассчитана на одновременное
    использование из нескольких задач. Поэтому заход разделён на три шага —
    прочитать и расшифровать, сходить к камерам, записать результат.
    """
    cipher = get_cipher()
    async with get_sessionmaker()() as session:
        cameras = list(
            await session.scalars(
                select(Camera)
                .where(Camera.probed_at.is_(None), Camera.is_enabled.is_(True))
                .limit(limit)
            )
        )

        targets: list[tuple[Camera, str]] = []
        for camera in cameras:
            try:
                targets.append((camera, cipher.decrypt(camera.rtsp_url_enc)))
            except DecryptionError:
                log.error("probe_decrypt_failed", camera_id=str(camera.id))
        if not targets:
            return

        semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def probe_one(camera: Camera, rtsp_url: str) -> ProbeResult:
            async with semaphore:
                result = await probe_rtsp(rtsp_url)
                # Снимок сразу за пробой: соединение с камерой уже проверено,
                # и второй раз ходить к ней в этом же заходе незачем.
                await capture_snapshot(camera.id, rtsp_url)
                return result

        # return_exceptions: одна сорвавшаяся камера не должна стоить партии
        # остальных. Без этого gather уронил бы весь заход целиком, и
        # следующий начал бы его заново — с тем же исходом.
        results = await asyncio.gather(
            *(probe_one(camera, url) for camera, url in targets), return_exceptions=True
        )

        probed = 0
        for (camera, _), result in zip(targets, results, strict=True):
            if isinstance(result, BaseException):
                log.warning(
                    "probe_crashed",
                    camera_id=str(camera.id),
                    error=f"{type(result).__name__}: {result}"[:200],
                )
                continue
            probed += 1
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

        if probed:
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

    await _prune_audit_log(now - dt.timedelta(days=AUDIT_RETENTION_DAYS))


async def _prune_audit_log(cutoff: dt.datetime) -> int:
    """Удаляет устаревшие записи журнала аудита. Возвращает сколько удалено.

    Своей транзакцией на каждую партию, а не вместе с остальной уборкой:
    дверь в триггере открывается через SET LOCAL и обязана закрываться сразу
    после удаления, а не висеть открытой до конца всего цикла.

    Удаление идёт по идентификаторам из подзапроса с LIMIT, а не одним
    `DELETE ... WHERE created_at < cutoff`: партия ограничена сверху, и
    каждая транзакция остаётся короткой независимо от того, сколько накопила
    таблица к моменту первого прогона.
    """
    removed = 0
    for _ in range(AUDIT_DELETE_MAX_BATCHES):
        async with get_sessionmaker()() as session:
            doomed = list(
                await session.scalars(
                    select(AuditLog.id)
                    .where(AuditLog.created_at < cutoff)
                    .order_by(AuditLog.created_at)
                    .limit(AUDIT_DELETE_BATCH)
                )
            )
            if not doomed:
                break
            # Дверь открывается в той же транзакции, в которой идёт удаление,
            # и закрывается вместе с ней.
            await session.execute(text(AUDIT_RETENTION_UNLOCK))
            await session.execute(delete(AuditLog).where(AuditLog.id.in_(doomed)))
            await session.commit()

        removed += len(doomed)
        if len(doomed) < AUDIT_DELETE_BATCH:
            break

    if removed:
        log.info("audit_log_pruned", removed=removed, older_than=cutoff.isoformat())
    return removed


async def _guarded(name: str, coro: Coroutine[Any, Any, object]) -> None:
    """Прогоняет цикл, не давая его падению остановить весь worker.

    CancelledError отдельной ветки не требует: он наследуется от
    BaseException и мимо `except Exception` проходит сам. Это важно —
    остановка worker'а не должна попадать в лог как «цикл упал».
    """
    try:
        await coro
    except (MediaMTXError, OSError) as exc:
        log.warning("cycle_failed", cycle=name, error=str(exc))
    except Exception:
        log.exception("cycle_crashed", cycle=name)


async def _probe_loop(stop: asyncio.Event) -> None:
    """Проба камер и превью — своим темпом, не задерживая реконсиляцию."""
    tick = 0
    while not stop.is_set():
        tick += 1
        await _guarded("probe", _probe_cycle())
        if tick % SNAPSHOT_EVERY_TICKS == 0:
            await _guarded("snapshot", _snapshot_cycle())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=PROBE_INTERVAL_SECONDS)


async def _reconcile_loop(stop: asyncio.Event, interval: int) -> None:
    """Приведение MediaMTX к состоянию БД, статусы камер и уборка."""
    tick = 0
    while not stop.is_set():
        tick += 1
        await _guarded("reconcile", _reconcile_cycle())
        # Уборка — не на каждом тике: она перебирает несколько таблиц целиком.
        if tick % 20 == 0:
            await _guarded("cleanup", _cleanup_cycle())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


async def _metrics_loop(stop: asyncio.Event) -> None:
    """Снимает нагрузку сервера и складывает её в историю для панели.

    Живёт в worker'е по той же причине, что и остальные циклы: при нескольких
    процессах uvicorn каждый писал бы в один и тот же кольцевой буфер свои
    замеры, и история превратилась бы в чересполосицу.

    Чтение из procfs синхронное и на машине с сотней процессов занимает
    миллисекунды — уводим его в поток, чтобы не задерживать остальные циклы.
    """
    metrics = HostMetrics()
    reason = metrics.available()
    if reason:
        # Не Linux или procfs не примонтирован. Это не поломка: панель
        # покажет объяснение вместо графиков, а worker продолжит работу.
        log.info("metrics_unavailable", reason=reason)
        return

    interval = max(get_settings().metrics_interval_seconds, 1)
    log.info("metrics_started", interval=interval)
    while not stop.is_set():
        try:
            reading = await asyncio.to_thread(metrics.sample)
            if reading is not None:
                await metrics_history.push(reading)
        except MetricsUnavailable as exc:
            log.warning("metrics_stopped", error=str(exc))
            return
        except Exception:
            log.exception("cycle_crashed", cycle="metrics")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    _ = settings.secret_key

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info(
        "worker_started",
        interval=settings.reconcile_interval_seconds,
        probe_interval=PROBE_INTERVAL_SECONDS,
    )

    # Два независимых цикла вместо одного: реконсиляция обязана идти по
    # расписанию, а проба камеры может занять полминуты на каждую (см.
    # PROBE_INTERVAL_SECONDS).
    probe = asyncio.create_task(_probe_loop(stop), name="probe")
    metrics = asyncio.create_task(_metrics_loop(stop), name="metrics")
    try:
        await _reconcile_loop(stop, settings.reconcile_interval_seconds)
    finally:
        stop.set()
        # Проба может держать запущенный ffprobe, а docker ждёт остановку
        # недолго — обрываем, не дожидаясь. Потери нет: probed_at ставится
        # только вместе с коммитом, поэтому недоведённая камера просто
        # попадёт в следующую партию.
        probe.cancel()
        metrics.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await probe
        with contextlib.suppress(asyncio.CancelledError):
            await metrics

        await close_mtx()
        await close_redis()
        await dispose_engine()
        log.info("worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
