"""Проверка и нормализация RTSP-URL перед добавлением камеры.

Пользователь панели вводит произвольный URL, а сервер идёт по нему сам. Без
проверок сервис превращается в сканер внутренней сети и в способ достать
метаданные облака (169.254.169.254). Поэтому:

* разрешены только схемы rtsp/rtsps;
* хост резолвится, и ВСЕ полученные адреса должны быть публичными;
* итоговая строка URL проходит жёсткий фильтр символов — при профиле transcode
  она попадает в аргументы ffmpeg, запускаемого MediaMTX, и пробел или кавычка
  там означают подмену аргументов команды.

Отдельная задача — учётные данные. Пароли камер сплошь и рядом содержат
спецсимволы (`!`, `#`, `@`, `/`, `%`), и требовать от оператора записывать их
в процентной кодировке вручную — прямой путь к «в VLC работает, а тут нет».
Поэтому URL нормализуется: логин и пароль перекодируются автоматически, и
дальше по системе (БД, MediaMTX, ffprobe, ffmpeg) ходит канонический URL.

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
from urllib.parse import quote, unquote, urlsplit

from ..config import IPNetwork, get_settings

ALLOWED_SCHEMES = ("rtsp", "rtsps")
DEFAULT_PORT = 554

#: RFC 3986 без кавычек, обратного слэша, пробелов и управляющих символов.
_SAFE_URL_CHARS = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+$")

#: Символы, которых не должно быть в строке ни в каком виде: они либо ломают
#: разбор, либо (пробел, кавычка) позволяют подменить аргументы команды
#: ffmpeg. Их мы не «чиним» молча, а отклоняем с объяснением.
_FORBIDDEN_RAW = re.compile(r"[\x00-\x20\x7f\"'\\`|<>{}^]")

#: host[:port] — то, чем обязан оказаться кусок URL сразу после «@».
_HOSTPORT = re.compile(
    r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?::\d{1,5})?$"
)

#: Маска «схема://логин:пароль@» в произвольной строке — в том числе в
#: середине сообщения об ошибке от ffprobe, а не только в аккуратном URL.
_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^/@\s:]+):[^/@\s]*@")


class UnsafeCameraUrl(ValueError):
    """URL камеры не проходит проверку безопасности."""


@dataclass(frozen=True, slots=True)
class CameraTarget:
    #: Полный нормализованный URL с учётными данными — шифруется и больше
    #: нигде не появляется.
    url: str
    host: str
    port: int
    path: str
    has_credentials: bool
    resolved_ips: tuple[str, ...]
    #: Безопасное представление для UI и логов: пароль вырезан.
    display_url: str
    #: Пришлось ли перекодировать логин или пароль. Показываем оператору,
    #: чтобы изменившийся адрес не выглядел магией.
    normalized: bool = False


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


# ─── Нормализация ────────────────────────────────────────────────────────────
def _percent_encode(part: str) -> str:
    """Кодирует один элемент userinfo, не удваивая уже готовую кодировку.

    unquote → quote идемпотентно для корректных escape-последовательностей
    (`%40` так и остаётся `%40`), но чинит всё остальное: `!` → `%21`,
    `@` → `%40`, одинокий `%` → `%25`.
    """
    return quote(unquote(part), safe="")


def _split_userinfo(rest: str) -> tuple[str, str]:
    """Делит «[userinfo@]host[:port][/path]» на userinfo и остаток.

    Пароли содержат и `@`, и `/`, поэтому нельзя просто взять первый `@` или
    обрезать по первому `/`. Идём от последнего `@` к первому и берём первый
    вариант, у которого хвост действительно похож на host[:port] — так
    корректно разбираются и `p@ss`, и `pa/ss`.
    """
    for at in range(len(rest) - 1, -1, -1):
        if rest[at] != "@":
            continue
        tail = rest[at + 1 :]
        hostport = re.split(r"[/?]", tail, maxsplit=1)[0]
        if hostport and _HOSTPORT.match(hostport):
            return rest[:at], tail
    return "", rest


def _split_authority(authority: str) -> tuple[str, str]:
    """«host», «host:port», «[::1]:port» -> (host, port|'')."""
    if authority.startswith("["):
        close = authority.find("]")
        host, remainder = authority[: close + 1], authority[close + 1 :]
        return host, remainder[1:] if remainder.startswith(":") else ""
    host, colon, port = authority.rpartition(":")
    return (host, port) if colon else (authority, "")


def normalize_rtsp_url(raw: str) -> tuple[str, bool]:
    """Приводит URL к каноническому виду. Возвращает (url, менялся ли он).

    Проверяет схему, вытаскивает логин и пароль как есть и перекодирует их в
    проценты, приводит хост к нижнему регистру. Путь и query остаются
    нетронутыми: спецсимволы там редки, а случайно «починить» рабочий путь
    с `%2F` было бы хуже, чем ничего не делать.
    """
    raw = raw.strip()
    if not raw:
        raise UnsafeCameraUrl("пустой URL")
    if len(raw) > 2048:
        raise UnsafeCameraUrl("URL слишком длинный")

    bad = _FORBIDDEN_RAW.search(raw)
    if bad is not None:
        raise UnsafeCameraUrl(
            f"URL содержит недопустимый символ {bad.group()!r}: пробел, кавычка, обратный "
            f"слэш или управляющий символ. Остальные спецсимволы логина и пароля "
            f"кодировать вручную не нужно — сервис делает это сам"
        )

    scheme, separator, rest = raw.partition("://")
    if not separator or scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeCameraUrl("поддерживаются только схемы rtsp:// и rtsps://")
    scheme = scheme.lower()

    userinfo, hostpart = _split_userinfo(rest)

    cut = min((i for i in (hostpart.find("/"), hostpart.find("?")) if i >= 0), default=-1)
    authority, tail = (hostpart, "") if cut < 0 else (hostpart[:cut], hostpart[cut:])
    if not authority:
        raise UnsafeCameraUrl("в URL не указан хост")
    if not _HOSTPORT.match(authority):
        raise UnsafeCameraUrl(f"не удалось разобрать адрес камеры: «{authority}»")

    host, port_text = _split_authority(authority)
    host = host.lower()

    credentials = ""
    if userinfo:
        user, has_password, password = userinfo.partition(":")
        credentials = _percent_encode(user)
        if has_password:
            credentials = f"{credentials}:{_percent_encode(password)}"
        credentials += "@"

    normalized = f"{scheme}://{credentials}{host}{f':{port_text}' if port_text else ''}{tail}"

    if not _SAFE_URL_CHARS.match(normalized):
        raise UnsafeCameraUrl("URL содержит символы, недопустимые в адресе камеры")

    return normalized, normalized != raw


async def validate_rtsp_url(raw: str) -> CameraTarget:
    normalized, changed = normalize_rtsp_url(raw)

    parts = urlsplit(normalized)
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
        url=normalized,
        host=host,
        port=port,
        path=parts.path,
        has_credentials=bool(parts.username),
        resolved_ips=resolved,
        display_url=strip_credentials(normalized),
        normalized=changed,
    )


def strip_credentials(text: str) -> str:
    """rtsp://user:pass@host:554/s -> rtsp://user:***@host:554/s

    Работает по произвольной строке, а не только по корректному URL: чаще
    всего сюда прилетает сообщение ffprobe, внутри которого URL с паролем.
    Именно поэтому здесь регулярка, а не urlsplit — urlsplit на строке
    «Server returned 401 for rtsp://a:b@host» пароль бы не заметил.
    """
    return _CREDENTIALS.sub(lambda m: f"{m['scheme']}{m['user']}:***@", text)
