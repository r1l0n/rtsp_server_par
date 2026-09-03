"""Диагностика камеры: классификация ошибок и вывод о совместимости.

Сетевые шаги здесь не проверяются — они и есть обращение к сети. Проверяется
то, ради чего диагностика существует: превращение сырого сообщения ffprobe и
набора кодеков в понятный оператору ответ «почему чёрный экран».
"""

from __future__ import annotations

import pytest

from app.media.diagnose import (
    FAIL,
    OK,
    WARN,
    Diagnosis,
    _check_browser_compat,
    _classify_rtsp_error,
    _sdp_notes,
    _summarize,
)
from app.media.probe import ProbeResult, _meaningful_error
from app.models import Camera, StreamProfile


def _camera(**kwargs) -> Camera:
    defaults = {
        "name": "Проходная",
        "host": "203.0.113.5",
        "port": 554,
        "mtx_path": "abcdefgh12345678abcdefgh",
        "profile": StreamProfile.passthrough,
        "on_demand": True,
        "audio_enabled": True,
        "is_enabled": True,
    }
    return Camera(**{**defaults, **kwargs})


def _probe(video: str = "h264", audio: str = "") -> ProbeResult:
    from app.media.probe import _interpret

    streams = [{"codec_type": "video", "codec_name": video, "r_frame_rate": "25/1"}]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": audio})
    return _interpret(streams)


# ─── Классификация ответа камеры ─────────────────────────────────────────────
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("method DESCRIBE failed: 401 Unauthorized", "логин"),
        ("method DESCRIBE failed: 404 Not Found", "путь потока"),
        ("Connection refused", "RTSP-сервера"),
        ("Connection timed out", "DESCRIBE"),
        ("461 Unsupported transport", "TCP"),
    ],
)
def test_typical_camera_answers_get_a_hint(message: str, expected: str) -> None:
    assert expected in _classify_rtsp_error(message)


def test_unknown_error_gets_no_invented_hint() -> None:
    """Лучше молчать, чем уводить оператора выдуманной подсказкой."""
    assert _classify_rtsp_error("something entirely unexpected") == ""


# ─── Выбор содержательной строки из вывода ffprobe ───────────────────────────
def test_meaningful_error_skips_trailing_noise() -> None:
    """Настоящая причина у ffprobe идёт перед статистикой, а не последней."""
    lines = [
        "Opening 'rtsp://admin:***@203.0.113.5/live'",
        "method DESCRIBE failed: 401 Unauthorized",
        "Statistics: 0 bytes read, 0 seeks",
    ]
    assert _meaningful_error(lines) == "method DESCRIBE failed: 401 Unauthorized"


def test_meaningful_error_survives_empty_output() -> None:
    assert _meaningful_error([]) == "неизвестная ошибка"


# ─── Совместимость с браузером ───────────────────────────────────────────────
def test_h264_passes_both_transports() -> None:
    steps = dict((key, state) for key, _title, state, _detail in _check_browser_compat(
        _probe("h264"), _camera()
    ))
    assert steps["webrtc"] == OK
    assert steps["hls"] == OK


def test_h265_fails_webrtc_and_only_warns_for_hls() -> None:
    """Ровно тот случай, который выглядит как «чёрный экран без ошибок»."""
    steps = dict((key, state) for key, _title, state, _detail in _check_browser_compat(
        _probe("hevc"), _camera()
    ))
    assert steps["webrtc"] == FAIL
    assert steps["hls"] == WARN


def test_transcoding_makes_everything_compatible() -> None:
    steps = dict((key, state) for key, _title, state, _detail in _check_browser_compat(
        _probe("hevc"), _camera(profile=StreamProfile.transcode)
    ))
    assert steps["webrtc"] == OK
    assert steps["hls"] == OK


def test_aac_audio_only_warns() -> None:
    """Несовместимый звук не должен читаться как «видео не работает»."""
    steps = dict((key, state) for key, _title, state, _detail in _check_browser_compat(
        _probe("h264", "aac"), _camera()
    ))
    assert steps["webrtc"] == OK
    assert steps["audio"] == WARN


def test_audio_is_not_checked_when_disabled() -> None:
    keys = [key for key, _title, _state, _detail in _check_browser_compat(
        _probe("h264", "aac"), _camera(audio_enabled=False)
    )]
    assert "audio" not in keys


# ─── Разбор SDP: чем эта камера неудобна строгому клиенту ────────────────────
#
# Реальный случай: ffprobe и VLC камеру открывают, MediaMTX — нет. Разница
# видна только в SDP, который ffprobe печатает целиком, поэтому подсказку
# собираем прямо из его вывода.
SDP_LOG = """
[rtsp @ 0x1] SDP:
v=0
o=- 1001 1 IN IP4 192.168.44.34
m=video 0 RTP/AVP 105
a=control:rtsp://192.168.44.34/media/video1/video
a=rtpmap:105 H264/90000
m=application 0 RTP/AVP 107
a=control:rtsp://192.168.44.34/media/video1/metadata
a=rtpmap:107 vnd.onvif.metadata/90000
Unsupported codec with id 0 for input stream 1
"""


def test_sdp_notes_spot_onvif_metadata_track() -> None:
    assert "ONVIF" in _sdp_notes(SDP_LOG)


def test_sdp_notes_spot_private_control_address() -> None:
    notes = _sdp_notes(SDP_LOG)
    assert "192.168.44.34" in notes
    assert "a=control" in notes


@pytest.mark.parametrize("host", ["10.0.0.5", "172.20.1.1", "192.168.1.2", "127.0.0.1"])
def test_sdp_notes_cover_every_private_range(host: str) -> None:
    assert host in _sdp_notes(f"a=control:rtsp://{host}/live")


def test_sdp_notes_stay_quiet_on_a_healthy_sdp() -> None:
    """Пустая подсказка лучше выдуманной: иначе оператор пойдёт чинить исправное."""
    healthy = "a=control:rtsp://203.0.113.5/live\na=rtpmap:96 H264/90000"
    assert _sdp_notes(healthy) == ""


# ─── Итоговый вердикт ────────────────────────────────────────────────────────
def test_verdict_recommends_transcoding_when_codec_is_the_problem() -> None:
    report = Diagnosis()
    report.add("rtsp", "RTSP-соединение", OK)
    report.add("webrtc", "WebRTC (WHEP)", FAIL)
    _summarize(report, _camera())

    assert report.verdict_state == FAIL
    assert "Перекодировать" in report.verdict


def test_verdict_is_clean_when_nothing_failed() -> None:
    report = Diagnosis()
    report.add("rtsp", "RTSP-соединение", OK)
    report.add("webrtc", "WebRTC (WHEP)", OK)
    _summarize(report, _camera())

    assert report.verdict_state == OK
    assert "порядке" in report.verdict


def test_verdict_recommends_transcoding_when_mediamtx_cannot_pull() -> None:
    """ffprobe камеру открыл, а медиа-сервер нет — обход тот же, транскод."""
    report = Diagnosis()
    report.add("rtsp", "RTSP-соединение", OK)
    report.add("webrtc", "WebRTC (WHEP)", OK)
    report.add("stream", "MediaMTX тянет поток", FAIL)
    _summarize(report, _camera())

    assert report.verdict_state == FAIL
    assert "Перекодировать" in report.verdict


def test_warnings_do_not_read_as_failure() -> None:
    report = Diagnosis()
    report.add("rtsp", "RTSP-соединение", OK)
    report.add("audio", "Звук", WARN)
    _summarize(report, _camera())

    assert report.verdict_state == WARN
    assert not report.failed
