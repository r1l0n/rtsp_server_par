"""Проверка RTSP-URL перед добавлением камеры.

Пользователь панели вводит произвольный URL, а сервер идёт по нему сам. Без
проверок сервис превращается в сканер внутренней сети и в способ достать
метаданные облака (169.254.169.254). Поэтому:

* разрешены только схемы rtsp/rtsps;
* хост резолвится, и ВСЕ полученные адреса должны быть публичными;
* строка URL проходит жёсткий фильтр символов — при профиле transcode она
  попадает в аргументы ffmpeg, запускаемого MediaMTX, и пробел или кавычка там
  означают подмену аргументов команды.

Остаточный риск: DNS rebinding — между нашей проверкой и подключением MediaMTX
имя может переехать на приватный адрес. Закрывается правилами egress на
хосте, см. docs/security.md.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from ..config import IPNetwork, get_settings

ALLOWED_SCHEMES = ("rtsp", "rtsps")
DEFAULT_PORT = 554

#: RFC 3986 без кавычек, обратного слэша, пробелов и управляющих символов.
_SAFE_URL_CHARS = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+$")


class UnsafeCameraUrl(ValueError):
    """URL камеры не проходит проверку безопасности."""


@dataclass(frozen=True, slots=True)
class CameraTarget:
    #: Полный URL с учётными данными — шифруется и больше нигде не появляется.
    url: str
    host: str
    port: int
    has_credentials: bool
    resolved_ips: tuple[str, ...]
    #: Безопасное представление для UI и логов: пароль вырезан.
    display_url: str


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Публично маршрутизируемый адрес?

    Основа — `is_global`: он закрывает и приватные сети, и loopback, и CGNAT
    (100.64.0.0/10), и документационные диапазоны, которые по отдельности легко
    забыть перечислить. Но у IPv4 `is_global` пропускает multicast
    (224.0.0.0/4), поэтому его исключаем отдельно.
    """
    return address.is_global and not address.is_multicast and not address.is_reserved


def _in_allowlist(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, networks: tuple[IPNetwork, ...]
) -> bool:
    return any(address in network for network in networks)


async def _resolve(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeCameraUrl(f"не удалось разрешить имя «{host}»: {exc.strerror or exc}") from exc
    return tuple(dict.fromkeys(info[4][0] for info in infos))


async def validate_rtsp_url(raw: str) -> CameraTarget:
    raw = raw.strip()
    if not raw:
        raise UnsafeCameraUrl("пустой URL")
    if len(raw) > 2048:
        raise UnsafeCameraUrl("URL слишком длинный")

    if not _SAFE_URL_CHARS.match(raw):
        raise UnsafeCameraUrl(
            "URL содержит недопустимые символы (пробел, кавычка, обратный слэш или "
            "управляющий символ). Спецсимволы в логине и пароле нужно записать в "
            "процентной кодировке, например '@' как %40"
        )

    parts = urlsplit(raw)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeCameraUrl("поддерживаются только схемы rtsp:// и rtsps://")

    try:
        host = parts.hostname
        port = parts.port or DEFAULT_PORT
    except ValueError as exc:  # некорректный порт
        raise UnsafeCameraUrl(f"некорректный адрес: {exc}") from exc

    if not host:
        raise UnsafeCameraUrl("в URL не указан хост")
    if not 1 <= port <= 65535:
        raise UnsafeCameraUrl("некорректный порт")

    settings = get_settings()

    # Если в URL сразу указан IP — проверяем его без обращения к DNS.
    resolved = await _resolve(host, port)
    if not resolved:
        raise UnsafeCameraUrl(f"имя «{host}» никуда не разрешается")

    if not settings.allow_private_camera_hosts:
        allowlist = settings.camera_allowlist_networks
        for item in resolved:
            address = ipaddress.ip_address(item)
            if _is_public(address) or _in_allowlist(address, allowlist):
                continue
            raise UnsafeCameraUrl(
                f"адрес {item} находится в приватном диапазоне. Камеры во внутренней "
                f"сети разрешаются только через CAMERA_HOST_ALLOWLIST"
            )

    return CameraTarget(
        url=raw,
        host=host,
        port=port,
        has_credentials=bool(parts.username),
        resolved_ips=resolved,
        display_url=strip_credentials(raw),
    )


def strip_credentials(url: str) -> str:
    """rtsp://user:pass@host:554/s -> rtsp://user:***@host:554/s"""
    parts = urlsplit(url)
    if not parts.username:
        return url
    user = quote(unquote(parts.username), safe="")
    host = parts.hostname or ""
    netloc = f"{user}:***@{host}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
