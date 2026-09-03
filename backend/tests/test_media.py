"""Конфигурация путей MediaMTX, разбор пробы и логика реконсиляции."""

from __future__ import annotations

import re

from app.media.paths import build_path_conf, new_mtx_path
from app.media.probe import _interpret
from app.media.reconciler import _differs
from app.models import Camera, StreamProfile


def _camera(**kwargs) -> Camera:
    defaults = {
        "name": "Проходная",
        "mtx_path": "abcdefgh12345678abcdefgh",
        "profile": StreamProfile.passthrough,
        "on_demand": True,
        "audio_enabled": True,
    }
    return Camera(**{**defaults, **kwargs})


# ─── Имена путей ─────────────────────────────────────────────────────────────
def test_mtx_path_matches_caddy_matcher() -> None:
    """Caddyfile пропускает только [a-z0-9]{8,64} — имена обязаны совпадать."""
    pattern = re.compile(r"^[a-z0-9]{8,64}$")
    for _ in range(50):
        assert pattern.match(new_mtx_path())


def test_mtx_paths_are_unique() -> None:
    assert len({new_mtx_path() for _ in range(500)}) == 500


# ─── Конфигурация путей ──────────────────────────────────────────────────────
def test_passthrough_pulls_camera_over_tcp() -> None:
    conf = build_path_conf(_camera(), "rtsp://203.0.113.5/s")
    assert conf["source"] == "rtsp://203.0.113.5/s"
    assert conf["rtspTransport"] == "tcp"
    assert conf["sourceOnDemand"] is True
    assert conf["record"] is False
    assert "runOnDemand" not in conf


def test_always_on_camera_keeps_source_open() -> None:
    conf = build_path_conf(_camera(on_demand=False), "rtsp://203.0.113.5/s")
    assert conf["sourceOnDemand"] is False


def test_transcode_publishes_back_to_loopback() -> None:
    camera = _camera(profile=StreamProfile.transcode)
    conf = build_path_conf(camera, "rtsp://203.0.113.5/s")

    assert conf["source"] == "publisher"
    assert conf["runOnDemandRestart"] is True
    command = conf["runOnDemand"]
    assert command.startswith("ffmpeg ")
    assert "-c:v libx264" in command
    assert "-c:a libopus" in command
    # Публикация строго на loopback: RTSP-сервер MediaMTX слушает только его.
    assert command.endswith(f"rtsp://127.0.0.1:8554/{camera.mtx_path}")


def test_transcode_without_audio_drops_audio_track() -> None:
    camera = _camera(profile=StreamProfile.transcode, audio_enabled=False)
    command = build_path_conf(camera, "rtsp://203.0.113.5/s")["runOnDemand"]
    assert " -an " in command
    assert "libopus" not in command


# ─── Сравнение состояний ─────────────────────────────────────────────────────
def test_identical_config_is_not_a_change() -> None:
    wanted = build_path_conf(_camera(), "rtsp://203.0.113.5/s")
    current = {**wanted, "someOtherMediaMtxDefault": 123}
    assert _differs(current, wanted) is False


def test_changed_source_is_detected() -> None:
    wanted = build_path_conf(_camera(), "rtsp://203.0.113.5/new")
    current = build_path_conf(_camera(), "rtsp://203.0.113.5/old")
    assert _differs(current, wanted) is True


def test_switch_to_transcode_is_detected() -> None:
    wanted = build_path_conf(_camera(profile=StreamProfile.transcode), "rtsp://203.0.113.5/s")
    current = build_path_conf(_camera(), "rtsp://203.0.113.5/s")
    assert _differs(current, wanted) is True


# ─── Разбор ffprobe ──────────────────────────────────────────────────────────
def test_h264_with_g711_needs_no_transcode() -> None:
    result = _interpret(
        [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
             "r_frame_rate": "25/1"},
            {"codec_type": "audio", "codec_name": "pcm_alaw"},
        ]
    )
    assert result.ok
    assert result.video_ok and result.audio_ok
    assert result.recommended_profile == StreamProfile.passthrough.value
    assert result.fps == 25.0
    assert result.height == 1080


def test_h265_requires_transcode() -> None:
    result = _interpret(
        [{"codec_type": "video", "codec_name": "hevc", "width": 2560, "height": 1440,
          "r_frame_rate": "20/1"}]
    )
    assert not result.video_ok
    assert result.recommended_profile == StreamProfile.transcode.value
    assert any("WebRTC" in note for note in result.notes)


def test_aac_audio_is_flagged() -> None:
    result = _interpret(
        [
            {"codec_type": "video", "codec_name": "h264", "r_frame_rate": "30/1"},
            {"codec_type": "audio", "codec_name": "aac"},
        ]
    )
    assert result.video_ok
    assert not result.audio_ok


def test_stream_without_video_is_an_error() -> None:
    result = _interpret([{"codec_type": "audio", "codec_name": "aac"}])
    assert not result.ok
    assert "видеодорожк" in result.error


def test_broken_frame_rate_does_not_crash() -> None:
    result = _interpret([{"codec_type": "video", "codec_name": "h264", "r_frame_rate": "0/0"}])
    assert result.fps == 0.0
