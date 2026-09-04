"""Восстановление пароля по ссылке из письма.

Устройство то же, что у приглашений: одноразовый токен, в базе только его
SHA-256, срок жизни короткий. Отличий два, и оба продиктованы тем, что
запросить восстановление может кто угодно, не входя в систему.

Первое: наружу не должно утекать, есть ли такая учётная запись. Форма отвечает
одинаково и на известный, и на незнакомый адрес.

Второе: ссылку нельзя показать в интерфейсе, даже когда почта не настроена.
Для приглашений это удобный запасной путь — их выписывает администратор.
Здесь тот же приём отдал бы ключ от чужой учётной записи любому, кто ввёл
чужой адрес.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import mail
from .config import get_settings
from .crypto import generate_token, hash_token
from .models import PasswordReset, User

#: Час. Письмо в чужом почтовом ящике не должно оставаться ключом от учётной
#: записи неделями — а на «открыть письмо и придумать пароль» хватает.
TTL_MINUTES = 60

SUBJECT = "Восстановление пароля RTSP"


def reset_url(token: str) -> str:
    return f"{get_settings().base_url}/reset/{token}"


async def create(db: AsyncSession, user: User, *, ip: str = "") -> str:
    """Выписывает ссылку восстановления и гасит прежние.

    Прежние гасятся намеренно: если человек нажал «забыл пароль» дважды,
    рабочей должна остаться ровно одна ссылка — та, что в последнем письме.
    """
    for previous in await db.scalars(
        select(PasswordReset).where(
            PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None)
        )
    ):
        previous.used_at = dt.datetime.now(dt.UTC)

    token = generate_token()
    db.add(
        PasswordReset(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=TTL_MINUTES),
            requested_ip=ip[:45],
        )
    )
    await db.flush()
    return token


async def resolve(db: AsyncSession, token: str) -> tuple[PasswordReset, User] | None:
    """Действующая ссылка вместе с её владельцем, иначе None."""
    if not token:
        return None
    row = await db.scalar(
        select(PasswordReset).where(PasswordReset.token_hash == hash_token(token))
    )
    if row is None or not row.is_pending or row.is_expired():
        return None

    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    return row, user


# ─── Письмо ──────────────────────────────────────────────────────────────────
def render_email(url: str) -> tuple[str, str]:
    text = (
        "Здравствуйте,\n\n"
        "Кто-то запросил восстановление пароля для вашей учётной записи "
        "в сервисе просмотра камер.\n\n"
        f"Чтобы задать новый пароль, откройте ссылку:\n{url}\n\n"
        f"Ссылка одноразовая и действует {TTL_MINUTES} минут.\n\n"
        "Если вы этого не делали — просто удалите письмо. "
        "Пароль останется прежним, никаких действий не требуется.\n"
    )

    body = mail.wrap_html(
        greeting="Здравствуйте,",
        lead=(
            "Кто-то запросил восстановление пароля для вашей учётной записи. "
            "Нажмите кнопку ниже, чтобы задать новый."
        ),
        button_label="Задать новый пароль",
        fine_print=(
            f"Ссылка одноразовая и действует {TTL_MINUTES} минут. "
            "Если кнопка не работает, откройте адрес вручную:"
        ),
        url=url,
        footer=(
            "Если вы этого не делали — удалите письмо. Пароль останется прежним, "
            "делать ничего не нужно."
        ),
    )
    return text, body


async def send_email(user: User, token: str, config: mail.MailConfig) -> None:
    """Отправляет письмо. Бросает mail.MailError, если не получилось."""
    text, body = render_email(reset_url(token))
    await mail.send(config, to=user.email, subject=SUBJECT, text_body=text, html_body=body)


async def user_by_email(db: AsyncSession, email: str) -> User | None:
    user = await db.scalar(select(User).where(User.email == email))
    return user if user is not None and user.is_active else None


def as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
