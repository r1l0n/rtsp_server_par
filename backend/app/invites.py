"""Приглашения сотрудников.

Схема: администратор вводит адрес → в базе появляется приглашение с хешем
одноразового токена → сотруднику уходит письмо со ссылкой → по ссылке он
задаёт пароль, и только в этот момент создаётся учётная запись.

Учётной записи до принятия приглашения намеренно нет. Иначе в `users` жила бы
строка с паролем, которого никто не знает, — её пришлось бы считать активной
во всех проверках, а адрес уже отвечал бы «пароль неверный» вместо «нет такого
пользователя», выдавая наличие учётки наружу.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit, mail
from .config import get_settings
from .crypto import generate_token, hash_token
from .models import Invitation, Role, User

#: Достаточно строгая проверка, чтобы отсеять опечатки и заведомый мусор.
#: Валидировать почту регуляркой «по RFC» бессмысленно — настоящая проверка
#: адреса это доставленное письмо.
#: \A и \Z, а не ^ и $: в Python $ совпадает и перед завершающим переводом
#: строки — адрес с хвостовым \n прошёл бы проверку и утёк в заголовок письма.
EMAIL_RE = re.compile(r"\A[^@\s]+@[^@\s]+\.[A-Za-z]{2,}\Z")
MAX_EMAIL_LENGTH = 320

SUBJECT = "Приглашение в RTSP Gateway"


class InviteError(ValueError):
    """Приглашение выдать нельзя. Текст пригоден для показа администратору."""


def normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not EMAIL_RE.match(email):
        raise InviteError("Укажите корректный адрес электронной почты.")
    return email


def invite_url(token: str) -> str:
    return f"{get_settings().base_url}/invite/{token}"


# ─── Создание ────────────────────────────────────────────────────────────────
async def create(
    db: AsyncSession,
    *,
    email: str,
    full_name: str,
    role: Role,
    invited_by: User,
) -> tuple[Invitation, str]:
    """Создаёт приглашение и возвращает его вместе с токеном.

    Токен возвращается ровно один раз: дальше в базе только его SHA-256.
    Коммит — на стороне вызывающего кода.
    """
    email = normalize_email(email)

    if await db.scalar(select(User).where(User.email == email)) is not None:
        raise InviteError("Пользователь с таким адресом уже существует.")

    # Действующее приглашение на тот же адрес отзываем: у сотрудника должна
    # быть ровно одна рабочая ссылка, иначе непонятно, какая из них «та».
    for previous in await db.scalars(
        select(Invitation).where(
            Invitation.email == email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    ):
        previous.revoked_at = dt.datetime.now(dt.UTC)

    token = generate_token()
    invitation = Invitation(
        email=email,
        full_name=full_name.strip()[:200],
        role=role,
        token_hash=hash_token(token),
        expires_at=dt.datetime.now(dt.UTC)
        + dt.timedelta(hours=get_settings().invite_ttl_hours),
        invited_by_id=invited_by.id,
    )
    db.add(invitation)
    await db.flush()
    return invitation, token


async def reissue(db: AsyncSession, invitation: Invitation) -> str:
    """Перевыпускает токен и продлевает срок — «отправить письмо ещё раз».

    Старая ссылка перестаёт работать: если первое письмо ушло не туда,
    повторная отправка не должна оставлять первый адрес в игре.
    """
    token = generate_token()
    invitation.token_hash = hash_token(token)
    invitation.expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(
        hours=get_settings().invite_ttl_hours
    )
    invitation.sent_at = None
    return token


# ─── Письмо ──────────────────────────────────────────────────────────────────
def _render_email(invitation: Invitation, token: str, inviter: User) -> tuple[str, str]:
    """Текст и HTML письма. Возвращает (text, html)."""
    settings = get_settings()
    url = invite_url(token)
    who = inviter.full_name.strip() or inviter.email
    hours = settings.invite_ttl_hours
    name = invitation.full_name.strip()
    greeting = f"Здравствуйте, {name}!" if name else "Здравствуйте!"

    text = (
        f"{greeting}\n\n"
        f"{who} приглашает вас в RTSP Gateway — сервис просмотра камер.\n\n"
        f"Чтобы начать работу, откройте ссылку и задайте пароль:\n"
        f"{url}\n\n"
        f"Ссылка одноразовая и действует {hours} ч.\n"
        f"Если срок истёк, попросите администратора выслать приглашение заново.\n\n"
        f"Если вы не ждали этого письма — просто удалите его: "
        f"без перехода по ссылке учётная запись не создаётся.\n"
    )

    body = html.escape(url)
    html_body = f"""<!DOCTYPE html>
<html lang="ru"><body style="margin:0;padding:24px;background:#f6f7f9;
 font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1c1e21">
  <div style="max-width:520px;margin:0 auto;background:#fff;border-radius:12px;padding:28px">
    <p style="margin:0 0 16px;font-size:15px">{html.escape(greeting)}</p>
    <p style="margin:0 0 20px;font-size:15px;line-height:1.5">
      {html.escape(who)} приглашает вас в <strong>RTSP Gateway</strong> —
      сервис просмотра камер.
    </p>
    <p style="margin:0 0 24px">
      <a href="{body}" style="display:inline-block;padding:11px 20px;border-radius:8px;
         background:#1c1e21;color:#fff;text-decoration:none;font-size:15px">Задать пароль</a>
    </p>
    <p style="margin:0 0 8px;font-size:13px;color:#65676b">
      Ссылка одноразовая и действует {hours} ч. Если кнопка не работает,
      скопируйте адрес в браузер:
    </p>
    <p style="margin:0 0 20px;font-size:13px;word-break:break-all"><a href="{body}"
       style="color:#1a6ed8">{body}</a></p>
    <p style="margin:0;font-size:13px;color:#65676b">
      Если вы не ждали этого письма — удалите его: без перехода по ссылке
      учётная запись не создаётся.
    </p>
  </div>
</body></html>"""
    return text, html_body


async def send_email(invitation: Invitation, token: str, inviter: User) -> None:
    """Отправляет письмо. Бросает mail.MailError, если не получилось."""
    text_body, html_body = _render_email(invitation, token, inviter)
    await mail.send(
        to=invitation.email, subject=SUBJECT, text_body=text_body, html_body=html_body
    )
    invitation.sent_at = dt.datetime.now(dt.UTC)


# ─── Приём ───────────────────────────────────────────────────────────────────
async def resolve(db: AsyncSession, token: str) -> Invitation | None:
    """Находит действующее приглашение по токену из ссылки.

    Любая причина отказа (нет такого токена, отозвано, просрочено, уже
    использовано) даёт None: посторонний по ответу не должен различать
    «ссылки не было» и «ссылка была, но истекла».
    """
    if not token or len(token) > 128:
        return None
    invitation = await db.scalar(
        select(Invitation).where(Invitation.token_hash == hash_token(token))
    )
    if invitation is None or not invitation.is_pending or invitation.is_expired():
        return None
    return invitation


async def accept(
    db: AsyncSession,
    invitation: Invitation,
    *,
    password_hash: str,
    full_name: str,
    ip: str = "",
    user_agent: str = "",
) -> User:
    """Создаёт учётную запись по приглашению и гасит его.

    Коммит — на стороне вызывающего кода.
    """
    if await db.scalar(select(User).where(User.email == invitation.email)) is not None:
        # Учётку успели завести другим путём, пока письмо лежало в ящике.
        raise InviteError(
            "Учётная запись с этим адресом уже существует. Войдите или "
            "восстановите пароль через администратора."
        )

    user = User(
        email=invitation.email,
        full_name=(full_name.strip() or invitation.full_name)[:200],
        password_hash=password_hash,
        role=invitation.role,
        # Пароль пользователь задал сам — менять его при входе не нужно.
        must_change_password=False,
    )
    db.add(user)
    await db.flush()

    now = dt.datetime.now(dt.UTC)
    invitation.accepted_at = now
    invitation.accepted_user_id = user.id
    # Токен гасим сразу: ссылка из письма одноразовая.
    invitation.token_hash = hash_token(f"used:{uuid.uuid4()}")

    await audit.record(
        db,
        audit.INVITE_ACCEPTED,
        actor_id=user.id,
        actor_label=user.email,
        target_type="invite",
        target_id=str(invitation.id),
        ip=ip,
        user_agent=user_agent,
        meta={"role": user.role.value, "invited_by": str(invitation.invited_by_id or "")},
    )
    return user


async def pending_for_list(db: AsyncSession) -> list[Invitation]:
    """Неиспользованные приглашения — для страницы «Пользователи»."""
    return list(
        await db.scalars(
            select(Invitation)
            .where(Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))
            .order_by(Invitation.created_at.desc())
        )
    )
