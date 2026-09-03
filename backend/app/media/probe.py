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
    #: Каким транспортом получен результат — «tcp» или «udp».
    transport: str = ""
    #: Полный вывод ffprobe с вырезанным паролем. Нужен диагностике: одна
    #: последняя строка ошибки слишком часто не содержит настоящей причины.
    stderr: str = ""

    def as_dict(self, *, include_log: bool = False) -> dict[str, Any]:
        """Для хранения в БД. Сырой лог по умолчанию не кладём: он большой,
        живёт ровно до следующей пробы и нужен только на экране диагностики."""
        data = asdict(self)
        if not include_log:
            data.pop("stderr", None)
        return data


def _parse_fps(value: str) -> float:
    try:
        num, _, den = value.partition("/")
        denominator = float(den or 1)
        return round(float(num) / denominator, 2) if denominator else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


async def probe_rtsp(
    url: str,
    timeout: int = PROBE_TIMEOUT,  # noqa: ASYNC109
    transport: str = "tcp",
) -> ProbeResult:
    # timeout здесь — это таймаут внешнего процесса ffprobe, который мы обязаны
    # убить сами; отменой задачи его не остановить, поэтому параметр свой.
    if transport not in ("tcp", "udp"):
        raise ValueError(f"неизвестный RTSP-транспорт: {transport}")

    args = [
        "ffprobe",
        # verbose, а не error: диагностике нужен весь диалог с камерой, иначе
        # «401 Unauthorized» теряется среди служебных строк ffprobe.
        "-v", "verbose",
        "-hide_banner",
        "-rtsp_transport", transport,
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
        return ProbeResult(error="ffprobe не найден в образе", transport=transport)

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ProbeResult(
            error=f"камера не ответила за {timeout} с (проверьте адрес, порт и учётные данные)",
            transport=transport,
        )

    # В выводе ffprobe всегда есть исходный URL с паролем — маскируем текст
    # целиком, а не только строку с ошибкой: он показывается оператору.
    log_text = strip_credentials(stderr.decode("utf-8", "replace").strip())[-8000:]

    if process.returncode != 0:
        lines = [line for line in log_text.splitlines() if line.strip()]
        return ProbeResult(
            error=_meaningful_error(lines)[:300], transport=transport, stderr=log_text
        )

    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return ProbeResult(
            error="не удалось разобрать ответ ffprobe", transport=transport, stderr=log_text
        )

    result = _interpret(payload.get("streams") or [])
    result.transport = transport
    result.stderr = log_text
    return result


#: Строки уровня verbose, которые ничего не объясняют, но всегда идут последними.
_NOISE = ("Immediate exit requested", "Statistics:", "bytes read", "seeks")


def _meaningful_error(lines: list[str]) -> str:
    """Последняя содержательная строка вывода ffprobe.

    Именно последняя, а не первая: ffprobe сначала печатает служебное, а
    настоящую причину («401 Unauthorized», «Connection timed out») — в конце.
    Но самый хвост занимает статистика, её и отбрасываем.
    """
    for line in reversed(lines):
        if not any(noise in line for noise in _NOISE):
            return line.strip()
    return lines[-1].strip() if lines else "неизвестная ошибка"


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
