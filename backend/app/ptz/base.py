"""Общие типы драйверов PTZ.

Пульт у зрителя предельно простой — четыре стрелки и зум, — а вот протоколов
за ними четыре, и они друг на друга не похожи: ONVIF это SOAP, Hikvision
кладёт XML в PUT, Dahua и Axis обходятся параметрами в query. Поэтому весь
разнобой заперт в драйверах, а наружу торчат ровно два действия: «двигайся
в эту сторону не дольше N секунд» и «стой».

Ограничение по времени в `move` — не украшение, а способ не оставить камеру
крутящейся, если браузер зрителя умер посреди нажатия. Драйверы, у которых
протокол умеет такой таймаут сам (ONVIF, Hikvision), передают его камере;
для остальных за этим следит `service.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final, Protocol

#: Направления, которые понимает пульт. Ровно то, что нарисовано на кнопках.
DIRECTIONS: Final = ("left", "right", "up", "down", "zoom_in", "zoom_out")

#: Доля от максимальной скорости камеры. На полной скорости прицелиться
#: невозможно: одно нажатие уводит кадр далеко за край.
DEFAULT_SPEED: Final = 0.6


class PtzError(RuntimeError):
    """Камера не приняла команду."""


class PtzUnsupported(PtzError):
    """Камеру не удалось опознать или она не умеет поворачиваться."""


class PtzAuthError(PtzError):
    """Камера не приняла логин или пароль."""


@dataclass(frozen=True, slots=True)
class Vector:
    """Скорость по трём осям, -1.0…1.0. Положительный tilt — вверх."""

    pan: float = 0.0
    tilt: float = 0.0
    zoom: float = 0.0

    @classmethod
    def from_direction(cls, name: str) -> Vector:
        try:
            return _VECTORS[name]
        except KeyError:
            raise ValueError(f"неизвестное направление: {name!r}") from None

    @property
    def is_zero(self) -> bool:
        return self.pan == 0.0 and self.tilt == 0.0 and self.zoom == 0.0


_VECTORS: Final[dict[str, Vector]] = {
    "left": Vector(pan=-DEFAULT_SPEED),
    "right": Vector(pan=DEFAULT_SPEED),
    "up": Vector(tilt=DEFAULT_SPEED),
    "down": Vector(tilt=-DEFAULT_SPEED),
    "zoom_in": Vector(zoom=DEFAULT_SPEED),
    "zoom_out": Vector(zoom=-DEFAULT_SPEED),
}


@dataclass(frozen=True, slots=True)
class Target:
    """Куда и под кем идти. Собирается из камеры в `service.py`."""

    host: str
    port: int
    tls: bool
    username: str
    password: str
    #: Номер канала, 1-based (см. комментарий у Camera.ptz_channel).
    channel: int = 1
    #: Кэш ONVIF: токен профиля и путь службы PTZ.
    profile_token: str = ""
    service_path: str = ""
    #: На сколько секунд часы камеры расходятся с нашими. ONVIF отвергает
    #: UsernameToken с чужим временем, и это самая частая причина «пароль
    #: верный, а ONVIF не работает».
    clock_skew: float = 0.0

    @property
    def origin(self) -> str:
        return f"{'https' if self.tls else 'http'}://{self.host}:{self.port}"

    def url(self, path: str) -> str:
        return f"{self.origin}{path}"


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Что удалось выяснить о камере при опознании."""

    driver: str
    model: str = ""
    has_ptz: bool = True
    profile_token: str = ""
    service_path: str = ""
    clock_skew: float = 0.0
    detail: str = ""


class Driver(Protocol):
    """Драйвер одного протокола управления."""

    name: ClassVar[str]

    async def identify(self, target: Target) -> DeviceInfo:
        """Это моя камера? Иначе PtzUnsupported."""
        ...

    async def move(self, target: Target, velocity: Vector, seconds: float) -> None: ...

    async def stop(self, target: Target) -> None: ...

    @property
    def stops_itself(self) -> bool:
        """Умеет ли протокол сам остановить камеру по истечении времени."""
        ...
