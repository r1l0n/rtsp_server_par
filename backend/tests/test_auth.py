"""Пароли, TOTP, коды восстановления, ограничение частоты."""

from __future__ import annotations

import re
import time
import uuid

import pyotp
import pytest
from fastapi.responses import Response

from app.auth import ratelimit, sessions
from app.auth.passwords import (
    WeakPasswordError,
    hash_password,
    validate_password_policy,
    verify_password,
)
from app.auth.sessions import SessionData
from app.auth.totp import (
    generate_recovery_codes,
    hash_recovery_code,
    looks_like_recovery_code,
    new_secret,
    verify_code,
    verify_recovery_code,
)
from app.config import get_settings
from app.web.auth_views import safe_next
from app.web.templating import set_session_cookie


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


def test_every_generated_code_passes_the_shape_check() -> None:
    """Иначе отсев по форме отрезал бы настоящие коды восстановления."""
    for code in generate_recovery_codes(30):
        assert looks_like_recovery_code(code)
        assert looks_like_recovery_code(f"  {code.upper()}  ")


def test_totp_digits_are_not_mistaken_for_a_recovery_code() -> None:
    """Ради этого отсев и заведён: неверный код TOTP не должен запускать
    до десяти проверок argon2 подряд."""
    for code in (
        "123456",       # обычный код TOTP
        "000000",
        "",
        "abcde",        # нет второй половины
        "a1b2c-3d4e",   # на символ короче
        "a1b2c-3d4e5f",  # на символ длиннее
        "a1b2c_3d4e5",  # не тот разделитель
        "albol-1o0oo",  # 'l' и 'o' из алфавита исключены как похожие
    ):
        assert not looks_like_recovery_code(code), code


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


async def test_rate_limit_key_always_gets_a_deadline(fake_redis) -> None:
    """Счётчик без TTL — это вечная блокировка.

    Redis настроен с maxmemory-policy noeviction, поэтому ключ, потерявший
    срок, не истечёт уже никогда: адрес или учётная запись оказались бы
    заблокированы до ручной уборки. Срок должен появляться тем же шагом,
    что и сам счётчик.
    """
    limit = ratelimit.Limit(limit=5, window=60)
    await ratelimit.hit("test", "deadline", limit)
    assert await fake_redis.ttl("rl:test:deadline") > 0


async def test_rate_limit_window_does_not_slide_forward(fake_redis) -> None:
    """Повторные попытки не должны продлевать уже идущее окно."""
    limit = ratelimit.Limit(limit=5, window=60)
    await ratelimit.hit("test", "window", limit)
    await fake_redis.expire("rl:test:window", 10)

    await ratelimit.hit("test", "window", limit)

    assert await fake_redis.ttl("rl:test:window") <= 10


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


# ─── «Запомнить меня» ────────────────────────────────────────────────────────
def _remember_seconds() -> int:
    return get_settings().remember_me_days * 86400


async def test_remember_me_extends_session_ttl(fake_redis) -> None:
    ordinary = await sessions.create(uuid.uuid4())
    long_lived = await sessions.create(uuid.uuid4(), remember=True)

    assert await fake_redis.ttl(f"sess:{ordinary.sid}") == pytest.approx(
        get_settings().session_ttl_minutes * 60, abs=5
    )
    assert await fake_redis.ttl(f"sess:{long_lived.sid}") == pytest.approx(
        _remember_seconds(), abs=5
    )

    loaded = await sessions.load(long_lived.sid)
    assert loaded is not None and loaded.remember and loaded.expires_at > time.time()


async def test_pending_2fa_session_stays_short_but_keeps_the_flag(fake_redis) -> None:
    """Окно ввода кода не должно жить месяц — дедлайн появляется после 2FA."""
    pending = await sessions.create(uuid.uuid4(), pending_2fa=True, remember=True)
    assert pending.expires_at == 0.0
    assert await fake_redis.ttl(f"sess:{pending.sid}") == pytest.approx(
        get_settings().session_ttl_minutes * 60, abs=5
    )

    fresh = await sessions.rotate(pending, pending_2fa=False)
    assert fresh.remember and fresh.expires_at > time.time()
    assert await fake_redis.ttl(f"sess:{fresh.sid}") == pytest.approx(_remember_seconds(), abs=5)


async def test_touch_does_not_shrink_a_remembered_session(fake_redis) -> None:
    session = await sessions.create(uuid.uuid4(), remember=True)
    await sessions.touch(session)
    assert await fake_redis.ttl(f"sess:{session.sid}") == pytest.approx(
        _remember_seconds(), abs=5
    )


async def test_touch_never_pushes_past_the_deadline(fake_redis) -> None:
    """Предел жёсткий: активность продлевает сессию только до даты входа + 30 дней."""
    session = await sessions.create(uuid.uuid4(), remember=True)
    session.expires_at = time.time() + 60  # как будто месяц почти истёк
    await sessions.touch(session)
    assert await fake_redis.ttl(f"sess:{session.sid}") == pytest.approx(60, abs=5)


async def test_touch_keeps_the_user_index_alive(fake_redis) -> None:
    """Индекс живёт не меньше самой долгой сессии, иначе профиль её теряет."""
    user_id = uuid.uuid4()
    long_lived = await sessions.create(user_id, remember=True)
    ordinary = await sessions.create(user_id)

    await sessions.touch(ordinary)  # короткая сессия не смеет обрезать индекс
    assert await fake_redis.ttl(f"user_sess:{user_id}") == pytest.approx(
        _remember_seconds(), abs=5
    )
    assert {s.sid for s in await sessions.list_for_user(user_id)} == {
        long_lived.sid,
        ordinary.sid,
    }


def test_session_cookie_lives_as_long_as_the_session() -> None:
    ordinary = SessionData(
        sid="a", user_id="u", csrf="c", created_at=0, last_seen=0, ip="", user_agent="",
        pending_2fa=False,
    )
    remembered = SessionData(
        sid="b", user_id="u", csrf="c", created_at=0, last_seen=0, ip="", user_agent="",
        pending_2fa=False, remember=True, expires_at=time.time() + _remember_seconds(),
    )

    assert sessions.ttl_for(ordinary) == get_settings().session_ttl_minutes * 60
    assert sessions.ttl_for(remembered) == pytest.approx(_remember_seconds(), abs=5)

    response = Response()
    set_session_cookie(response, remembered)
    cookie = response.headers["set-cookie"]
    max_age = int(re.search(r"Max-Age=(\d+)", cookie).group(1))  # type: ignore[union-attr]
    assert max_age == pytest.approx(_remember_seconds(), abs=5)
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie


# ─── Открытые редиректы ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value",
    ["https://evil.example", "//evil.example", "http://evil", None, "", "evil.example/path"],
)
def test_external_redirect_targets_rejected(value: str | None) -> None:
    assert safe_next(value) == "/"


def test_local_redirect_target_preserved() -> None:
    assert safe_next("/cameras/123") == "/cameras/123"
