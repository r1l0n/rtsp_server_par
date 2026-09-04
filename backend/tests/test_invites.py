"""Приглашения сотрудников: адреса, письмо, настройки SMTP, транспорт.

Работа с базой здесь не проверяется — в тестовом окружении её нет. Проверено
всё, что можно проверить без неё: разбор адреса, содержимое письма, выбор
источника настроек (панель против `.env`), порядок команд SMTP и защита
заголовков от подстановки переводов строк.
"""

from __future__ import annotations

import datetime as dt
import smtplib
import socket
import uuid
from typing import ClassVar

import pytest

from app import invites, mail
from app.config import Settings
from app.crypto import get_cipher
from app.models import Invitation, MailSecurity, MailSettings, Role, User


def _config(**kwargs: object) -> mail.MailConfig:
    """Параметры отправки с разумными значениями по умолчанию."""
    base: dict[str, object] = {
        "enabled": True,
        "host": "smtp.example.com",
        "port": 587,
        "security": "starttls",
        "username": "",
        "password": "",
        "sender": "noreply@cam.test",
        "from_name": "RTSP",
        "timeout": 15,
        "source": "panel",
    }
    base.update(kwargs)
    return mail.MailConfig(**base)  # type: ignore[arg-type]


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


def test_email_with_newline_inside_is_rejected() -> None:
    """Перевод строки в адресе — это попытка дописать заголовки письма."""
    with pytest.raises(invites.InviteError):
        invites.normalize_email("victim@example.com\nBcc: everyone@example.com")


def test_trailing_newline_is_stripped_not_smuggled() -> None:
    """Хвостовой перевод строки срезается — в заголовок он попасть не может.

    Проверка именно на это: в Python «$» совпадает и перед завершающим \\n,
    поэтому регулярка стоит на \\A…\\Z, а сам символ убирает strip().
    """
    assert invites.normalize_email("victim@example.com\n") == "victim@example.com"


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
    text, html_body = invites._render_email(_invitation(full_name="Анна"), "T", _inviter())
    assert text.startswith("Здравствуйте, Анна,")
    assert "Здравствуйте, Анна," in html_body


def test_email_is_laid_out_with_tables() -> None:
    """Outlook рисует письма движком Word: ни flex, ни grid там нет.

    Проверка дешёвая, а ломается это молча и только у получателя.
    """
    _, html_body = invites._render_email(_invitation(), "T", _inviter())
    assert "<table" in html_body
    assert "display:flex" not in html_body
    assert "display:grid" not in html_body
    # Стили только инлайновые: <style> в <head> Gmail вырезает.
    assert "<style" not in html_body


def test_email_button_and_fallback_point_at_the_same_link() -> None:
    """Кнопка не работает у части почтовиков — адрес обязан быть и текстом."""
    _, html_body = invites._render_email(_invitation(), "TOKEN123", _inviter())
    assert html_body.count("https://cam.test/invite/TOKEN123") >= 3


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
        _config(),
        to="new@example.com",
        subject="Тема",
        text_body="текст",
        html_body="<b>текст</b>",
    )
    types = {part.get_content_type() for part in message.walk()}
    assert "text/plain" in types
    assert "text/html" in types


def test_message_from_uses_the_configured_sender() -> None:
    message = mail.build_message(
        _config(sender="noreply@panel.example", from_name="Панель"),
        to="new@example.com",
        subject="Тема",
        text_body="текст",
    )
    assert "noreply@panel.example" in message["From"]
    assert "Панель" in message["From"]


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
        mail.build_message(_config(), **kwargs)  # type: ignore[arg-type]


def test_header_injection_through_sender_name_is_refused() -> None:
    """Имя отправителя тоже приходит из формы администратора."""
    with pytest.raises(mail.MailError):
        mail.build_message(
            _config(from_name="Панель\r\nBcc: everyone@example.com"),
            to="ok@example.com",
            subject="Тема",
            text_body="текст",
        )


# ─── Откуда берутся настройки ────────────────────────────────────────────────
def _row(**kwargs: object) -> MailSettings:
    base: dict[str, object] = {
        "id": 1,
        "enabled": True,
        "host": "smtp.panel.example",
        "port": 465,
        "security": MailSecurity.ssl,
        "username": "bot",
        "password_enc": None,
        "mail_from": "noreply@panel.example",
        "from_name": "Панель",
        "timeout_seconds": 20,
        "last_error": "",
    }
    base.update(kwargs)
    return MailSettings(**base)


def test_panel_settings_replace_environment() -> None:
    config = mail.config_from_row(_row())
    assert config.source == "panel"
    assert config.host == "smtp.panel.example"
    assert config.port == 465
    assert config.security == "ssl"
    assert config.timeout == 20


def test_disabled_row_disables_sending() -> None:
    assert not mail.config_from_row(_row(enabled=False)).enabled


def test_row_without_host_is_not_enabled() -> None:
    """Галочка «отправлять» при пустом сервере не должна включать отправку."""
    assert not mail.config_from_row(_row(host="  ")).enabled


def test_stored_password_is_decrypted_for_sending() -> None:
    row = _row(password_enc=get_cipher().encrypt("секрет-smtp"))
    assert mail.config_from_row(row).password == "секрет-smtp"


def test_undecryptable_password_does_not_break_sending() -> None:
    """Сменили ключ шифрования — почта должна ругаться на авторизацию,
    а не ронять страницу пятисоткой."""
    config = mail.config_from_row(_row(password_enc=b"\x01not-a-secretbox-blob"))
    assert config.password == ""
    assert config.enabled


def test_row_without_sender_falls_back_to_own_domain() -> None:
    assert mail.config_from_row(_row(mail_from="")).sender == "noreply@cam.test"


def test_environment_is_used_until_panel_settings_appear() -> None:
    config = mail.config_from_env(Settings(smtp_host="smtp.env.example", smtp_port=2525))
    assert config.source == "env"
    assert config.host == "smtp.env.example"
    assert config.enabled


def test_mail_is_disabled_until_something_is_configured() -> None:
    assert not mail.config_from_env(Settings()).enabled


# ─── Транспорт ───────────────────────────────────────────────────────────────
async def test_send_while_disabled_says_so() -> None:
    """Выключенная почта обязана падать понятной ошибкой, а не молчать."""
    with pytest.raises(mail.MailNotConfigured):
        await mail.send(
            _config(enabled=False), to="new@example.com", subject="Тема", text_body="текст"
        )


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
    # Подменяем именно наши подклассы: они наследуют smtplib на момент
    # импорта, и подмена самого smtplib до них уже не дошла бы — тест ушёл бы
    # соединяться с настоящим сервером.
    _FakeSMTP.instances = []
    monkeypatch.setattr(mail, "_SMTP", _FakeSMTP)
    monkeypatch.setattr(mail, "_SMTP_SSL", _FakeSMTP)
    return _FakeSMTP


# ─── Перебор адресов ─────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _forget_addresses() -> object:
    """Память об удачном адресе — глобальная, между тестами её не тащим."""
    mail._LAST_GOOD.clear()
    yield
    mail._LAST_GOOD.clear()


def _addrinfo(*addresses: tuple[int, str]) -> list[tuple]:
    return [
        (family, socket.SOCK_STREAM, 6, "", (ip, 587, 0, 0) if family == socket.AF_INET6
         else (ip, 587))
        for family, ip in addresses
    ]


def test_connect_error_reports_every_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """Провал по IPv4 не должен прятаться за мгновенным отказом IPv6.

    Настоящий случай: у имени есть A и AAAA, IPv6 на машине нет. Ядро
    отвергает IPv6 мгновенно, и стандартный create_connection показывает
    именно эту ошибку — «Network is unreachable». Администратор чинит
    маршрутизацию, хотя на деле по IPv4 просто закрыт порт.
    """
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: _addrinfo(
            (socket.AF_INET, "77.88.21.158"), (socket.AF_INET6, "2a02:6b8::19d")
        ),
    )

    errors = {"77.88.21.158": TimeoutError("Connection timed out"),
              "2a02:6b8::19d": OSError(101, "Network is unreachable")}

    class _Socket:
        def __init__(self, family: int, *_: object) -> None:
            self.family = family

        def settimeout(self, _: float) -> None: ...
        def close(self) -> None: ...

        def connect(self, address: tuple) -> None:
            raise errors[address[0]]

    monkeypatch.setattr(socket, "socket", _Socket)

    with pytest.raises(mail.SMTPUnreachable) as caught:
        mail._connect("smtp.yandex.ru", 587, 5)

    text = str(caught.value)
    assert "77.88.21.158 [IPv4]" in text
    assert "2a02:6b8::19d [IPv6]" in text
    assert "Connection timed out" in text


def test_connect_returns_the_first_address_that_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: _addrinfo(
            (socket.AF_INET6, "2a02:6b8::19d"), (socket.AF_INET, "77.88.21.158")
        ),
    )

    class _Socket:
        def __init__(self, family: int, *_: object) -> None:
            self.family = family

        def settimeout(self, _: float) -> None: ...
        def close(self) -> None: ...

        def connect(self, address: tuple) -> None:
            if self.family == socket.AF_INET6:
                raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(socket, "socket", _Socket)

    assert mail._connect("smtp.yandex.ru", 587, 5).family == socket.AF_INET


def test_working_address_is_tried_first_next_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Заблокированный адрес не должен съедать таймаут на каждом письме.

    Провайдер, режущий исходящий SMTP по IPv4, роняет пакеты молча — попытка
    висит до таймаута. Порядок адресов задаёт система, и с ULA-адресом docker
    она ставит первым как раз IPv4. Один раз подождать придётся, дальше —
    сразу по рабочему адресу.
    """
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: _addrinfo(
            (socket.AF_INET, "77.88.21.158"), (socket.AF_INET6, "2a02:6b8::19d")
        ),
    )

    attempts: list[str] = []

    class _Socket:
        def __init__(self, family: int, *_: object) -> None:
            self.family = family

        def settimeout(self, _: float) -> None: ...
        def close(self) -> None: ...

        def connect(self, address: tuple) -> None:
            attempts.append(address[0])
            if self.family == socket.AF_INET:
                raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "socket", _Socket)

    mail._connect("smtp.yandex.ru", 587, 5)
    assert attempts == ["77.88.21.158", "2a02:6b8::19d"]

    attempts.clear()
    mail._connect("smtp.yandex.ru", 587, 5)
    assert attempts == ["2a02:6b8::19d"], "рабочий адрес обязан идти первым"


def test_remembered_address_is_forgotten_when_it_stops_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mail._LAST_GOOD[("smtp.yandex.ru", 587)] = ("2a02:6b8::19d", 587, 0, 0)
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **kw: _addrinfo((socket.AF_INET6, "2a02:6b8::19d")),
    )

    class _Socket:
        def __init__(self, *_: object) -> None: ...
        def settimeout(self, _: float) -> None: ...
        def close(self) -> None: ...

        def connect(self, address: tuple) -> None:
            raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(socket, "socket", _Socket)

    with pytest.raises(mail.SMTPUnreachable):
        mail._connect("smtp.yandex.ru", 587, 5)
    assert ("smtp.yandex.ru", 587) not in mail._LAST_GOOD


def test_unresolvable_name_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: object, **kw: object) -> None:
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(mail.SMTPUnreachable, match="не разрешается"):
        mail._connect("smtp.opechatka.ru", 587, 5)


def test_ssl_client_takes_its_socket_from_our_connect() -> None:
    """MRO обязан вести SMTP_SSL к нашему _get_socket, а не к stdlib-овому."""
    assert mail._SMTP_SSL.__mro__.index(mail._SMTP) < mail._SMTP_SSL.__mro__.index(smtplib.SMTP)
    assert mail._SMTP._get_socket is not smtplib.SMTP._get_socket


def _message() -> object:
    return mail.build_message(
        _config(), to="new@example.com", subject="Тема", text_body="текст"
    )


def test_starttls_happens_before_login(fake_smtp: type[_FakeSMTP]) -> None:
    """Логин до STARTTLS отдал бы пароль открытым текстом."""
    config = _config(username="bot", password="s3cret")
    mail._send_sync(config, _message())  # type: ignore[arg-type]

    calls = fake_smtp.instances[0].calls
    assert calls.index("starttls") < calls.index("login:bot:s3cret")
    # После STARTTLS соединение другое — EHLO обязан повториться.
    assert calls.count("ehlo") == 2
    assert "send" in calls


def test_implicit_tls_does_not_call_starttls(fake_smtp: type[_FakeSMTP]) -> None:
    config = _config(port=465, security="ssl")
    mail._send_sync(config, _message())  # type: ignore[arg-type]
    assert "starttls" not in fake_smtp.instances[0].calls


def test_login_is_skipped_without_username(fake_smtp: type[_FakeSMTP]) -> None:
    """Локальный релей часто пускает без авторизации."""
    config = _config(host="mailhog", port=1025, security="none")
    mail._send_sync(config, _message())  # type: ignore[arg-type]
    assert not any(call.startswith("login") for call in fake_smtp.instances[0].calls)


def test_configured_timeout_reaches_the_client(fake_smtp: type[_FakeSMTP]) -> None:
    mail._send_sync(_config(timeout=7), _message())  # type: ignore[arg-type]
    assert fake_smtp.instances[0].timeout == 7


def test_password_file_wins_over_env_variable(tmp_path) -> None:
    secret_file = tmp_path / "smtp_password"
    secret_file.write_text("из-файла\n", encoding="utf-8")
    settings = Settings(smtp_password="из-переменной", smtp_password_file=secret_file)
    assert settings.smtp_secret == "из-файла"
