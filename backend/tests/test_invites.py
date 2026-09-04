"""Приглашения сотрудников: адреса, письмо, SMTP-транспорт.

Работа с базой здесь не проверяется — в тестовом окружении её нет. Проверено
всё, что можно проверить без неё: разбор адреса, содержимое письма, порядок
команд SMTP и защита заголовков от подстановки переводов строк.
"""

from __future__ import annotations

import datetime as dt
import smtplib
import uuid
from typing import ClassVar

import pytest

from app import invites, mail
from app.config import Settings
from app.models import Invitation, Role, User


# ─── Адрес ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Ivan@Example.RU ", "ivan@example.ru"),
        ("a.b+tag@sub.example.com", "a.b+tag@sub.example.com"),
    ],
)
def test_email_is_normalized(raw: str, expected: str) -> None:
    assert invites.normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "нет-собаки",
        "два@@example.com",
        "no@tld",
        "a b@example.com",
        "x@example.com" + "y" * 320,
    ],
)
def test_bad_email_is_rejected(raw: str) -> None:
    with pytest.raises(invites.InviteError):
        invites.normalize_email(raw)


def test_email_with_newline_is_rejected() -> None:
    """Перевод строки в адресе — это попытка дописать заголовки письма."""
    with pytest.raises(invites.InviteError):
        invites.normalize_email("victim@example.com\nBcc: everyone@example.com")


def test_invite_url_points_at_own_domain() -> None:
    url = invites.invite_url("токен-без-кириллицы-в-жизни")
    assert url.startswith("https://cam.test/invite/")


# ─── Письмо ──────────────────────────────────────────────────────────────────
def _invitation(**kwargs: object) -> Invitation:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "email": "new@example.com",
        "full_name": "",
        "role": Role.operator,
        "token_hash": "0" * 64,
        "expires_at": dt.datetime.now(dt.UTC) + dt.timedelta(hours=72),
    }
    base.update(kwargs)
    return Invitation(**base)


def _inviter() -> User:
    return User(id=uuid.uuid4(), email="admin@example.com", full_name="Пётр Смирнов")


def test_email_carries_the_link_in_both_parts() -> None:
    text, html_body = invites._render_email(_invitation(), "TOKEN123", _inviter())
    assert "https://cam.test/invite/TOKEN123" in text
    assert "https://cam.test/invite/TOKEN123" in html_body


def test_email_greets_by_name_when_it_is_known() -> None:
    text, _ = invites._render_email(_invitation(full_name="Анна"), "T", _inviter())
    assert text.startswith("Здравствуйте, Анна!")


def test_email_names_the_inviter() -> None:
    text, _ = invites._render_email(_invitation(), "T", _inviter())
    assert "Пётр Смирнов" in text


def test_email_escapes_html_in_names() -> None:
    inviter = User(id=uuid.uuid4(), email="a@b.ru", full_name="<script>alert(1)</script>")
    _, html_body = invites._render_email(_invitation(), "T", inviter)
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body


# ─── Состояние приглашения ───────────────────────────────────────────────────
def test_pending_invitation_is_pending() -> None:
    assert _invitation().is_pending


@pytest.mark.parametrize("field", ["accepted_at", "revoked_at"])
def test_used_invitation_is_not_pending(field: str) -> None:
    invitation = _invitation(**{field: dt.datetime.now(dt.UTC)})
    assert not invitation.is_pending


def test_expiry_is_compared_against_now() -> None:
    past = _invitation(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
    assert past.is_expired()
    assert not _invitation().is_expired()


# ─── Сборка сообщения ────────────────────────────────────────────────────────
def test_message_has_both_plain_text_and_html() -> None:
    message = mail.build_message(
        to="new@example.com", subject="Тема", text_body="текст", html_body="<b>текст</b>"
    )
    types = {part.get_content_type() for part in message.walk()}
    assert "text/plain" in types
    assert "text/html" in types


def test_message_from_defaults_to_own_domain() -> None:
    message = mail.build_message(to="new@example.com", subject="Тема", text_body="текст")
    assert "noreply@cam.test" in message["From"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("to", "victim@example.com\nBcc: everyone@example.com"),
        ("subject", "Тема\r\nBcc: everyone@example.com"),
    ],
)
def test_header_injection_is_refused(field: str, value: str) -> None:
    kwargs = {"to": "ok@example.com", "subject": "Тема", "text_body": "текст"}
    kwargs[field] = value
    with pytest.raises(mail.MailError):
        mail.build_message(**kwargs)  # type: ignore[arg-type]


# ─── Транспорт ───────────────────────────────────────────────────────────────
async def test_send_without_smtp_host_says_so() -> None:
    """Без SMTP_HOST отправка обязана падать понятной ошибкой, а не молчать."""
    with pytest.raises(mail.MailNotConfigured):
        await mail.send(to="new@example.com", subject="Тема", text_body="текст")


class _FakeSMTP:
    """Записывает вызовы вместо настоящего соединения."""

    instances: ClassVar[list[_FakeSMTP]] = []

    def __init__(self, host: str, port: int, timeout: float = 0, context: object = None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent: list[object] = []
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, context: object = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, message: object) -> None:
        self.calls.append("send")
        self.sent.append(message)


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSMTP]:
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    return _FakeSMTP


def _message() -> object:
    return mail.build_message(to="new@example.com", subject="Тема", text_body="текст")


def test_starttls_happens_before_login(fake_smtp: type[_FakeSMTP]) -> None:
    """Логин до STARTTLS отдал бы пароль открытым текстом."""
    settings = Settings(
        smtp_host="smtp.example.com", smtp_port=587, smtp_security="starttls",
        smtp_username="bot", smtp_password="s3cret",
    )
    mail._send_sync(settings, _message())  # type: ignore[arg-type]

    calls = fake_smtp.instances[0].calls
    assert calls.index("starttls") < calls.index("login:bot:s3cret")
    # После STARTTLS соединение другое — EHLO обязан повториться.
    assert calls.count("ehlo") == 2
    assert "send" in calls


def test_implicit_tls_does_not_call_starttls(fake_smtp: type[_FakeSMTP]) -> None:
    settings = Settings(smtp_host="smtp.example.com", smtp_port=465, smtp_security="ssl")
    mail._send_sync(settings, _message())  # type: ignore[arg-type]
    assert "starttls" not in fake_smtp.instances[0].calls


def test_login_is_skipped_without_username(fake_smtp: type[_FakeSMTP]) -> None:
    """Локальный релей часто пускает без авторизации."""
    settings = Settings(smtp_host="mailhog", smtp_port=1025, smtp_security="none")
    mail._send_sync(settings, _message())  # type: ignore[arg-type]
    assert not any(call.startswith("login") for call in fake_smtp.instances[0].calls)


def test_password_file_wins_over_env_variable(tmp_path) -> None:
    secret_file = tmp_path / "smtp_password"
    secret_file.write_text("из-файла\n", encoding="utf-8")
    settings = Settings(smtp_password="из-переменной", smtp_password_file=secret_file)
    assert settings.smtp_secret == "из-файла"


def test_mail_is_disabled_until_host_is_set() -> None:
    assert not Settings().mail_enabled
    assert Settings(smtp_host="smtp.example.com").mail_enabled
