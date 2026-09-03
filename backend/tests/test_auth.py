"""Пароли, TOTP, коды восстановления, ограничение частоты."""

from __future__ import annotations

import time
import uuid

import pyotp
import pytest

from app.auth import ratelimit, sessions
from app.auth.passwords import (
    WeakPasswordError,
    hash_password,
    validate_password_policy,
    verify_password,
)
from app.auth.totp import (
    generate_recovery_codes,
    hash_recovery_code,
    new_secret,
    verify_code,
    verify_recovery_code,
)
from app.web.auth_views import safe_next


# ─── Пароли ──────────────────────────────────────────────────────────────────
def test_password_roundtrip() -> None:
    digest = hash_password("правильный-пароль-2026")
    assert verify_password(digest, "правильный-пароль-2026")
    assert not verify_password(digest, "другой-пароль-2026")


def test_hash_is_argon2id_and_salted() -> None:
    first = hash_password("одинаковый-пароль-123")
    second = hash_password("одинаковый-пароль-123")
    assert first.startswith("$argon2id$")
    assert first != second


@pytest.mark.parametrize("garbage", ["not-a-hash", "", "$argon2id$broken", "не-хеш-вовсе"])
def test_verify_tolerates_garbage_hash(garbage: str) -> None:
    """Битый хеш в БД не должен ронять форму входа."""
    assert not verify_password(garbage, "что угодно")


@pytest.mark.parametrize(
    "password",
    [
        "короткий",                # < 12 символов
        "aaaaaaaaaaaaaaaa",        # мало уникальных символов
        "123456789012345",         # только цифры
    ],
)
def test_weak_passwords_rejected(password: str) -> None:
    with pytest.raises(WeakPasswordError):
        validate_password_policy(password)


def test_password_must_not_contain_login() -> None:
    with pytest.raises(WeakPasswordError, match="имя учётной записи"):
        validate_password_policy("ivanov-Parol-2026", email="ivanov@company.ru")


def test_reasonable_password_passes() -> None:
    validate_password_policy("Kamera-Prohodnaya-2026", email="admin@company.ru")


# ─── TOTP ────────────────────────────────────────────────────────────────────
async def test_valid_code_accepted() -> None:
    secret = new_secret()
    code = pyotp.TOTP(secret).now()
    assert await verify_code(secret, code, uuid.uuid4())


async def test_code_cannot_be_reused() -> None:
    """Подсмотренный код не должен работать второй раз в том же окне."""
    secret = new_secret()
    user_id = uuid.uuid4()
    code = pyotp.TOTP(secret).now()

    assert await verify_code(secret, code, user_id)
    assert not await verify_code(secret, code, user_id)


async def test_same_code_is_independent_per_user() -> None:
    secret = new_secret()
    code = pyotp.TOTP(secret).now()
    assert await verify_code(secret, code, uuid.uuid4())
    assert await verify_code(secret, code, uuid.uuid4())


async def test_neighbouring_window_accepted() -> None:
    """Часы телефона могут отставать на один шаг."""
    secret = new_secret()
    code = pyotp.TOTP(secret).at(int(time.time()) - 30)
    assert await verify_code(secret, code, uuid.uuid4())


async def test_far_window_rejected() -> None:
    secret = new_secret()
    code = pyotp.TOTP(secret).at(int(time.time()) - 300)
    assert not await verify_code(secret, code, uuid.uuid4())


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "12 34 56"])
async def test_malformed_codes_rejected(code: str) -> None:
    assert not await verify_code(new_secret(), code, uuid.uuid4())


# ─── Коды восстановления ─────────────────────────────────────────────────────
def test_recovery_codes_are_unique_and_formatted() -> None:
    codes = generate_recovery_codes()
    assert len(codes) == 10
    assert len(set(codes)) == 10
    assert all(len(code) == 11 and code[5] == "-" for code in codes)


def test_recovery_code_verification_ignores_case_and_spaces() -> None:
    code = generate_recovery_codes(1)[0]
    digest = hash_recovery_code(code)
    assert verify_recovery_code(digest, f"  {code.upper()} ")
    assert not verify_recovery_code(digest, generate_recovery_codes(1)[0])


# ─── Ограничение частоты ─────────────────────────────────────────────────────
async def test_rate_limit_blocks_after_threshold() -> None:
    limit = ratelimit.Limit(limit=3, window=60)
    for _ in range(3):
        assert (await ratelimit.hit("test", "1.2.3.4", limit)).allowed
    blocked = await ratelimit.hit("test", "1.2.3.4", limit)
    assert not blocked.allowed
    assert blocked.retry_after > 0


async def test_rate_limit_is_per_key() -> None:
    limit = ratelimit.Limit(limit=1, window=60)
    assert (await ratelimit.hit("test", "a", limit)).allowed
    assert (await ratelimit.hit("test", "b", limit)).allowed


async def test_rate_limit_reset_clears_counter() -> None:
    limit = ratelimit.Limit(limit=1, window=60)
    await ratelimit.hit("test", "c", limit)
    assert not (await ratelimit.hit("test", "c", limit)).allowed
    await ratelimit.reset("test", "c")
    assert (await ratelimit.hit("test", "c", limit)).allowed


# ─── Сессии ──────────────────────────────────────────────────────────────────
async def test_session_lifecycle() -> None:
    user_id = uuid.uuid4()
    created = await sessions.create(user_id, ip="203.0.113.9", user_agent="Firefox")

    loaded = await sessions.load(created.sid)
    assert loaded is not None
    assert loaded.user_id == str(user_id)
    assert loaded.csrf == created.csrf
    assert loaded.authenticated

    await sessions.delete(created.sid)
    assert await sessions.load(created.sid) is None


async def test_rotate_issues_new_id_and_kills_old() -> None:
    """Смена уровня привилегий не должна оставлять старый идентификатор живым."""
    pending = await sessions.create(uuid.uuid4(), pending_2fa=True)
    fresh = await sessions.rotate(pending, pending_2fa=False)

    assert fresh.sid != pending.sid
    assert await sessions.load(pending.sid) is None
    loaded = await sessions.load(fresh.sid)
    assert loaded is not None and loaded.authenticated


async def test_delete_all_keeps_current_session() -> None:
    user_id = uuid.uuid4()
    keep = await sessions.create(user_id)
    await sessions.create(user_id)
    await sessions.create(user_id)

    removed = await sessions.delete_all_for_user(user_id, except_sid=keep.sid)
    assert removed == 2
    assert await sessions.load(keep.sid) is not None


async def test_unknown_session_id_returns_none() -> None:
    assert await sessions.load("не существует") is None
    assert await sessions.load(None) is None


# ─── Открытые редиректы ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value",
    ["https://evil.example", "//evil.example", "http://evil", None, "", "evil.example/path"],
)
def test_external_redirect_targets_rejected(value: str | None) -> None:
    assert safe_next(value) == "/"


def test_local_redirect_target_preserved() -> None:
    assert safe_next("/cameras/123") == "/cameras/123"
