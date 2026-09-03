"""Пошаговая диагностика камеры.

Отдельный модуль, а не расширение probe.py, потому что задача другая. Проба
отвечает на вопрос «какие кодеки» и крутится в фоне у worker'а. Диагностика
отвечает на вопрос «почему не показывает» и запускается оператором вручную из
панели — она обязана дать связный отчёт, а не одну строку ошибки.

Порядок шагов повторяет путь пакета:

    камера → TCP → RTSP → декодер → MediaMTX → отдача в браузер

Ключевой шаг здесь — не ffprobe, а «MediaMTX тянет поток». ffprobe и VLC
прощают камере многое: пропускают неизвестные дорожки, чинят неверные адреса
в SDP. MediaMTX строже, поэтому «в VLC работает, а тут чёрный экран» — самая
частая жалоба, и различить эти два случая можно, только заставив MediaMTX
реально подключиться и посмотреть, пошли ли байты.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from ..config import get_settings
from ..logging_setup import get_logger
from ..models import Camera, StreamProfile
from .mtx_client import MediaMTXClient, MediaMTXError, PathNotFound
from .paths import build_path_conf
from .probe import WEBRTC_AUDIO_CODECS, WEBRTC_VIDEO_CODECS, ProbeResult, probe_rtsp
from .snapshot import grab_frame
from .ssrf import strip_credentials

log = get_logger("diagnose")

TCP_TIMEOUT = 5.0
PROBE_TIMEOUT = 12
FRAME_TIMEOUT = 15
#: Сколько ждём, что путь поднимется после того, как мы его разбудили.
STREAM_WAIT = 18

#: Что MediaMTX умеет отдать в LL-HLS. Шире, чем WebRTC (H.265 сюда попадает),
#: но браузеры играют HEVC далеко не все — отсюда отдельная оговорка в выводе.
HLS_VIDEO_CODECS = frozenset({"h264", "h265", "hevc"})

OK = "ok"
FAIL = "fail"
WARN = "warn"


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
    #: JPEG первого кадра, если его удалось получить. Картинка — самое
    #: убедительное доказательство, что до камеры дошли и поток декодируется.
    frame: bytes | None = None
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


# ─── Сеть ────────────────────────────────────────────────────────────────────
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


# ─── RTSP ────────────────────────────────────────────────────────────────────
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


#: Приватные префиксы, которые камеры любят подставлять в a=control.
_PRIVATE_PREFIXES = (
    "192.168.",
    "10.",
    "127.",
    "169.254.",
    *(f"172.{octet}." for octet in range(16, 32)),
)


def _sdp_notes(ffprobe_log: str) -> str:
    """Особенности SDP, из-за которых MediaMTX спотыкается там, где VLC нет.

    ffprobe печатает SDP камеры целиком — грех этим не воспользоваться. Обе
    проверки ниже взяты из реальных поломок: строгий RTSP-клиент делает SETUP
    всех объявленных дорожек и идёт по адресу из a=control, а лояльные ffmpeg
    и VLC и то и другое обходят.
    """
    notes: list[str] = []
    if "vnd.onvif.metadata" in ffprobe_log:
        notes.append(
            "камера объявляет рядом с видео служебную дорожку ONVIF-метаданных — "
            "ffprobe её пропускает («Unsupported codec»), строгий клиент обязан "
            "сделать по ней SETUP и может на этом сорваться"
        )
    for line in ffprobe_log.splitlines():
        stripped = line.strip()
        if not stripped.startswith("a=control:rtsp://"):
            continue
        host = urlsplit(stripped[len("a=control:") :]).hostname or ""
        if host.startswith(_PRIVATE_PREFIXES):
            notes.append(
                f"в SDP камера указывает себя по внутреннему адресу {host} (a=control), "
                f"снаружи он недостижим"
            )
            break
    return "; ".join(notes)


# ─── Совместимость с браузером ───────────────────────────────────────────────
def _check_browser_compat(probe: ProbeResult, camera: Camera) -> list[tuple[str, str, str, str]]:
    """Что из этого потока браузер реально сможет показать."""
    steps: list[tuple[str, str, str, str]] = []

    if camera.profile is StreamProfile.transcode:
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


# ─── MediaMTX ────────────────────────────────────────────────────────────────
async def _check_path_config(
    camera: Camera, rtsp_url: str, mtx: MediaMTXClient
) -> tuple[str, str, str]:
    """Путь принят медиа-сервером?

    Заодно пересоздаём его: если Control API отверг конфигурацию — а раньше об
    этом знал только лог worker'а — оператор увидит текст отказа здесь.
    """
    try:
        await mtx.upsert_path(camera.mtx_path, build_path_conf(camera, rtsp_url))
    except MediaMTXError as exc:
        return (
            FAIL,
            f"Control API отверг конфигурацию пути: {exc}",
            "чаще всего это несовместимая версия образа mediamtx — сверьте набор "
            "ключей в mediamtx/mediamtx.yml с тегом образа в docker-compose.yml",
        )
    return OK, f"путь {camera.mtx_path} настроен", ""


async def _pull_playlist(url: str) -> tuple[int | None, str]:
    """Запрос плейлиста напрямую к MediaMTX, в обход Caddy.

    Он же будит источник: у пути в режиме on-demand соединение с камерой
    открывается только при появлении читателя.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(STREAM_WAIT)) as client:
            response = await client.get(url)
            return response.status_code, response.text[:200]
    except httpx.HTTPError as exc:
        return None, str(exc)[:200]


async def _check_stream(camera: Camera, mtx: MediaMTXClient) -> list[tuple[str, str, str, str]]:
    """Заставляем MediaMTX подключиться к камере и смотрим, что вышло.

    Главный шаг диагностики. Без него камера в режиме on-demand всегда выглядит
    здоровой: путь создан, читателей нет — и провала не видно до тех пор, пока
    его не увидит зритель.
    """
    settings = get_settings()
    playlist_url = f"{settings.mtx_hls_url.rstrip('/')}/{camera.mtx_path}/index.m3u8"
    puller = asyncio.create_task(_pull_playlist(playlist_url))

    item: dict[str, object] | None = None
    api_error = ""
    deadline = time.monotonic() + STREAM_WAIT

    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            try:
                item = await mtx.get_active_path(camera.mtx_path)
            except PathNotFound:
                item = None
                continue
            except MediaMTXError as exc:
                api_error = str(exc)
                break
            if item.get("ready"):
                break

        try:
            status, body = await asyncio.wait_for(asyncio.shield(puller), timeout=3)
        except TimeoutError:
            status, body = None, "плейлист не отдан за отведённое время"
    finally:
        puller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await puller

    if api_error:
        return [("stream", "MediaMTX тянет поток", FAIL, f"Control API недоступен: {api_error}")]
    if item is None:
        return [("stream", "MediaMTX тянет поток", FAIL,
                 "путь исчез из медиа-сервера — он его не принял")]

    tracks = [str(track) for track in (item.get("tracks") or [])]  # type: ignore[union-attr]
    received = int(item.get("bytesReceived") or 0)  # type: ignore[arg-type]
    summary = f"дорожки: {', '.join(tracks) or 'нет'}; принято {received / 1048576:.2f} МБ"

    steps: list[tuple[str, str, str, str]] = []
    if item.get("ready") and received > 0:
        steps.append(("stream", "MediaMTX тянет поток", OK, summary))
    elif item.get("ready"):
        steps.append(("stream", "MediaMTX тянет поток", WARN,
                      f"путь готов, но данные ещё не пошли. {summary}"))
    else:
        steps.append(("stream", "MediaMTX тянет поток", FAIL,
                      f"за {STREAM_WAIT} с поток не поднялся. {summary}"))

    if status == 200:
        steps.append(("delivery", "Отдача в браузер", OK,
                      "плейлист LL-HLS отдаётся медиа-сервером"))
    elif status is None:
        steps.append(("delivery", "Отдача в браузер", FAIL,
                      f"медиа-сервер не отдал плейлист: {body}"))
    else:
        steps.append(("delivery", "Отдача в браузер", FAIL,
                      f"медиа-сервер ответил {status} на запрос плейлиста"))
    return steps


def _stream_hint(state: str, ffprobe_log: str) -> str:
    """Подсказка к провалу на стороне медиа-сервера.

    Самый непрозрачный для оператора случай: ffprobe камеру открыл, а MediaMTX
    нет. Значит клиенты разошлись в трактовке ответа камеры, и полезнее всего
    указать на конкретные особенности именно этого SDP.
    """
    if state == OK:
        return ""
    base = (
        "ffprobe эту камеру открывает, а медиа-сервер — нет. Точная причина в его "
        "логе: docker compose logs --tail=200 mediamtx. Обходится профилем "
        "«Перекодировать в H.264/Opus» — там поток тянет ffmpeg"
    )
    notes = _sdp_notes(ffprobe_log)
    return f"{base}. Что подозрительно в SDP камеры: {notes}" if notes else base


# ─── Сборка отчёта ───────────────────────────────────────────────────────────
async def diagnose(camera: Camera, rtsp_url: str, mtx: MediaMTXClient | None) -> Diagnosis:
    report = Diagnosis()
    masked = strip_credentials(rtsp_url)

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

    state, detail, hint, probe, raw = await _check_rtsp(rtsp_url)
    report.add("rtsp", "RTSP-соединение", state, detail, hint)
    report.probe = probe
    report.raw = raw

    if not probe.ok:
        report.verdict = "Камера доступна по сети, но поток не отдаёт."
        report.verdict_state = FAIL
        return report

    geometry = f"{probe.width}×{probe.height}" if probe.width else "разрешение неизвестно"
    fps = f", {probe.fps:g} fps" if probe.fps else ""
    report.add(
        "codecs", "Кодеки", OK,
        f"видео {probe.video_codec.upper()} · {geometry}{fps}; "
        f"звук {probe.audio_codec.upper() or 'нет'}",
    )

    frame, frame_error = await grab_frame(rtsp_url, timeout=FRAME_TIMEOUT)
    report.frame = frame
    report.add(
        "frame", "Кадр с камеры", OK if frame else WARN,
        "получен — снимок ниже" if frame else f"кадр не получен: {frame_error}",
        "" if frame else "поток открывается, но декодировать его не удалось",
    )

    for key, title, compat_state, compat_detail in _check_browser_compat(probe, camera):
        report.add(key, title, compat_state, compat_detail)

    if mtx is not None:
        state, detail, hint = await _check_path_config(camera, rtsp_url, mtx)
        report.add("mtxconf", "Путь в MediaMTX", state, detail, hint)
        if state == OK:
            for key, title, stream_state, stream_detail in await _check_stream(camera, mtx):
                report.add(
                    key, title, stream_state, stream_detail,
                    _stream_hint(stream_state, raw) if key == "stream" else "",
                )

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
    keys = {s.key for s in failed}

    if "webrtc" in keys and camera.profile is StreamProfile.passthrough:
        report.verdict = (
            "Камера работает, но её кодек браузер не покажет. Включите профиль "
            "«Перекодировать в H.264/Opus» — это единственное рабочее решение "
            "для таких камер."
        )
        return
    if "stream" in keys and camera.profile is StreamProfile.passthrough:
        report.verdict = (
            "Камера отдаёт поток, но медиа-сервер его не поднимает — они расходятся "
            "в трактовке ответа камеры. Включите профиль «Перекодировать в "
            "H.264/Opus»: там поток тянет ffmpeg, который к таким камерам терпимее."
        )
        return
    report.verdict = "Не работает: " + "; ".join(s.title.lower() for s in failed)
