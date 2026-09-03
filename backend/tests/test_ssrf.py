"""Проверки защиты от SSRF при добавлении камеры."""

from __future__ import annotations

import pytest

from app.media import ssrf
from app.media.ssrf import UnsafeCameraUrl, strip_credentials, validate_rtsp_url


@pytest.fixture
def resolve_to(monkeypatch: pytest.MonkeyPatch):
    """Подменяет DNS: тесты не должны зависеть от сети."""

    def _apply(*addresses: str):
        async def fake_resolve(host: str, port: int) -> tuple[str, ...]:
            return tuple(addresses)

        monkeypatch.setattr(ssrf, "_resolve", fake_resolve)

    return _apply


async def test_public_address_allowed(resolve_to) -> None:
    resolve_to("31.148.246.249")
    target = await validate_rtsp_url("rtsp://user:pass@31.148.246.249:4259/stream")
    assert target.host == "31.148.246.249"
    assert target.port == 4259
    assert target.has_credentials


async def test_default_port_is_554(resolve_to) -> None:
    resolve_to("31.148.246.249")
    target = await validate_rtsp_url("rtsp://camera.example.com/live")
    assert target.port == 554
    assert not target.has_credentials


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",       # loopback
        "10.1.2.3",        # частная сеть
        "192.168.0.10",    # частная сеть
        "172.16.5.4",      # частная сеть
        "169.254.169.254", # метаданные облака
        "100.64.0.1",      # CGNAT
        "0.0.0.0",         # неопределённый  # noqa: S104
        "203.0.113.5",     # документационный диапазон
        "224.0.0.1",       # multicast
        "::1",             # loopback IPv6
        "fd00::1",         # unique local IPv6
    ],
)
async def test_private_addresses_blocked(resolve_to, address: str) -> None:
    resolve_to(address)
    host = f"[{address}]" if ":" in address else address
    with pytest.raises(UnsafeCameraUrl, match="приватн"):
        await validate_rtsp_url(f"rtsp://{host}:554/stream")


async def test_blocked_when_any_resolved_address_is_private(resolve_to) -> None:
    """Имя может резолвиться в несколько адресов — хватает одного приватного."""
    resolve_to("203.0.113.5", "10.0.0.7")
    with pytest.raises(UnsafeCameraUrl):
        await validate_rtsp_url("rtsp://camera.example.com/stream")


async def test_private_allowed_when_explicitly_enabled(resolve_to, monkeypatch) -> None:
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "allow_private_camera_hosts", True)
    resolve_to("192.168.1.50")
    target = await validate_rtsp_url("rtsp://192.168.1.50/stream")
    assert target.host == "192.168.1.50"


@pytest.mark.parametrize(
    "url",
    [
        "http://203.0.113.5/stream",
        "file:///etc/passwd",
        "rtmp://203.0.113.5/live",
        "https://203.0.113.5/stream",
    ],
)
async def test_only_rtsp_schemes_allowed(url: str) -> None:
    with pytest.raises(UnsafeCameraUrl, match="схем"):
        await validate_rtsp_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "rtsp://user:pa ss@203.0.113.5/s",     # пробел
        'rtsp://203.0.113.5/s"',               # кавычка
        "rtsp://203.0.113.5/s'",               # одинарная кавычка
        "rtsp://203.0.113.5/s\\x",             # обратный слэш
        "rtsp://203.0.113.5/s\nx",             # перевод строки
    ],
)
async def test_argument_injection_characters_rejected(url: str) -> None:
    """URL попадает в аргументы ffmpeg — пробелы и кавычки недопустимы."""
    with pytest.raises(UnsafeCameraUrl, match="символ"):
        await validate_rtsp_url(url)


async def test_empty_url_rejected() -> None:
    with pytest.raises(UnsafeCameraUrl):
        await validate_rtsp_url("   ")


def test_strip_credentials_hides_password() -> None:
    masked = strip_credentials("rtsp://admin:s3cret@203.0.113.5:4259/stream")
    assert "s3cret" not in masked
    assert masked == "rtsp://admin:***@203.0.113.5:4259/stream"


def test_strip_credentials_keeps_url_without_credentials() -> None:
    url = "rtsp://203.0.113.5:554/stream"
    assert strip_credentials(url) == url
