"""Пошаговая диагностика камеры.

Отдельный модуль, а не расширение probe.py, потому что задача другая. Проба
отвечает на вопрос «какие кодеки» и крутится в фоне у worker'а. Диагностика
отвечает на вопрос «почему не показывает» и запускается оператором вручную из
панели — она обязана дать связный отчёт, а не одну строку ошибки.

Порядок шагов повторяет путь пакета: имя -> TCP -> RTSP -> кодеки -> браузер ->
MediaMTX. Первый упавший шаг и есть ответ; дальше идти обычно бессмысленно, но
мы всё равно идём, чтобы за один прогон собрать полную картину.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..logging_setup import get_logger
from ..models import Camera, StreamProfile
from .mtx_client import MediaMTXClient, MediaMTXError, PathNotFound
from .probe import WEBRTC_AUDIO_CODECS, WEBRTC_VIDEO_CODECS, ProbeResult, probe_rtsp
from .ssrf import strip_credentials

log = get_logger("diagnose")

TCP_TIMEOUT = 6.0
PROBE_TIMEOUT = 15

#: Что MediaMTX умеет отдать в LL-HLS. Шире, чем WebRTC (H.265 сюда попадает),
#: но браузеры играют HEVC далеко не все — отсюда отдельная оговорка в выводе.
HLS_VIDEO_CODECS = frozenset({"h264", "h265", "hevc"})
HLS_AUDIO_CODECS = frozenset({"aac", "opus", "mp3"})

OK = "ok"
FAIL = "fail"
WARN = "warn"
SKIP = "skip"


@dataclass(slots=True)
class Step:
    key: str
    title: str
    state: str
    detail: str = ""
    hint: str = ""


@dataclass(slots=True)
class Diagnosis:
    steps: list[Step] = field(default_factory=list)
    probe: ProbeResult | None = None
    #: Сырой вывод ffprobe с вырезанным паролем — то, что просят приложить
    #: к обращению в поддержку.
    raw: str = ""
    verdict: str = ""
    verdict_state: str = OK

    def add(self, key: str, title: str, state: str, detail: str = "", hint: str = "") -> Step:
        step = Step(key=key, title=title, state=state, detail=detail, hint=hint)
        self.steps.append(step)
        return step

    @property
    def failed(self) -> list[Step]:
        return [s for s in self.steps if s.state == FAIL]


# ─── Отдельные проверки ──────────────────────────────────────────────────────
async def _check_dns(host: str) -> tuple[str, str, str]:
    """(состояние, что показать, подсказка)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=5
        )
    except TimeoutError:
        return FAIL, "DNS не ответил за 5 с", "проверьте DNS на сервере"
    except socket.gaierror as exc:
        return (
            FAIL,
            f"имя не разрешается: {exc.strerror or exc}",
            "опечатка в имени хоста либо DNS недоступен из контейнера",
        )
    addresses = sorted({info[4][0] for info in infos})
    return OK, ", ".join(addresses), ""


async def _check_tcp(host: str, port: int) -> tuple[str, str, str]:
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=TCP_TIMEOUT
        )
    except TimeoutError:
        return (
            FAIL,
            f"порт {port} не ответил за {TCP_TIMEOUT:.0f} с",
            "пакеты уходят в никуда: закрыт проброс на роутере камеры, "
            "либо сервер не выпускают на этот порт наружу",
        )
    except OSError as exc:
        return (
            FAIL,
            f"соединение не установлено: {exc.strerror or exc}",
            "хост отвечает, но порт закрыт — проверьте номер порта и проброс",
        )
    del reader
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    elapsed = (time.perf_counter() - started) * 1000
    return OK, f"порт {port} открыт, отклик {elapsed:.0f} мс", ""


def _classify_rtsp_error(message: str) -> str:
    """Человеческая подсказка по типовому сообщению ffprobe."""
    lowered = message.lower()
    if "401" in lowered or "unauthorized" in lowered:
        return (
            "камера ответила «401 Unauthorized» — неверный логин или пароль. "
            "Спецсимволы пароля перекодировать вручную не нужно, вставьте его как есть"
        )
    if "404" in lowered or "not found" in lowered:
        return (
            "камера ответила «404» — неверный путь потока. Уточните его в веб-интерфейсе "
            "камеры (у Hikvision это /Streaming/Channels/101, у Dahua — /cam/realmonitor?...)"
        )
    if "455" in lowered or "method not valid" in lowered:
        return "камера отвергла последовательность RTSP-запросов — попробуйте другой путь"
    if "461" in lowered or "unsupported transport" in lowered:
        return "камера не принимает RTSP поверх TCP — редкость, но встречается"
    if "connection refused" in lowered:
        return "порт открыт, но RTSP-сервера за ним нет"
    if "timed out" in lowered or "timeout" in lowered:
        return "камера приняла соединение, но не ответила на DESCRIBE"
    if "immediate exit requested" in lowered or "invalid data" in lowered:
        return "ответ не похож на RTSP — возможно, за этим портом другой сервис"
    return ""


async def _check_rtsp(url: str) -> tuple[str, str, str, ProbeResult, str]:
    """(состояние, деталь, подсказка, результат пробы, сырой лог).

    Сначала TCP — так поток надёжнее проходит через NAT и так же ходит
    MediaMTX. Если TCP не взлетел, пробуем UDP: разница в результате прямо
    указывает на причину.
    """
    result = await probe_rtsp(url, timeout=PROBE_TIMEOUT, transport="tcp")
    raw = f"$ ffprobe -rtsp_transport tcp\n{result.stderr or '(пусто)'}"

    if result.ok:
        return OK, "поток открыт по RTSP/TCP", "", result, raw

    over_udp = await probe_rtsp(url, timeout=PROBE_TIMEOUT, transport="udp")
    raw += f"\n\n$ ffprobe -rtsp_transport udp\n{over_udp.stderr or '(пусто)'}"

    if over_udp.ok:
        return (
            WARN,
            "по TCP камера не отдаёт поток, по UDP — отдаёт",
            "MediaMTX тянет камеру по TCP. Такую камеру нужно переводить в профиль "
            "«перекодировать» — ffmpeg умеет тянуть по UDP.",
            over_udp,
            raw,
        )

    detail = result.error or "поток не открылся"
    return FAIL, detail, _classify_rtsp_error(detail), result, raw


def _check_browser_compat(probe: ProbeResult, camera: Camera) -> list[tuple[str, str, str, str]]:
    """Что из этого потока браузер реально сможет показать."""
    steps: list[tuple[str, str, str, str]] = []
    transcoding = camera.profile is StreamProfile.transcode

    if transcoding:
        steps.append(
            ("webrtc", "WebRTC (WHEP)", OK,
             "включено перекодирование в H.264/Opus — WebRTC сыграет в любом браузере")
        )
        steps.append(
            ("hls", "LL-HLS (запасной транспорт)", OK, "H.264 играют все браузеры")
        )
        return steps

    video, audio = probe.video_codec, probe.audio_codec

    if video in WEBRTC_VIDEO_CODECS:
        webrtc_video = (OK, f"видео {video.upper()} проходит в WebRTC как есть")
    else:
        webrtc_video = (
            FAIL,
            f"видео {video.upper() or 'неизвестного кодека'} по WebRTC не проходит — "
            f"это и есть чёрный экран",
        )
    steps.append(("webrtc", "WebRTC (WHEP)", *webrtc_video))

    if video in HLS_VIDEO_CODECS and video not in ("h265", "hevc"):
        hls_state = (OK, f"видео {video.upper()} играют все браузеры")
    elif video in ("h265", "hevc"):
        hls_state = (
            WARN,
            "H.265 в LL-HLS отдаётся, но играет только Safari и часть Chrome "
            "с аппаратным декодером — в остальных браузерах будет чёрный экран",
        )
    else:
        hls_state = (FAIL, f"видео {video.upper() or 'неизвестного кодека'} в HLS не отдаётся")
    steps.append(("hls", "LL-HLS (запасной транспорт)", *hls_state))

    if camera.audio_enabled and audio:
        if audio in WEBRTC_AUDIO_CODECS:
            steps.append(("audio", "Звук", OK, f"{audio.upper()} проходит в WebRTC как есть"))
        else:
            steps.append(
                ("audio", "Звук", WARN,
                 f"{audio.upper()} по WebRTC не проходит. Само видео это не ломает: "
                 f"звук можно выключить в настройках камеры или включить перекодирование")
            )
    return steps


async def _check_mediamtx(camera: Camera, mtx: MediaMTXClient) -> tuple[str, str, str]:
    try:
        item = await mtx.get_active_path(camera.mtx_path)
    except PathNotFound:
        return (
            FAIL,
            "пути нет в MediaMTX",
            "камера выключена либо реконсилятор ещё не отработал — подождите "
            "15 секунд и повторите",
        )
    except MediaMTXError as exc:
        return FAIL, f"Control API недоступен: {exc}", "проверьте контейнер mediamtx"

    ready = bool(item.get("ready"))
    readers = len(item.get("readers") or [])
    tracks = item.get("tracks") or []
    received = int(item.get("bytesReceived") or 0)

    detail = (
        f"источник: {item.get('source', {}).get('type') if item.get('source') else 'нет'}; "
        f"дорожки: {', '.join(tracks) or 'нет'}; "
        f"принято {received / 1024 / 1024:.1f} МБ; зрителей: {readers}"
    )

    if ready:
        return OK, f"поток поднят. {detail}", ""
    if camera.on_demand and readers == 0:
        return (
            OK,
            f"путь создан, соединение с камерой закрыто до первого зрителя "
            f"(режим on-demand). {detail}",
            "",
        )
    return (
        FAIL,
        f"путь создан, но поток не поднимается. {detail}",
        "смотрите логи медиа-сервера: docker compose logs --tail=100 mediamtx",
    )


# ─── Сборка отчёта ───────────────────────────────────────────────────────────
async def diagnose(camera: Camera, rtsp_url: str, mtx: MediaMTXClient | None) -> Diagnosis:
    report = Diagnosis()
    masked = strip_credentials(rtsp_url)

    # 1. Что вообще будем открывать.
    parts = urlsplit(rtsp_url)
    if parts.path in ("", "/"):
        report.add(
            "url", "Адрес", WARN, masked,
            "в адресе нет пути потока. Часть камер отдаёт поток и так, но большинство "
            "требует явный путь вида /Streaming/Channels/101 или /cam/realmonitor?channel=1",
        )
    else:
        report.add("url", "Адрес", OK, masked)

    if not camera.is_enabled:
        report.add(
            "enabled", "Камера включена", FAIL, "камера выключена в настройках",
            "включите галочку «Камера включена» — пока она снята, путь в MediaMTX не создаётся",
        )

    # 2. Сеть.
    state, detail, hint = await _check_dns(camera.host)
    report.add("dns", "Имя хоста", state, detail, hint)
    if state == FAIL:
        report.verdict = "Сервис не может определить адрес камеры."
        report.verdict_state = FAIL
        return report

    state, detail, hint = await _check_tcp(camera.host, camera.port)
    report.add("tcp", f"TCP {camera.host}:{camera.port}", state, detail, hint)
    if state == FAIL:
        report.verdict = "До камеры нет сети: TCP-соединение не устанавливается."
        report.verdict_state = FAIL
        return report

    # 3. Сам RTSP.
    state, detail, hint, probe, raw = await _check_rtsp(rtsp_url)
    report.add("rtsp", "RTSP-соединение", state, detail, hint)
    report.probe = probe
    report.raw = raw

    if not probe.ok:
        report.verdict = "Камера доступна по сети, но поток не отдаёт."
        report.verdict_state = FAIL
        if mtx is not None:
            state, detail, hint = await _check_mediamtx(camera, mtx)
            report.add("mediamtx", "Состояние в MediaMTX", state, detail, hint)
        return report

    # 4. Кодеки и совместимость с браузером.
    geometry = f"{probe.width}×{probe.height}" if probe.width else "разрешение неизвестно"
    fps = f", {probe.fps:g} fps" if probe.fps else ""
    report.add(
        "codecs", "Кодеки", OK,
        f"видео {probe.video_codec.upper()} · {geometry}{fps}; "
        f"звук {probe.audio_codec.upper() or 'нет'}",
    )
    for key, title, compat_state, compat_detail in _check_browser_compat(probe, camera):
        report.add(key, title, compat_state, compat_detail)

    # 5. Что об этом думает медиа-сервер.
    if mtx is not None:
        state, detail, hint = await _check_mediamtx(camera, mtx)
        report.add("mediamtx", "Состояние в MediaMTX", state, detail, hint)

    _summarize(report, camera)
    return report


def _summarize(report: Diagnosis, camera: Camera) -> None:
    failed = report.failed
    if not failed:
        warnings = [s for s in report.steps if s.state == WARN]
        if warnings:
            report.verdict_state = WARN
            report.verdict = "Поток идёт, но есть замечания: " + "; ".join(
                s.title.lower() for s in warnings
            )
        else:
            report.verdict = "Всё в порядке: поток открывается и совместим с браузером."
        return

    report.verdict_state = FAIL
    if any(s.key == "webrtc" for s in failed) and camera.profile is StreamProfile.passthrough:
        report.verdict = (
            "Камера работает, но её кодек браузер не покажет. Включите профиль "
            "«Перекодировать в H.264/Opus» — это единственное рабочее решение "
            "для таких камер."
        )
        return
    report.verdict = "Не работает: " + "; ".join(s.title.lower() for s in failed)
