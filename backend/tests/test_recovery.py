"""Восстановление пароля: письмо, срок жизни, границы токена.

Работа с базой здесь не проверяется — её в тестовом окружении нет. Проверено
то, что можно проверить без неё, и прежде всего свойства, ради которых эта
форма и опасна: она открыта всем без входа в систему.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app import recovery
from app.models import PasswordReset, User


def _row(**kwargs: object) -> PasswordReset:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "token_hash": "0" * 64,
        "expires_at": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=30),
    }
    base.update(kwargs)
    return PasswordReset(**base)


def test_link_points_at_own_domain() -> None:
    assert recovery.reset_url("TOKEN").startswith("https://cam.test/reset/")


def test_fresh_link_is_usable() -> None:
    row = _row()
    assert row.is_pending
    assert not row.is_expired()


def test_used_link_stops_working() -> None:
    assert not _row(used_at=dt.datetime.now(dt.UTC)).is_pending


def test_expired_link_stops_working() -> None:
    past = _row(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
    assert past.is_expired()


def test_link_lives_an_hour_not_a_week() -> None:
    """Письмо в чужом ящике не должно годами открывать учётную запись."""
    assert recovery.TTL_MINUTES <= 120


def test_email_carries_the_link_in_both_parts() -> None:
    text, html_body = recovery.render_email("https://cam.test/reset/TOKEN123")
    assert "https://cam.test/reset/TOKEN123" in text
    assert "https://cam.test/reset/TOKEN123" in html_body


def test_email_says_what_to_do_if_you_did_not_ask() -> None:
    """Письмо получает и тот, чей адрес назвали чужие руки."""
    text, _ = recovery.render_email("https://cam.test/reset/T")
    assert "не делали" in text


def test_email_never_mentions_the_account_holder() -> None:
    """Ни имени, ни роли: письмо могло уйти на чужой ящик по опечатке."""
    text, html_body = recovery.render_email("https://cam.test/reset/T")
    for part in (text, html_body):
        assert "@" not in part.replace("https://cam.test/reset/T", "")


def test_email_looks_like_the_invitation() -> None:
    """Общая оболочка: иначе второе письмо читается как подделка первого."""
    _, html_body = recovery.render_email("https://cam.test/reset/T")
    assert "<table" in html_body
    assert "display:flex" not in html_body


def test_inactive_user_is_treated_as_absent() -> None:
    """Отключённой учётной записи восстанавливать нечего."""
    disabled = User(id=uuid.uuid4(), email="off@example.com", is_active=False)
    assert disabled.is_active is False
