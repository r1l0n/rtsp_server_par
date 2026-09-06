"""ONVIF Profile S — универсальный драйвер.

SOAP-конверты собираются здесь вручную, без `zeep`/`onvif-zeep`. Причина
простая: нам нужны ровно четыре операции (`GetSystemDateAndTime`,
`GetCapabilities`, `GetProfiles`, `ContinuousMove`/`Stop`), а `zeep` тянет за
собой `lxml` (C-расширение в slim-образе) и синхронный `requests`, который в
асинхронном приложении пришлось бы уводить в поток. Шаблоны ниже занимают
меньше места, чем обвязка вокруг библиотеки.

Две ловушки, из-за которых ONVIF «не работает при верном пароле»:

1. **Часы.** UsernameToken подписывает время создания, и камера отвергает
   подпись, если её собственные часы ушли. Поэтому перед первым обращением
   спрашиваем у камеры её время (этот запрос идёт БЕЗ аутентификации) и
   дальше подставляем в подпись время камеры, а не своё.
2. **XAddr с внутренним адресом.** Камера честно сообщает адрес своей PTZ-
   службы — и сплошь и рядом это `192.168.x.x`, потому что за NAT она о себе
   больше ничего не знает. Ходить по нему нельзя: он либо никуда не ведёт,
   либо ведёт в чужую сеть. Берём из XAddr только путь, а хост и порт
   подставляем свои. Ровно та же болезнь, что у `a=control` в SDP, которую
   диагностика уже умеет объяснять.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import math
import secrets
from typing import ClassVar
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from .base import DeviceInfo, PtzError, PtzUnsupported, Target, Vector
from .transport import ensure_digest, request

DEVICE_PATH = "/onvif/device_service"
MEDIA_PATH = "/onvif/media_service"
PTZ_PATH = "/onvif/ptz_service"

_MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
_PTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
_DEVICE_NS = "http://www.onvif.org/ver10/device/wsdl"
_SCHEMA_NS = "http://www.onvif.org/ver10/schema"
_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_DIGEST_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
_B64_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)
_SOAP_CT = "application/soap+xml; charset=utf-8"

#: Дальше этого тело ответа не читаем — защита от бесконечного потока.
_MAX_BODY = 256_000


# ─── Сборка запроса ──────────────────────────────────────────────────────────
def password_digest(password: str, nonce: bytes, created: str) -> str:
    """Base64(SHA1(nonce + created + password)) — WS-UsernameToken Profile 1.0."""
    digest = hashlib.sha1(  # noqa: S324 - алгоритм задан спецификацией ONVIF
        nonce + created.encode("utf-8") + password.encode("utf-8")
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def security_header(username: str, password: str, nonce: bytes, created: str) -> str:
    return (
        f'<wsse:Security xmlns:wsse="{_WSSE}" xmlns:wsu="{_WSU}">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{escape(username)}</wsse:Username>"
        f'<wsse:Password Type="{_DIGEST_TYPE}">'
        f"{password_digest(password, nonce, created)}</wsse:Password>"
        f'<wsse:Nonce EncodingType="{_B64_TYPE}">'
        f"{base64.b64encode(nonce).decode('ascii')}</wsse:Nonce>"
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security>"
    )


def envelope(body: str, header: str = "") -> str:
    head = f"<s:Header>{header}</s:Header>" if header else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f"{head}<s:Body>{body}</s:Body></s:Envelope>"
    )


def duration(seconds: float) -> str:
    """xs:duration. Дробные секунды переваривают не все камеры — округляем."""
    return f"PT{max(1, math.ceil(seconds))}S"


def continuous_move_body(profile_token: str, velocity: Vector, seconds: float) -> str:
    return (
        f'<tptz:ContinuousMove xmlns:tptz="{_PTZ_NS}" xmlns:tt="{_SCHEMA_NS}">'
        f"<tptz:ProfileToken>{escape(profile_token)}</tptz:ProfileToken>"
        "<tptz:Velocity>"
        f'<tt:PanTilt x="{velocity.pan:.2f}" y="{velocity.tilt:.2f}"/>'
        f'<tt:Zoom x="{velocity.zoom:.2f}"/>'
        "</tptz:Velocity>"
        f"<tptz:Timeout>{duration(seconds)}</tptz:Timeout>"
        "</tptz:ContinuousMove>"
    )


def stop_body(profile_token: str) -> str:
    return (
        f'<tptz:Stop xmlns:tptz="{_PTZ_NS}">'
        f"<tptz:ProfileToken>{escape(profile_token)}</tptz:ProfileToken>"
        "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>"
        "</tptz:Stop>"
    )


# ─── Разбор ответа ───────────────────────────────────────────────────────────
def _localname(tag: str) -> str:
    """Имя без неймспейса: вендоры называют префиксы как им вздумается."""
    return tag.rpartition("}")[2]


def _parse(xml: str) -> ET.Element:
    try:
        # ElementTree не раскрывает внешние сущности и не обрабатывает DTD,
        # а тело ограничено по размеру ещё до разбора.
        return ET.fromstring(xml)  # noqa: S314
    except ET.ParseError as exc:
        raise PtzUnsupported(f"камера ответила не по ONVIF: {exc}") from exc


def _find(root: ET.Element, name: str) -> ET.Element | None:
    for element in root.iter():
        if _localname(element.tag) == name:
            return element
    return None


def fault_text(xml: str) -> str:
    """Человеческая часть SOAP-Fault. Пусто — камера отказала молча."""
    if not xml.lstrip().startswith("<"):
        return ""
    try:
        root = _parse(xml)
    except PtzUnsupported:
        return ""
    if _find(root, "Fault") is None:
        return ""
    for name in ("Text", "faultstring", "Value"):
        node = _find(root, name)
        if node is not None and node.text and node.text.strip():
            return node.text.strip()[:200]
    return ""


def rewrite_xaddr(xaddr: str) -> str:
    """Оставляет от объявленного камерой адреса только путь.

    `http://192.168.1.64/onvif/PTZ` → `/onvif/PTZ`. Возвращается путь, а не
    полный URL: ходим мы всегда на тот хост камеры, который прошёл проверку
    SSRF, а не на тот, который камера назвала сама.
    """
    path = urlsplit(xaddr).path if "://" in xaddr else xaddr
    return path or PTZ_PATH


def parse_profiles(xml: str) -> list[tuple[str, bool]]:
    """[(токен профиля, есть ли у него PTZ-конфигурация)] в порядке ответа."""
    root = _parse(xml)
    profiles: list[tuple[str, bool]] = []
    for element in root.iter():
        if _localname(element.tag) != "Profiles":
            continue
        token = element.get("token", "")
        if not token:
            continue
        has_ptz = any(_localname(child.tag) == "PTZConfiguration" for child in element.iter())
        profiles.append((token, has_ptz))
    return profiles


def pick_profile(profiles: list[tuple[str, bool]]) -> str:
    """Первый профиль с PTZ; если таких нет — просто первый."""
    for token, has_ptz in profiles:
        if has_ptz:
            return token
    return profiles[0][0] if profiles else ""


def parse_ptz_xaddr(xml: str) -> str:
    """XAddr службы PTZ из GetCapabilitiesResponse. Пусто — PTZ у камеры нет."""
    root = _parse(xml)
    for element in root.iter():
        if _localname(element.tag) != "PTZ":
            continue
        xaddr = _find(element, "XAddr")
        if xaddr is not None and xaddr.text:
            return xaddr.text.strip()
    return ""


def parse_camera_time(xml: str) -> dt.datetime | None:
    """UTC-время камеры из GetSystemDateAndTimeResponse."""
    root = _parse(xml)
    utc = _find(root, "UTCDateTime")
    if utc is None:
        return None
    date, time = _find(utc, "Date"), _find(utc, "Time")
    if date is None or time is None:
        return None

    def part(parent: ET.Element, name: str) -> int | None:
        node = _find(parent, name)
        if node is None or not node.text:
            return None
        try:
            return int(node.text)
        except ValueError:
            return None

    year, month, day = part(date, "Year"), part(date, "Month"), part(date, "Day")
    hour, minute, second = part(time, "Hour"), part(time, "Minute"), part(time, "Second")
    if None in (year, month, day, hour, minute, second):
        return None
    try:
        return dt.datetime(
            int(year or 0), int(month or 0), int(day or 0),
            int(hour or 0), int(minute or 0), int(second or 0), tzinfo=dt.UTC,
        )
    except ValueError:
        return None


# ─── Драйвер ─────────────────────────────────────────────────────────────────
class OnvifDriver:
    name: ClassVar[str] = "onvif"

    @property
    def stops_itself(self) -> bool:
        # ContinuousMove/Timeout — часть спецификации: камера встанет сама.
        return True

    async def _call(self, target: Target, path: str, body: str) -> str:
        created = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=target.clock_skew)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        header = security_header(
            target.username, target.password, secrets.token_bytes(16), created
        )
        response = await request(
            target,
            "POST",
            path,
            content=envelope(body, header),
            headers={"Content-Type": _SOAP_CT},
        )
        ensure_digest(response, target)
        text = response.text[:_MAX_BODY]
        if response.status_code >= 400:
            raise PtzError(fault_text(text) or f"камера ответила {response.status_code}")
        return text

    async def _clock_skew(self, target: Target) -> float:
        """Расхождение часов камеры с нашими. Запрос намеренно без пароля."""
        response = await request(
            target,
            "POST",
            DEVICE_PATH,
            content=envelope(f'<tds:GetSystemDateAndTime xmlns:tds="{_DEVICE_NS}"/>'),
            headers={"Content-Type": _SOAP_CT},
            authenticate=False,
        )
        if response.status_code >= 400:
            return 0.0
        camera_time = parse_camera_time(response.text[:_MAX_BODY])
        if camera_time is None:
            return 0.0
        return (camera_time - dt.datetime.now(dt.UTC)).total_seconds()

    async def identify(self, target: Target) -> DeviceInfo:
        skew = await self._clock_skew(target)
        target = dataclasses.replace(target, clock_skew=skew)

        capabilities = await self._call(
            target,
            DEVICE_PATH,
            f'<tds:GetCapabilities xmlns:tds="{_DEVICE_NS}">'
            "<tds:Category>All</tds:Category></tds:GetCapabilities>",
        )
        xaddr = parse_ptz_xaddr(capabilities)
        if not xaddr:
            raise PtzUnsupported("камера отвечает по ONVIF, но службы PTZ у неё нет")

        profiles = parse_profiles(
            await self._call(target, MEDIA_PATH, f'<trt:GetProfiles xmlns:trt="{_MEDIA_NS}"/>')
        )
        token = pick_profile(profiles)
        if not token:
            raise PtzUnsupported("камера не отдала ни одного профиля ONVIF")

        return DeviceInfo(
            driver=self.name,
            has_ptz=True,
            profile_token=token,
            service_path=rewrite_xaddr(xaddr),
            clock_skew=skew,
            detail="ONVIF Profile S",
        )

    async def move(self, target: Target, velocity: Vector, seconds: float) -> None:
        await self._call(
            target,
            target.service_path or PTZ_PATH,
            continuous_move_body(target.profile_token, velocity, seconds),
        )

    async def stop(self, target: Target) -> None:
        await self._call(
            target, target.service_path or PTZ_PATH, stop_body(target.profile_token)
        )
