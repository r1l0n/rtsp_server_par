"""Управление обзором камеры (PTZ)."""

from .base import DIRECTIONS, DeviceInfo, PtzAuthError, PtzError, PtzUnsupported, Target, Vector
from .service import Result, detect, press, release, store_detection

__all__ = [
    "DIRECTIONS",
    "DeviceInfo",
    "PtzAuthError",
    "PtzError",
    "PtzUnsupported",
    "Result",
    "Target",
    "Vector",
    "detect",
    "press",
    "release",
    "store_detection",
]
