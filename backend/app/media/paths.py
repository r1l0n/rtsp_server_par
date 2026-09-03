"""Построение конфигурации пути MediaMTX из записи о камере."""

from __future__ import annotations

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


def conf_diff(current: dict[str, Any], wanted: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Расхождения по ключам: {ключ: (в MediaMTX, у нас)}.

    Нужна для лога: «путь обновлён» без указания причины не позволяет отличить
    осмысленное изменение от бесконечной перезаписи вхолостую.
    """
    return {
        key: (current.get(key), value)
        for key, value in wanted.items()
        if current.get(key) != value
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
