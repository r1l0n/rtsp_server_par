"""Проба камеры через ffprobe.

Главная практическая засада этого сервиса — кодеки. Камера может отдавать
H.265 или AAC, и такой поток браузер по WebRTC не сыграет. Пробуем камеру
в момент добавления и сразу говорим оператору, нужен ли транскод, вместо того
чтобы показывать ему чёрный квадрат.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from ..logging_setup import get_logger
from ..models import StreamProfile
from .ssrf import strip_credentials

log = get_logger("probe")

PROBE_TIMEOUT = 20

#: Что MediaMTX умеет отдавать в WebRTC без перекодирования.
WEBRTC_VIDEO_CODECS = frozenset({"h264", "vp8", "vp9", "av1"})
WEBRTC_AUDIO_CODECS = frozenset({"opus", "g722", "pcm_mulaw", "pcm_alaw"})


@dataclass(slots=True)
class ProbeResult:
    ok: bool = False
    error: str = ""
    video_codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    audio_codec: str = ""
    video_ok: bool = False
    audio_ok: bool = True
    recommended_profile: str = StreamProfile.passthrough.value
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_fps(value: str) -> float:
    try:
        num, _, den = value.partition("/")
        denominator = float(den or 1)
        return round(float(num) / denominator, 2) if denominator else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


async def probe_rtsp(url: str, timeout: int = PROBE_TIMEOUT) -> ProbeResult:  # noqa: ASYNC109
    # timeout здесь — это таймаут внешнего процесса ffprobe, который мы обязаны
    # убить сами; отменой задачи его не остановить, поэтому параметр свой.
    args = [
        "ffprobe",
        "-v", "error",
        "-hide_banner",
        "-rtsp_transport", "tcp",
        "-analyzeduration", "3000000",
        "-probesize", "5000000",
        "-print_format", "json",
        "-show_streams",
        "-i", url,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return ProbeResult(error="ffprobe не найден в образе")

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ProbeResult(
            error=f"камера не ответила за {timeout} с (проверьте адрес, порт и учётные данные)"
        )

    if process.returncode != 0:
        message = stderr.decode("utf-8", "replace").strip().splitlines()
        detail = message[-1] if message else "неизвестная ошибка"
        # В сообщении ffprobe может оказаться исходный URL с паролем.
        return ProbeResult(error=strip_credentials(detail)[:300])

    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return ProbeResult(error="не удалось разобрать ответ ffprobe")

    return _interpret(payload.get("streams") or [])


def _interpret(streams: list[dict[str, Any]]) -> ProbeResult:
    result = ProbeResult(ok=True)

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        return ProbeResult(error="в потоке нет видеодорожки")

    result.video_codec = str(video.get("codec_name", "")).lower()
    result.width = int(video.get("width") or 0)
    result.height = int(video.get("height") or 0)
    result.fps = _parse_fps(str(video.get("r_frame_rate", "0/1")))
    result.video_ok = result.video_codec in WEBRTC_VIDEO_CODECS

    if audio is not None:
        result.audio_codec = str(audio.get("codec_name", "")).lower()
        result.audio_ok = result.audio_codec in WEBRTC_AUDIO_CODECS

    if not result.video_ok:
        result.recommended_profile = StreamProfile.transcode.value
        result.notes.append(
            f"видео в {result.video_codec.upper() or 'неизвестном кодеке'} — браузер такой поток "
            f"по WebRTC не покажет, нужен транскод (примерно одно ядро CPU на камеру)"
        )
    if not result.audio_ok:
        result.notes.append(
            f"звук в {result.audio_codec.upper()} — для WebRTC нужен транскод либо отключение звука"
        )
    if result.video_ok and result.audio_ok:
        result.notes.append("поток совместим с WebRTC как есть, транскод не нужен")

    return result
