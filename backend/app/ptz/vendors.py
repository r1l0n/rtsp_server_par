"""Родные протоколы Hikvision, Dahua и Axis.

Все три живут в одном файле намеренно: каждый занимает по два десятка строк,
и разносить их по отдельным модулям — значит трижды повторить одни и те же
импорты ради иллюзии структуры. ONVIF вынесен отдельно, потому что он на
порядок сложнее.

Почему эти драйверы вообще нужны, если есть универсальный ONVIF: у Hikvision
и Dahua ONVIF на заводских настройках выключен, а после включения нередко
требует отдельного пользователя. Родной же API работает ровно с теми
учётными данными, которые оператор уже вписал в RTSP-ссылку. Поэтому при
автоопределении родной протокол пробуется раньше ONVIF.
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlencode

from .base import DeviceInfo, PtzError, PtzUnsupported, Target, Vector
from .transport import ensure_digest, request

#: Тело ответа опознания дальше этого не читаем.
_MAX_BODY = 64_000


def _clamp(value: float, scale: int) -> int:
    """Нормализованную скорость -1…1 — в вендорскую шкалу -scale…scale."""
    return max(-scale, min(scale, round(value * scale)))


# ─── Hikvision ISAPI ─────────────────────────────────────────────────────────
class HikvisionDriver:
    name: ClassVar[str] = "hikvision"

    @property
    def stops_itself(self) -> bool:
        # /momentary принимает длительность и останавливает камеру сам.
        return True

    def body(self, velocity: Vector, seconds: float) -> str:
        """PTZData со скоростями -100…100 и длительностью в миллисекундах."""
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<PTZData version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            f"<pan>{_clamp(velocity.pan, 100)}</pan>"
            f"<tilt>{_clamp(velocity.tilt, 100)}</tilt>"
            f"<zoom>{_clamp(velocity.zoom, 100)}</zoom>"
            f"<Momentary><duration>{max(1, round(seconds * 1000))}</duration></Momentary>"
            "</PTZData>"
        )

    def path(self, target: Target, *, momentary: bool) -> str:
        tail = "momentary" if momentary else "continuous"
        return f"/ISAPI/PTZCtrl/channels/{target.channel}/{tail}"

    async def identify(self, target: Target) -> DeviceInfo:
        response = await request(target, "GET", "/ISAPI/System/deviceInfo")
        ensure_digest(response, target)
        text = response.text[:_MAX_BODY]
        if response.status_code >= 400 or "<DeviceInfo" not in text:
            raise PtzUnsupported("камера не отвечает по ISAPI")
        model = ""
        start = text.find("<model>")
        if start >= 0:
            model = text[start + 7 : text.find("</model>", start)].strip()[:80]
        return DeviceInfo(driver=self.name, model=model, detail="Hikvision ISAPI")

    async def move(self, target: Target, velocity: Vector, seconds: float) -> None:
        response = await request(
            target,
            "PUT",
            self.path(target, momentary=True),
            content=self.body(velocity, seconds),
            headers={"Content-Type": "application/xml"},
        )
        if response.status_code >= 400:
            raise PtzError(f"камера ответила {response.status_code} на команду поворота")

    async def stop(self, target: Target) -> None:
        # Нулевые скорости по continuous — штатный способ остановки ISAPI.
        await request(
            target,
            "PUT",
            self.path(target, momentary=False),
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<PTZData version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
                "<pan>0</pan><tilt>0</tilt><zoom>0</zoom></PTZData>"
            ),
            headers={"Content-Type": "application/xml"},
        )


# ─── Dahua CGI ───────────────────────────────────────────────────────────────
class DahuaDriver:
    name: ClassVar[str] = "dahua"

    @property
    def stops_itself(self) -> bool:
        # Только пара start/stop: за остановкой следит сторож в service.py.
        return False

    def code(self, velocity: Vector) -> str:
        if velocity.zoom > 0:
            return "ZoomTele"
        if velocity.zoom < 0:
            return "ZoomWide"
        if velocity.tilt > 0:
            return "Up"
        if velocity.tilt < 0:
            return "Down"
        if velocity.pan > 0:
            return "Right"
        return "Left"

    def query(self, target: Target, velocity: Vector, action: str) -> str:
        """У Dahua каналы нумеруются с нуля, у всех остальных — с единицы.

        Оператор везде видит номер как в RTSP-пути (1-based), поэтому единицу
        вычитаем здесь, а не заставляем его помнить про исключение. Ошибка в
        этом месте выглядит как «поворачивается не та камера регистратора».
        """
        speed = max(1, abs(_clamp(velocity.pan or velocity.tilt or velocity.zoom, 8)))
        return "/cgi-bin/ptz.cgi?" + urlencode(
            {
                "action": action,
                "channel": max(0, target.channel - 1),
                "code": self.code(velocity),
                "arg1": 0,
                "arg2": speed,
                "arg3": 0,
            }
        )

    async def identify(self, target: Target) -> DeviceInfo:
        response = await request(
            target, "GET", "/cgi-bin/magicBox.cgi?action=getDeviceType"
        )
        ensure_digest(response, target)
        text = response.text[:_MAX_BODY]
        if response.status_code >= 400 or "type=" not in text:
            raise PtzUnsupported("камера не отвечает по Dahua CGI")
        return DeviceInfo(
            driver=self.name,
            model=text.partition("type=")[2].strip()[:80],
            detail="Dahua CGI",
        )

    async def move(self, target: Target, velocity: Vector, seconds: float) -> None:
        response = await request(target, "GET", self.query(target, velocity, "start"))
        if response.status_code >= 400:
            raise PtzError(f"камера ответила {response.status_code} на команду поворота")
        # Направление запоминается сторожем: остановить Dahua можно только той
        # же командой с action=stop, «стоп вообще» протокол не умеет.
        _LAST_DIRECTION[(target.host, target.port, target.channel)] = velocity

    async def stop(self, target: Target) -> None:
        velocity = _LAST_DIRECTION.pop((target.host, target.port, target.channel), None)
        if velocity is None:
            return
        await request(target, "GET", self.query(target, velocity, "stop"))


#: Последнее направление на камеру — нужно только Dahua (см. stop()).
_LAST_DIRECTION: dict[tuple[str, int, int], Vector] = {}


# ─── Axis VAPIX ──────────────────────────────────────────────────────────────
class AxisDriver:
    name: ClassVar[str] = "axis"

    @property
    def stops_itself(self) -> bool:
        return False

    def query(self, target: Target, velocity: Vector) -> str:
        params: dict[str, str | int] = {"camera": target.channel}
        if velocity.pan or velocity.tilt:
            params["continuouspantiltmove"] = (
                f"{_clamp(velocity.pan, 100)},{_clamp(velocity.tilt, 100)}"
            )
        if velocity.zoom:
            params["continuouszoommove"] = str(_clamp(velocity.zoom, 100))
        if velocity.is_zero:
            params["continuouspantiltmove"] = "0,0"
            params["continuouszoommove"] = "0"
        return "/axis-cgi/com/ptz.cgi?" + urlencode(params)

    async def identify(self, target: Target) -> DeviceInfo:
        response = await request(
            target, "GET", "/axis-cgi/param.cgi?action=list&group=Brand"
        )
        ensure_digest(response, target)
        text = response.text[:_MAX_BODY]
        if response.status_code >= 400 or "Brand=AXIS" not in text:
            raise PtzUnsupported("камера не отвечает по VAPIX")
        model = ""
        for line in text.splitlines():
            if line.startswith("root.Brand.ProdNbr="):
                model = line.partition("=")[2].strip()[:80]
        return DeviceInfo(driver=self.name, model=model, detail="Axis VAPIX")

    async def move(self, target: Target, velocity: Vector, seconds: float) -> None:
        response = await request(target, "GET", self.query(target, velocity))
        if response.status_code >= 400:
            raise PtzError(f"камера ответила {response.status_code} на команду поворота")

    async def stop(self, target: Target) -> None:
        await request(target, "GET", self.query(target, Vector()))
