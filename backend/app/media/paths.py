"""Построение конфигурации пути MediaMTX из записи о камере."""

from __future__ import annotations

import re
import secrets
from typing import Any

from ..models import Camera, StreamProfile

#: Имя пути: только строчная латиница и цифры — так же, как в матчерах Caddyfile.
_PATH_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_PATH_LENGTH = 24


def new_mtx_path() -> str:
    """Случайное неугадываемое имя пути.

    Имя фактически является частью публичного URL, поэтому перебор путей не
    должен давать доступ к чужим камерам даже при ошибке в forward_auth.
    """
    return "".join(secrets.choice(_PATH_ALPHABET) for _ in range(_PATH_LENGTH))


def _seconds(value: int) -> str:
    return f"{value}s"


def build_path_conf(camera: Camera, rtsp_url: str, *, close_after: int = 60) -> dict[str, Any]:
    """Конфигурация пути для Control API.

    passthrough — MediaMTX сам тянет RTSP, нулевая нагрузка на CPU.
    transcode   — ffmpeg перекодирует в H.264/Opus и публикует обратно на
                  loopback-RTSP того же контейнера.
    """
    if camera.profile is StreamProfile.transcode:
        return {
            "source": "publisher",
            "runOnDemand": _ffmpeg_command(camera, rtsp_url),
            "runOnDemandRestart": True,
            "runOnDemandStartTimeout": "15s",
            "runOnDemandCloseAfter": _seconds(close_after),
            "maxReaders": 0,
            "record": False,
        }

    return {
        "source": rtsp_url,
        "sourceOnDemand": camera.on_demand,
        "sourceOnDemandStartTimeout": "10s",
        "sourceOnDemandCloseAfter": _seconds(close_after),
        "rtspTransport": "tcp",
        "maxReaders": 0,
        "record": False,
    }


#: Длительность в записи Go: «60s», «1m0s», «1h30m», «200ms».
_DURATION = re.compile(r"^(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h))+$")
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_DURATION_UNITS = {
    "ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0,
}


def _duration_seconds(value: Any) -> float | None:
    """Секунды из строки-длительности, либо None, если это не длительность."""
    if not isinstance(value, str) or not _DURATION.match(value):
        return None
    return sum(
        float(number) * _DURATION_UNITS[unit]
        for number, unit in _DURATION_PART.findall(value)
    )


def _same_value(current: Any, wanted: Any) -> bool:
    """Равны ли значения одного ключа конфигурации.

    MediaMTX нормализует длительности при разборе: посланное «60s» он вернёт
    как «1m0s». Дословное сравнение строк считало это расхождением, путь
    переписывался каждый цикл реконсиляции, и медиа-сервер бесконечно
    перечитывал конфигурацию.
    """
    if current == wanted:
        return True
    a, b = _duration_seconds(current), _duration_seconds(wanted)
    return a is not None and b is not None and a == b


def conf_diff(current: dict[str, Any], wanted: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Расхождения по ключам: {ключ: (в MediaMTX, у нас)}.

    Нужна для лога: «путь обновлён» без указания причины не позволяет отличить
    осмысленное изменение от бесконечной перезаписи вхолостую.
    """
    return {
        key: (current.get(key), value)
        for key, value in wanted.items()
        if not _same_value(current.get(key), value)
    }


def _ffmpeg_command(camera: Camera, rtsp_url: str) -> str:
    """Команда транскодирования.

    MediaMTX разбирает строку на аргументы сам, без shell. URL уже прошёл
    фильтр символов в media/ssrf.py, поэтому пробел или кавычка сюда не попадут
    и подменить аргументы команды нельзя.
    """
    audio = (
        ["-c:a", "libopus", "-b:a", "64k", "-ar", "48000", "-ac", "1"]
        if camera.audio_enabled
        else ["-an"]
    )
    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "warning",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-profile:v", "main",
        "-pix_fmt", "yuv420p",
        "-g", "30",
        "-sc_threshold", "0",
        *audio,
        # RTSP-сервер MediaMTX слушает только TCP (rtspTransports: [tcp]).
        # Без этой опции ffmpeg сначала пробует UDP, получает
        # «461 Unsupported Transport» и переподключается — лишний круг
        # и мусорная строка в логе при каждом старте потока.
        "-rtsp_transport", "tcp",
        "-f", "rtsp",
        f"rtsp://127.0.0.1:8554/{camera.mtx_path}",
    ]
    return " ".join(args)
