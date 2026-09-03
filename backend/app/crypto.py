"""Шифрование учётных данных камер.

RTSP-URL содержит логин и пароль от камеры — единственный по-настоящему
чувствительный секрет в этой БД. Храним его зашифрованным libsodium SecretBox
(XSalsa20-Poly1305): дамп базы без ключа бесполезен.

Формат blob: [1 байт версии][nonce (24) + ciphertext + tag]. Версия нужна,
чтобы ротация ключа (`python -m app.cli rotate-key`) могла различать записи.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import secrets

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox

CURRENT_VERSION = 1


class DecryptionError(RuntimeError):
    """Blob не расшифровывается текущим ключом."""


class SecretCipher:
    """Обёртка над SecretBox с версионированием формата."""

    def __init__(self, key: bytes) -> None:
        self._box = SecretBox(key)

    def encrypt(self, plaintext: str) -> bytes:
        return bytes([CURRENT_VERSION]) + self._box.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, blob: bytes) -> str:
        if not blob:
            raise DecryptionError("пустой blob")
        version = blob[0]
        if version != CURRENT_VERSION:
            raise DecryptionError(f"неизвестная версия формата: {version}")
        try:
            return self._box.decrypt(blob[1:]).decode("utf-8")
        except CryptoError as exc:
            raise DecryptionError(
                "не удалось расшифровать: неверный ключ или повреждён blob"
            ) from exc


def generate_key() -> str:
    """Новый ключ шифрования в base64 — для `cli gen-key`."""
    return base64.b64encode(secrets.token_bytes(SecretBox.KEY_SIZE)).decode("ascii")


@functools.lru_cache(maxsize=1)
def get_cipher() -> SecretCipher:
    """Шифр на ключе из конфигурации. Один на процесс."""
    from .config import get_settings

    return SecretCipher(get_settings().secret_key)


# --- Токены публичных ссылок -------------------------------------------------
# Токен непрозрачный и случайный; в БД лежит только его SHA-256. Отзыв ссылки
# мгновенный, списки отзыва не нужны — в отличие от JWT.

TOKEN_BYTES = 32


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
