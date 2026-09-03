"""Второй фактор: TOTP (RFC 6238) и одноразовые коды восстановления."""

from __future__ import annotations

import hmac
import io
import secrets
import time
import uuid

import pyotp
import qrcode
from qrcode.image.svg import SvgPathImage

from .passwords import hash_password, verify_password

STEP_SECONDS = 30
#: Принимаем код текущего окна и по одному соседнему в каждую сторону —
#: компенсация расхождения часов телефона.
VALID_WINDOW = 1
RECOVERY_CODE_COUNT = 10
_REPLAY_TTL = STEP_SECONDS * (VALID_WINDOW * 2 + 2)


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str, issuer: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def qr_svg(uri: str) -> str:
    """QR-код в виде инлайнового SVG.

    Именно SVG, а не PNG: не тянем Pillow в образ и не нарушаем CSP
    директивой img-src data: для растровой картинки.
    """
    img = qrcode.make(uri, image_factory=SvgPathImage, box_size=10, border=2)
    buffer = io.BytesIO()
    img.save(buffer)
    return buffer.getvalue().decode("utf-8")


async def verify_code(secret: str, code: str, user_id: uuid.UUID | str) -> bool:
    """Проверяет TOTP-код с защитой от повторного использования.

    Без этой защиты подсмотренный код остаётся валидным до конца окна —
    достаточно, чтобы им воспользовался кто-то ещё.
    """
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False

    from ..redis_client import get_redis

    totp = pyotp.TOTP(secret)
    now = int(time.time())
    for offset in range(-VALID_WINDOW, VALID_WINDOW + 1):
        moment = now + offset * STEP_SECONDS
        if hmac.compare_digest(totp.at(moment), code):
            step = moment // STEP_SECONDS
            key = f"totp_used:{user_id}:{step}"
            # SET NX: первый, кто предъявил код в этом окне, его и «сжигает».
            first_use = await get_redis().set(key, "1", ex=_REPLAY_TTL, nx=True)
            return bool(first_use)
    return False


# ─── Коды восстановления ─────────────────────────────────────────────────────
def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Коды вида 'a1b2c-3d4e5'. Показываются пользователю ровно один раз."""
    codes: list[str] = []
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"  # без похожих символов
    for _ in range(count):
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_recovery_code(code: str) -> str:
    return hash_password(normalize_recovery_code(code))


def verify_recovery_code(code_hash: str, code: str) -> bool:
    return verify_password(code_hash, normalize_recovery_code(code))


def normalize_recovery_code(code: str) -> str:
    return code.strip().lower().replace(" ", "")
