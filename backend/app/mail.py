"""Отправка почты через SMTP.

Писем здесь мало — приглашения сотрудников и проверочное письмо, — поэтому
взят stdlib-овый smtplib, вынесенный в поток. Асинхронный SMTP-клиент был бы
ещё одной зависимостью ради нескольких писем в месяц.

Откуда берутся параметры: сначала строка `mail_settings` (её заполняет
администратор в панели), и только если её нет — переменные окружения. Так
почту меняют мышкой, а `.env` остаётся способом задать её при развёртывании.

Отправка синхронна по отношению к запросу: администратор должен увидеть
«письмо ушло» или «SMTP отказал» сразу, а не гадать. Поэтому таймаут жёсткий,
а при отказе панель показывает ссылку приглашения прямо на странице.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .crypto import DecryptionError, get_cipher
from .logging_setup import get_logger
from .models import MailSettings

log = get_logger("mail")


class MailError(RuntimeError):
    """Письмо не отправлено. Текст пригоден для показа администратору."""


class MailNotConfigured(MailError):
    """SMTP не настроен — отправлять некуда."""


@dataclass(frozen=True, slots=True)
class MailConfig:
    """Готовые параметры отправки — уже неважно, из панели они или из `.env`."""

    enabled: bool
    host: str
    port: int
    security: str
    username: str
    password: str
    sender: str
    from_name: str
    timeout: int
    #: "panel" или "env" — панель показывает это администратору, чтобы он
    #: понимал, почему видит не те значения, что правил в файле.
    source: str

    @property
    def summary(self) -> str:
        return f"{self.host}:{self.port} ({self.security})" if self.host else "не настроена"


def config_from_env(settings: Settings | None = None) -> MailConfig:
    settings = settings or get_settings()
    return MailConfig(
        enabled=settings.mail_enabled,
        host=settings.smtp_host.strip(),
        port=settings.smtp_port,
        security=settings.smtp_security,
        username=settings.smtp_username,
        password=settings.smtp_secret,
        sender=settings.mail_sender,
        from_name=settings.mail_from_name,
        timeout=settings.smtp_timeout_seconds,
        source="env",
    )


def config_from_row(row: MailSettings) -> MailConfig:
    """Строка настроек → параметры отправки.

    Нерасшифровываемый пароль (сменили ключ шифрования, не перезадав почту)
    не должен ронять отправку с 500: считаем, что пароля нет, а SMTP сам
    скажет «не авторизован» — и это попадёт администратору на экран.
    """
    password = ""
    if row.password_enc:
        try:
            password = get_cipher().decrypt(row.password_enc)
        except DecryptionError:
            log.error("smtp_password_undecryptable")

    settings = get_settings()
    return MailConfig(
        enabled=row.enabled and bool(row.host.strip()),
        host=row.host.strip(),
        port=row.port,
        security=str(row.security),
        username=row.username,
        password=password,
        sender=row.mail_from.strip() or f"noreply@{settings.domain}",
        from_name=row.from_name,
        timeout=row.timeout_seconds,
        source="panel",
    )


async def get_row(db: AsyncSession) -> MailSettings | None:
    """Единственная строка настроек, если её уже завели."""
    row: MailSettings | None = await db.scalar(
        select(MailSettings).where(MailSettings.id == 1)
    )
    return row


async def load_config(db: AsyncSession) -> MailConfig:
    row = await get_row(db)
    return config_from_row(row) if row is not None else config_from_env()


async def record_result(db: AsyncSession, *, error: str = "") -> None:
    """Запоминает исход последней отправки — чтобы «работает ли почта»
    было видно на странице настроек, а не только в логах.

    Коммит — на стороне вызывающего кода. Если настройки заданы окружением,
    строки нет и записывать некуда: это не ошибка.
    """
    row = await get_row(db)
    if row is None:
        return
    if error:
        row.last_error = error[:500]
    else:
        row.last_success_at = dt.datetime.now(dt.UTC)
        row.last_error = ""


# ─── Сборка письма ───────────────────────────────────────────────────────────
def _check_header(value: str, field: str) -> str:
    """Заголовок не должен содержать переводов строк.

    Иначе адрес вида "a@b\\nBcc: victim@c" превращает одно письмо в рассылку.
    Значения приходят из формы администратора, но проверка тут дешевле веры.
    """
    if any(char in value for char in "\r\n"):
        raise MailError(f"недопустимый символ в поле {field}")
    return value


def build_message(
    config: MailConfig,
    *,
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr(
        (
            _check_header(config.from_name, "From"),
            _check_header(config.sender, "From"),
        )
    )
    message["To"] = _check_header(to, "To")
    message["Subject"] = _check_header(subject, "Subject")
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=get_settings().domain)
    # Приглашение — не рассылка: автоответчики и «вне офиса» тут не нужны.
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(text_body)
    if html_body is not None:
        message.add_alternative(html_body, subtype="html")
    return message


# ─── Соединение ──────────────────────────────────────────────────────────────
# socket.create_connection перебирает все адреса имени и, не подключившись ни
# к одному, поднимает ошибку ПОСЛЕДНЕГО. У почтовых серверов последним обычно
# оказывается IPv6, а на машине без IPv6 ядро отвергает его мгновенно —
# «Network is unreachable». Настоящая причина (таймаут по IPv4, закрытый порт)
# при этом теряется, и администратор чинит не то: сеть выглядит сломанной
# целиком, хотя не работает ровно один маршрут.
#
# Поэтому адреса перебираем сами и в ошибке показываем итог по каждому.


class SMTPUnreachable(OSError):
    """Ни один адрес имени не отозвался. В тексте — что ответил каждый."""


#: Адрес, по которому в прошлый раз удалось подключиться, — на хост и порт.
#:
#: Нужен, когда один из адресов имени «чёрная дыра»: хостинг режет исходящий
#: SMTP по IPv4, молча роняя пакеты, а по IPv6 пускает. Порядок адресов при
#: этом выбираем не мы — его задаёт RFC 6724, и с адресом ULA (а именно такой
#: выдаёт docker) IPv4 оказывается первым. Без этой памяти каждое письмо
#: сначала выжидало бы полный таймаут на заблокированном адресе.
_LAST_GOOD: dict[tuple[str, int], tuple[object, ...]] = {}


def _connect(host: str, port: int, timeout: float) -> socket.socket:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SMTPUnreachable(f"имя {host} не разрешается: {exc}") from exc

    # Удачный в прошлый раз адрес пробуем первым, порядок остальных не трогаем.
    remembered = _LAST_GOOD.get((host, port))
    if remembered is not None:
        addresses.sort(key=lambda item: item[4] != remembered)

    failures: list[str] = []
    for family, socktype, proto, _canonname, address in addresses:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(address)
        except OSError as exc:
            sock.close()
            kind = "IPv6" if family == socket.AF_INET6 else "IPv4"
            failures.append(f"{address[0]} [{kind}] — {exc.strerror or exc}")
            if address == remembered:
                # Больше не работает — пусть в следующий раз решает система.
                _LAST_GOOD.pop((host, port), None)
        else:
            _LAST_GOOD[(host, port)] = address
            return sock

    raise SMTPUnreachable("; ".join(failures) or f"у имени {host} нет адресов")


class _SMTP(smtplib.SMTP):
    """smtplib с нашим перебором адресов вместо create_connection."""

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return _connect(host, port, timeout)


class _SMTP_SSL(smtplib.SMTP_SSL, _SMTP):
    """То же для порта 465: TLS поднимается поверх нашего сокета.

    Порядок баз важен — SMTP_SSL берёт сокет через super()._get_socket,
    и по MRO им оказывается _SMTP.
    """


# ─── Транспорт ───────────────────────────────────────────────────────────────
def _send_sync(config: MailConfig, message: EmailMessage) -> None:
    """Блокирующая отправка. Вызывается только из потока (см. send)."""
    context = ssl.create_default_context()

    client: smtplib.SMTP
    if config.security == "ssl":
        client = _SMTP_SSL(config.host, config.port, timeout=config.timeout, context=context)
    else:
        client = _SMTP(config.host, config.port, timeout=config.timeout)

    with client:
        client.ehlo()
        if config.security == "starttls":
            client.starttls(context=context)
            # После STARTTLS соединение другое — представляемся заново.
            client.ehlo()
        if config.username:
            client.login(config.username, config.password)
        client.send_message(message)


async def send(
    config: MailConfig,
    *,
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> None:
    """Отправляет письмо. Бросает MailError, если не получилось."""
    if not config.enabled:
        raise MailNotConfigured(
            "почта не настроена: откройте «Почта» в панели и укажите SMTP-сервер"
        )

    message = build_message(
        config, to=to, subject=subject, text_body=text_body, html_body=html_body
    )

    try:
        await asyncio.to_thread(_send_sync, config, message)
    except smtplib.SMTPAuthenticationError as exc:
        log.error("smtp_auth_failed", host=config.host, code=exc.smtp_code)
        raise MailError("SMTP отклонил логин или пароль.") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        log.warning("smtp_recipient_refused", to=to)
        raise MailError("SMTP не принял адрес получателя.") from exc
    except smtplib.SMTPException as exc:
        log.error("smtp_failed", host=config.host, error=str(exc))
        raise MailError(f"SMTP-сервер ответил ошибкой: {exc}") from exc
    except (OSError, ssl.SSLError) as exc:
        # Недоступный хост, таймаут, битый TLS — самая частая причина отказа.
        log.error("smtp_unreachable", host=config.host, port=config.port, error=str(exc))
        raise MailError(
            f"Не удалось связаться с SMTP-сервером {config.host}:{config.port} — {exc}"
        ) from exc

    log.info("mail_sent", to=to, subject=subject, source=config.source)
