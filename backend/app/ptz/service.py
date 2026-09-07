"""Точка входа PTZ: от камеры в базе до команды на устройстве.

Здесь собрано всё, что не относится к конкретному протоколу: учётные данные,
проверка адреса, выбор драйвера, кэш опознания, блокировка и сторож, который
останавливает камеру за драйверы, не умеющие останавливаться сами.

Модель «нажал и держит» устроена так: браузер шлёт команду примерно раз в
700 мс, пока кнопка нажата, а каждая команда действует `ptz_move_seconds`.
Отсюда три независимых рубежа остановки, и это главное, ради чего всё
написано именно так:

1. сам протокол (ONVIF Timeout, Hikvision momentary) — работает, даже если
   наш процесс умер;
2. сторож в этом модуле — покрывает Dahua и Axis, у которых такого таймаута
   нет, при закрытой вкладке или оборванной сети;
3. явный «стоп» от браузера при отпускании кнопки — обычный путь.

Чего сознательно нет: сторожа, переживающего перезапуск самого `api`. Если
процесс убить ровно в момент удержания стрелки, камера Dahua или Axis
доедет до упора. Случай редкий и самоустраняющийся при следующем нажатии,
а надёжное решение потребовало бы держать состояние движения в Redis и
разбирать его из `worker` — цена выше пользы. Отмечено в docs/runbook.md.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import uuid
from urllib.parse import unquote, urlsplit

from ..config import get_settings
from ..crypto import DecryptionError, get_cipher
from ..logging_setup import get_logger
from ..media.ssrf import UnsafeCameraUrl, assert_reachable_host, strip_credentials
from ..models import Camera, PtzDriver
from ..redis_client import get_redis
from .base import DeviceInfo, Driver, PtzError, PtzUnsupported, Target, Vector
from .onvif import OnvifDriver
from .vendors import AxisDriver, DahuaDriver, HikvisionDriver

log = get_logger("ptz")

DRIVERS: dict[str, Driver] = {
    "hikvision": HikvisionDriver(),
    "dahua": DahuaDriver(),
    "axis": AxisDriver(),
    "onvif": OnvifDriver(),
}

#: Порядок автоопределения. Родной протокол пробуется раньше ONVIF: у
#: Hikvision и Dahua ONVIF на заводских настройках выключен, а ISAPI и CGI
#: работают с теми же учётными данными, что уже вписаны в RTSP-ссылку.
DETECT_ORDER = ("hikvision", "dahua", "axis", "onvif")

#: Проверенный адрес камеры держим в кэше: DNS-запрос на каждое нажатие
#: стрелки недопустим, а не проверять вовсе нельзя — имя может переехать
#: на приватный адрес уже после добавления камеры.
_HOST_CHECK_TTL = 300
_HOST_CHECK_PREFIX = "ptz:host:"

#: Сторожа по камерам. Живут только в этом процессе — см. докстринг модуля.
_watchdogs: dict[uuid.UUID, asyncio.Task[None]] = {}


# ─── Учётные данные ──────────────────────────────────────────────────────────
def credentials_from_rtsp(rtsp_url: str) -> tuple[str, str]:
    """Логин и пароль из RTSP-ссылки, обратно из процентной кодировки."""
    parts = urlsplit(rtsp_url)
    return unquote(parts.username or ""), unquote(parts.password or "")


def pack_credentials(username: str, password: str) -> bytes:
    return get_cipher().encrypt(json.dumps({"u": username, "p": password}))


def unpack_credentials(blob: bytes) -> tuple[str, str]:
    data = json.loads(get_cipher().decrypt(blob))
    return str(data.get("u", "")), str(data.get("p", ""))


def default_port(camera: Camera) -> int:
    if camera.ptz_port:
        return camera.ptz_port
    return 443 if camera.ptz_tls else 80


# ─── Сборка цели ─────────────────────────────────────────────────────────────
async def _assert_host_allowed(camera: Camera, port: int) -> None:
    """SSRF-проверка адреса управления, с коротким кэшем результата."""
    redis = get_redis()
    key = f"{_HOST_CHECK_PREFIX}{camera.id}:{port}"
    if await redis.get(key):
        return
    await assert_reachable_host(camera.host, port)
    await redis.set(key, "1", ex=_HOST_CHECK_TTL)


async def build_target(camera: Camera) -> Target:
    """Куда и под кем идти. Кэш опознания подставляется, если он есть."""
    if camera.ptz_credentials_enc is not None:
        username, password = unpack_credentials(camera.ptz_credentials_enc)
    else:
        username, password = credentials_from_rtsp(get_cipher().decrypt(camera.rtsp_url_enc))

    port = default_port(camera)
    await _assert_host_allowed(camera, port)

    meta = camera.ptz_meta or {}
    return Target(
        host=camera.host,
        port=port,
        tls=camera.ptz_tls,
        username=username,
        password=password,
        channel=max(1, camera.ptz_channel),
        profile_token=str(meta.get("profile_token", "")),
        service_path=str(meta.get("service_path", "")),
        clock_skew=float(meta.get("clock_skew", 0.0) or 0.0),
    )


def driver_for(camera: Camera) -> Driver | None:
    """Драйвер по настройке камеры или по кэшу опознания."""
    name = camera.ptz_driver
    if name == PtzDriver.auto.value:
        name = str((camera.ptz_meta or {}).get("driver", ""))
    return DRIVERS.get(name)


# ─── Опознание ───────────────────────────────────────────────────────────────
async def detect(camera: Camera) -> DeviceInfo:
    """Определяет вендора. Пробует протоколы по очереди, а не разом.

    Последовательно — намеренно: четыре одновременных запроса с неверным
    паролем часть камер воспринимает как перебор и временно блокирует
    учётную запись.
    """
    target = await build_target(camera)

    if camera.ptz_driver != PtzDriver.auto.value:
        driver = DRIVERS.get(camera.ptz_driver)
        if driver is None:
            raise PtzUnsupported(f"неизвестный драйвер: {camera.ptz_driver}")
        return await driver.identify(target)

    problems: list[str] = []
    for name in DETECT_ORDER:
        try:
            return await DRIVERS[name].identify(target)
        except PtzError as exc:
            problems.append(f"{name}: {exc}")

    log.info("ptz_detect_failed", camera_id=str(camera.id), problems=problems)
    raise PtzUnsupported(
        "не удалось опознать управление: камера не ответила ни по ISAPI, ни по "
        "Dahua CGI, ни по VAPIX, ни по ONVIF. Проверьте HTTP-порт камеры и то, "
        "что ONVIF включён в её веб-интерфейсе"
    )


def store_detection(camera: Camera, info: DeviceInfo) -> None:
    """Кладёт результат опознания в камеру. Коммит — на вызывающем.

    Кэш обязателен: без него ONVIF делал бы три SOAP-запроса на каждое
    нажатие стрелки вместо одного.
    """
    camera.ptz_meta = {
        "driver": info.driver,
        "model": info.model,
        "profile_token": info.profile_token,
        "service_path": info.service_path,
        "clock_skew": info.clock_skew,
        "detail": info.detail,
    }
    camera.ptz_checked_at = dt.datetime.now(dt.UTC)


# ─── Сторож ──────────────────────────────────────────────────────────────────
async def _auto_stop(camera_id: uuid.UUID, driver: Driver, target: Target, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        await driver.stop(target)
        log.info("ptz_auto_stopped", camera_id=str(camera_id))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # камера может не ответить — это не наша авария
        log.info("ptz_auto_stop_failed", camera_id=str(camera_id), error=str(exc)[:200])


def _arm_watchdog(camera_id: uuid.UUID, driver: Driver, target: Target, delay: float) -> None:
    _cancel_watchdog(camera_id)
    task = asyncio.create_task(_auto_stop(camera_id, driver, target, delay))
    _watchdogs[camera_id] = task
    task.add_done_callback(lambda done: _forget_watchdog(camera_id, done))


def _forget_watchdog(camera_id: uuid.UUID, task: asyncio.Task[None]) -> None:
    # Сверяем сам объект: за время работы сторожа его мог заменить новый.
    if _watchdogs.get(camera_id) is task:
        _watchdogs.pop(camera_id, None)


def _cancel_watchdog(camera_id: uuid.UUID) -> None:
    task = _watchdogs.pop(camera_id, None)
    if task is not None and not task.done():
        task.cancel()


async def shutdown() -> None:
    """Гасит всех сторожей — вызывается при остановке приложения."""
    for camera_id in list(_watchdogs):
        _cancel_watchdog(camera_id)


# ─── Команды ─────────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True, slots=True)
class Result:
    ok: bool
    #: "busy" — камерой управляет другой; "error" — камера не приняла команду.
    reason: str = ""
    message: str = ""
    retry_after_ms: int = 0
    #: Как часто браузеру повторять команду, пока кнопка нажата.
    heartbeat_ms: int = 700


def _heartbeat_ms(move_seconds: float) -> int:
    """Заметно чаще, чем истекает команда, — иначе движение будет рваным."""
    return max(200, int(move_seconds * 1000 * 0.35))


async def should_audit(camera_id: uuid.UUID, holder: str) -> bool:
    """Пора ли писать в журнал про этого владельца и эту камеру.

    Одна наводка камеры — несколько десятков команд. В журнале нужен факт
    «такой-то управлял такой-то камерой», а не каждое нажатие стрелки,
    поэтому пишем не чаще раза в пять минут на пару камера+владелец.
    """
    key = f"ptz:audited:{camera_id}:{holder}"
    return bool(await get_redis().set(key, "1", nx=True, ex=300))


async def press(camera: Camera, direction: str, holder: str) -> Result:
    """Одно нажатие (или его продолжение, пока кнопку держат)."""
    from . import lock

    settings = get_settings()
    heartbeat = _heartbeat_ms(settings.ptz_move_seconds)

    # Направление разбираем до захвата камеры. Разбор ничего не требует, кроме
    # самой строки, а мусорный запрос не должен ни занимать управление, ни
    # доходить до Redis: раньше неизвестное направление камеру захватывало, а
    # обратно не отпускало — и следующие ptz_hold_seconds пульт был заблокирован
    # ни для кого. Ровно то, чего не должно происходить и с неисправной камерой
    # (см. lock.release ниже).
    try:
        velocity = Vector.from_direction(direction)
    except ValueError:
        return Result(
            ok=False, reason="error", message="Неизвестное направление.",
            heartbeat_ms=heartbeat,
        )

    acquired, retry_after = await lock.acquire(camera.id, holder, settings.ptz_hold_seconds)
    if not acquired:
        # К камере не ходим вовсе: чужие нажатия не должны создавать трафик.
        return Result(
            ok=False,
            reason="busy",
            message="Камерой управляет другой зритель.",
            retry_after_ms=retry_after,
            heartbeat_ms=heartbeat,
        )

    try:
        driver, target = await _resolve(camera)
        await driver.move(target, velocity, settings.ptz_move_seconds)
    except (PtzError, UnsafeCameraUrl, DecryptionError) as exc:
        # Неисправная камера не должна держать управление за собой: иначе
        # следующие пятнадцать секунд пульт заблокирован ни для кого.
        await lock.release(camera.id, holder)
        detail = strip_credentials(str(exc))[:200]
        log.warning("ptz_move_failed", camera_id=str(camera.id), error=detail)
        return Result(ok=False, reason="error", message=detail, heartbeat_ms=heartbeat)

    if not driver.stops_itself:
        _arm_watchdog(camera.id, driver, target, settings.ptz_move_seconds)

    return Result(ok=True, heartbeat_ms=heartbeat)


async def release(camera: Camera, holder: str) -> Result:
    """Кнопку отпустили."""
    from . import lock

    _cancel_watchdog(camera.id)
    try:
        driver, target = await _resolve(camera)
        await driver.stop(target)
    except (PtzError, UnsafeCameraUrl, DecryptionError) as exc:
        detail = strip_credentials(str(exc))[:200]
        log.info("ptz_stop_failed", camera_id=str(camera.id), error=detail)
    finally:
        await lock.release(camera.id, holder)
    return Result(ok=True)


async def _resolve(camera: Camera) -> tuple[Driver, Target]:
    driver = driver_for(camera)
    if driver is None:
        raise PtzUnsupported(
            "для этой камеры не определён способ управления — нажмите "
            "«Проверить управление» в её настройках"
        )
    return driver, await build_target(camera)
