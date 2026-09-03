"""Проверки защиты от SSRF при добавлении камеры."""

from __future__ import annotations

import pytest

from app.media import ssrf
from app.media.ssrf import (
    UnsafeCameraUrl,
    normalize_rtsp_url,
    strip_credentials,
    validate_rtsp_url,
)


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


# ─── Нормализация учётных данных ─────────────────────────────────────────────
# Спецсимволы в паролях камер — норма, а не экзотика. Пока их требовалось
# кодировать вручную, самым частым отчётом было «в VLC работает, а здесь нет».
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Восклицательный знак: urlsplit его переживает, а Go в MediaMTX — нет.
        ("rtsp://admin:Pass1!@203.0.113.5:4259",
         "rtsp://admin:Pass1%21@203.0.113.5:4259"),
        # «@» в пароле: хост определяется по последнему собачьему,
        # за которым идёт что-то похожее на host[:port].
        ("rtsp://admin:p@ss@203.0.113.5:554/live",
         "rtsp://admin:p%40ss@203.0.113.5:554/live"),
        # «/» в пароле: первый слэш попадает внутрь пароля, а не в путь.
        ("rtsp://admin:pa/ss@203.0.113.5/live",
         "rtsp://admin:pa%2Fss@203.0.113.5/live"),
        # «#» иначе обрезал бы весь остаток URL как фрагмент.
        ("rtsp://admin:p#ss@203.0.113.5/live",
         "rtsp://admin:p%23ss@203.0.113.5/live"),
        # Одинокий процент — невалидная escape-последовательность для Go.
        ("rtsp://admin:100%@203.0.113.5/live",
         "rtsp://admin:100%25@203.0.113.5/live"),
        # Двоеточие в пароле: делим userinfo по ПЕРВОМУ двоеточию.
        ("rtsp://admin:a:b@203.0.113.5/live",
         "rtsp://admin:a%3Ab@203.0.113.5/live"),
        ("rtsp://CAM.Example.COM/live", "rtsp://cam.example.com/live"),
    ],
)
def test_credentials_are_percent_encoded(raw: str, expected: str) -> None:
    normalized, changed = normalize_rtsp_url(raw)
    assert normalized == expected
    assert changed is True


@pytest.mark.parametrize(
    "url",
    [
        "rtsp://admin:already%40encoded@203.0.113.5/live",
        "rtsp://admin:simple@203.0.113.5:554/live",
        "rtsp://203.0.113.5:42590",
        "rtsp://user:pass@[2001:db8::1]:8554/s",
    ],
)
def test_canonical_urls_are_left_alone(url: str) -> None:
    """Нормализация идемпотентна: уже корректный URL не должен меняться."""
    normalized, changed = normalize_rtsp_url(url)
    assert normalized == url
    assert changed is False


def test_normalization_is_stable_on_second_pass() -> None:
    once, _ = normalize_rtsp_url("rtsp://admin:p@ss!@203.0.113.5/live")
    twice, changed = normalize_rtsp_url(once)
    assert twice == once
    assert changed is False


async def test_encoded_password_survives_validation(resolve_to) -> None:
    resolve_to("31.148.246.249")
    target = await validate_rtsp_url("rtsp://admin:Pass1!@31.148.246.249:4259/live")
    assert target.url == "rtsp://admin:Pass1%21@31.148.246.249:4259/live"
    assert target.host == "31.148.246.249"
    assert target.port == 4259
    assert target.path == "/live"
    assert target.normalized is True
    assert "Pass1" not in target.display_url


def test_strip_credentials_hides_password() -> None:
    masked = strip_credentials("rtsp://admin:s3cret@203.0.113.5:4259/stream")
    assert "s3cret" not in masked
    assert masked == "rtsp://admin:***@203.0.113.5:4259/stream"


def test_strip_credentials_keeps_url_without_credentials() -> None:
    url = "rtsp://203.0.113.5:554/stream"
    assert strip_credentials(url) == url


def test_strip_credentials_works_inside_an_error_message() -> None:
    """Сюда прилетает вывод ffprobe, а не аккуратный URL.

    Раньше маскировка шла через urlsplit и на такой строке не срабатывала —
    пароль камеры уезжал прямо на экран диагностики.
    """
    masked = strip_credentials(
        "rtsp://admin:s3cret@203.0.113.5:554/live: server returned 401 Unauthorized"
    )
    assert "s3cret" not in masked
    assert "401 Unauthorized" in masked
