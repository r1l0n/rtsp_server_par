"""Хеширование паролей — argon2id."""

from __future__ import annotations

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Параметры выше дефолтных argon2-cffi: сервис не логинит тысячи людей в секунду,
# ~64 МБ и 3 прохода — комфортный компромисс (около 50-80 мс на проверку).
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)

MIN_PASSWORD_LENGTH = 12


class WeakPasswordError(ValueError):
    """Пароль не проходит политику."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except (UnicodeEncodeError, ValueError):
        # Хеш в БД повреждён или не является хешем argon2 (например, остался
        # от другой схемы). Это не повод ронять форму входа с 500.
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """True, если хеш сделан старыми параметрами и его пора пересчитать."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def validate_password_policy(password: str, *, email: str = "") -> None:
    """Минимальная политика. Длина важнее «спецсимвола в верхнем регистре»."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")
    if len(password) > 1024:
        raise WeakPasswordError("пароль слишком длинный")
    if len(set(password)) < 5:
        raise WeakPasswordError("пароль слишком однообразный")
    local_part = email.split("@")[0].lower()
    if local_part and len(local_part) >= 4 and local_part in password.lower():
        raise WeakPasswordError("пароль не должен содержать имя учётной записи")
    if re.fullmatch(r"[0-9]+", password):
        raise WeakPasswordError("пароль не должен состоять только из цифр")
    return None
