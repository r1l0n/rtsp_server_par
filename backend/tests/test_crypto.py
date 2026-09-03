from __future__ import annotations

import base64

import pytest

from app.crypto import (
    DecryptionError,
    SecretCipher,
    generate_key,
    generate_token,
    hash_token,
    tokens_equal,
)


def _cipher(byte: int = 1) -> SecretCipher:
    return SecretCipher(bytes([byte]) * 32)


def test_roundtrip_preserves_value() -> None:
    cipher = _cipher()
    secret = "rtsp://user:p%40ss@203.0.113.10:554/stream1"
    assert cipher.decrypt(cipher.encrypt(secret)) == secret


def test_ciphertext_differs_between_calls() -> None:
    """SecretBox использует случайный nonce — одинаковый вход даёт разный blob."""
    cipher = _cipher()
    assert cipher.encrypt("одно и то же") != cipher.encrypt("одно и то же")


def test_wrong_key_cannot_decrypt() -> None:
    blob = _cipher(1).encrypt("секрет")
    with pytest.raises(DecryptionError):
        _cipher(2).decrypt(blob)


def test_tampered_blob_rejected() -> None:
    cipher = _cipher()
    blob = bytearray(cipher.encrypt("секрет"))
    blob[-1] ^= 0xFF
    with pytest.raises(DecryptionError):
        cipher.decrypt(bytes(blob))


def test_unknown_version_rejected() -> None:
    with pytest.raises(DecryptionError):
        _cipher().decrypt(b"\x99abc")


def test_empty_blob_rejected() -> None:
    with pytest.raises(DecryptionError):
        _cipher().decrypt(b"")


def test_generate_key_is_32_bytes() -> None:
    assert len(base64.b64decode(generate_key())) == 32


def test_token_hash_is_stable_and_hex() -> None:
    token = generate_token()
    assert hash_token(token) == hash_token(token)
    assert len(hash_token(token)) == 64
    assert tokens_equal(hash_token(token), hash_token(token))
    assert not tokens_equal(hash_token(token), hash_token(generate_token()))


def test_tokens_are_unique() -> None:
    assert len({generate_token() for _ in range(200)}) == 200
