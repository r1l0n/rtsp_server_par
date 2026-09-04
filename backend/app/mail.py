"""Отправка почты через SMTP.

Писем здесь мало — приглашения сотрудников и ничего больше, — поэтому взят
stdlib-овый smtplib, вынесенный в поток. Асинхронный SMTP-клиент был бы ещё
одной зависимостью ради нескольких писем в месяц.

Отправка синхронна по отношению к запросу: администратор должен увидеть
«письмо ушло» или «SMTP отказал» сразу, а не гадать. Поэтому таймаут жёсткий
(SMTP_TIMEOUT_SECONDS), а при отказе панель показывает ссылку прямо на
странице — приглашение остаётся рабочим.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from .config import Settings, get_settings
from .logging_setup import get_logger

log = get_logger("mail")


class MailError(RuntimeError):
    """Письмо не отправлено. Текст пригоден для показа администратору."""


class MailNotConfigured(MailError):
    """SMTP не настроен — отправлять некуда."""


def _check_header(value: str, field: str) -> str:
    """Заголовок не должен содержать переводов строк.

    Иначе адрес вида "a@b\\nBcc: victim@c" превращает одно письмо в рассылку.
    Адрес приходит из формы администратора, но проверка тут дешевле веры.
    """
    if any(char in value for char in "\r\n"):
        raise MailError(f"недопустимый символ в поле {field}")
    return value


def build_message(
    *, to: str, subject: str, text_body: str, html_body: str | None = None
) -> EmailMessage:
    settings = get_settings()
    message = EmailMessage()
    message["From"] = formataddr(
        (
            _check_header(settings.mail_from_name, "From"),
            _check_header(settings.mail_sender, "From"),
        )
    )
    message["To"] = _check_header(to, "To")
    message["Subject"] = _check_header(subject, "Subject")
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=settings.domain)
    # Приглашение — не рассылка: автоответчики и «вне офиса» тут не нужны.
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(text_body)
    if html_body is not None:
        message.add_alternative(html_body, subtype="html")
    return message


def _send_sync(settings: Settings, message: EmailMessage) -> None:
    """Блокирующая отправка. Вызывается только из потока (см. send)."""
    context = ssl.create_default_context()
    timeout = settings.smtp_timeout_seconds
    host, port = settings.smtp_host.strip(), settings.smtp_port

    client: smtplib.SMTP
    if settings.smtp_security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=timeout)

    with client:
        client.ehlo()
        if settings.smtp_security == "starttls":
            client.starttls(context=context)
            # После STARTTLS соединение другое — представляемся заново.
            client.ehlo()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_secret)
        client.send_message(message)


async def send(*, to: str, subject: str, text_body: str, html_body: str | None = None) -> None:
    """Отправляет письмо. Бросает MailError, если не получилось."""
    settings = get_settings()
    if not settings.mail_enabled:
        raise MailNotConfigured(
            "почта не настроена: задайте SMTP_HOST в .env, чтобы приглашения уходили письмом"
        )

    message = build_message(to=to, subject=subject, text_body=text_body, html_body=html_body)

    try:
        await asyncio.to_thread(_send_sync, settings, message)
    except smtplib.SMTPAuthenticationError as exc:
        log.error("smtp_auth_failed", host=settings.smtp_host, code=exc.smtp_code)
        raise MailError("SMTP отклонил логин или пароль.") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        log.warning("smtp_recipient_refused", to=to)
        raise MailError("SMTP не принял адрес получателя.") from exc
    except smtplib.SMTPException as exc:
        log.error("smtp_failed", host=settings.smtp_host, error=str(exc))
        raise MailError(f"SMTP-сервер ответил ошибкой: {exc}") from exc
    except (OSError, ssl.SSLError) as exc:
        # Недоступный хост, таймаут, битый TLS — самая частая причина отказа.
        log.error("smtp_unreachable", host=settings.smtp_host, error=str(exc))
        raise MailError(
            f"Не удалось связаться с SMTP-сервером {settings.smtp_host}: {exc}"
        ) from exc

    log.info("mail_sent", to=to, subject=subject)
