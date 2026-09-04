"""Администрирование: пользователи, приглашения и журнал аудита."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit as audit_log
from .. import invites, mail
from ..auth import ratelimit, sessions
from ..auth.deps import CsrfProtected, DbSession, Forbidden, RequireAdmin
from ..config import get_settings
from ..crypto import get_cipher
from ..mail import MailError, MailNotConfigured
from ..middleware import client_ip
from ..models import AuditLog, Invitation, MailSecurity, MailSettings, RecoveryCode, Role, User
from .templating import notice, redirect, render

router = APIRouter(prefix="/admin", tags=["admin"])

AUDIT_PAGE_SIZE = 100


async def _users_page(
    request: Request, db: AsyncSession, admin: User, *, status_code: int = 200,
    **extra: object,
) -> HTMLResponse:
    """Страница «Пользователи»: список учёток и неиспользованные приглашения."""
    return render(
        request,
        "users.html",
        status_code=status_code,
        user=admin,
        users=list(await db.scalars(select(User).order_by(User.email))),
        invitations=await invites.pending_for_list(db),
        mail_enabled=(await mail.load_config(db)).enabled,
        invite_ttl_hours=get_settings().invite_ttl_hours,
        now=dt.datetime.now(dt.UTC),
        **extra,
    )


@router.get("/users", response_class=HTMLResponse)
async def users_list(request: Request, db: DbSession, admin: RequireAdmin) -> HTMLResponse:
    return await _users_page(
        request, db, admin, notice=notice(request.query_params.get("notice"))
    )


# ─── Приглашения ─────────────────────────────────────────────────────────────
async def _deliver(
    request: Request,
    db: AsyncSession,
    admin: User,
    invitation: Invitation,
    token: str,
    *,
    action: str,
) -> Response:
    """Пытается отправить письмо и отвечает страницей.

    Отказ SMTP не отменяет приглашение: ссылка уже выпущена и работает.
    Поэтому вместо ошибки администратор получает эту ссылку на экран — чтобы
    передать её сотруднику любым другим способом. Иначе внедрение упиралось бы
    в почтовый сервер, которого может не быть вовсе.
    """
    ip = client_ip(request)
    await audit_log.record(
        db, action, actor_id=admin.id, target_type="invite", target_id=str(invitation.id),
        ip=ip, meta={"email": invitation.email, "role": invitation.role.value},
    )

    try:
        await invites.send_email(invitation, token, admin, await mail.load_config(db))
    except MailError as exc:
        await audit_log.record(
            db, audit_log.INVITE_SEND_FAILED, actor_id=admin.id, target_type="invite",
            target_id=str(invitation.id), ip=ip,
            meta={"email": invitation.email, "error": type(exc).__name__},
        )
        if not isinstance(exc, MailNotConfigured):
            await mail.record_result(db, error=str(exc))
        await db.commit()
        hint = (
            "Письмо не отправлено, но приглашение выпущено — передайте ссылку сотруднику."
            if isinstance(exc, MailNotConfigured)
            else f"Письмо не отправлено: {exc} Приглашение выпущено — передайте ссылку сами."
        )
        return await _users_page(
            request, db, admin,
            invite_link=invites.invite_url(token),
            invite_link_email=invitation.email,
            error=hint,
        )

    await audit_log.record(
        db, audit_log.INVITE_SENT, actor_id=admin.id, target_type="invite",
        target_id=str(invitation.id), ip=ip, meta={"email": invitation.email},
    )
    await mail.record_result(db)
    await db.commit()
    return redirect("/admin/users?notice=invite_sent")


@router.post("/users/invite")
async def user_invite(
    request: Request,
    db: DbSession,
    admin: RequireAdmin,
    _: CsrfProtected,
    email: Annotated[str, Form()],
    role: Annotated[str, Form()] = Role.operator.value,
    full_name: Annotated[str, Form()] = "",
) -> Response:
    quota = await ratelimit.hit("invite_send", str(admin.id), ratelimit.INVITE_SEND_BY_ACTOR)
    if not quota.allowed:
        return await _users_page(
            request, db, admin, status_code=429,
            error="Слишком много приглашений за час. Повторите позже.",
        )

    try:
        chosen_role = Role(role)
    except ValueError:
        return await _users_page(request, db, admin, status_code=400, error="Неизвестная роль.")

    try:
        invitation, token = await invites.create(
            db, email=email, full_name=full_name, role=chosen_role, invited_by=admin
        )
    except invites.InviteError as exc:
        return await _users_page(request, db, admin, status_code=400, error=str(exc))

    return await _deliver(
        request, db, admin, invitation, token, action=audit_log.INVITE_CREATED
    )


@router.post("/invites/{invite_id}/resend")
async def invite_resend(
    request: Request, db: DbSession, admin: RequireAdmin, _: CsrfProtected, invite_id: uuid.UUID
) -> Response:
    invitation = await db.get(Invitation, invite_id)
    if invitation is None or not invitation.is_pending:
        raise Forbidden("Приглашение не найдено или уже использовано")

    quota = await ratelimit.hit("invite_send", str(admin.id), ratelimit.INVITE_SEND_BY_ACTOR)
    if not quota.allowed:
        return await _users_page(
            request, db, admin, status_code=429,
            error="Слишком много приглашений за час. Повторите позже.",
        )

    token = await invites.reissue(db, invitation)
    return await _deliver(
        request, db, admin, invitation, token, action=audit_log.INVITE_CREATED
    )


@router.post("/invites/{invite_id}/revoke")
async def invite_revoke(
    request: Request, db: DbSession, admin: RequireAdmin, _: CsrfProtected, invite_id: uuid.UUID
) -> RedirectResponse:
    invitation = await db.get(Invitation, invite_id)
    if invitation is None or not invitation.is_pending:
        raise Forbidden("Приглашение не найдено или уже использовано")

    invitation.revoked_at = dt.datetime.now(dt.UTC)
    await audit_log.record(
        db, audit_log.INVITE_REVOKED, actor_id=admin.id, target_type="invite",
        target_id=str(invitation.id), ip=client_ip(request), meta={"email": invitation.email},
    )
    await db.commit()
    return redirect("/admin/users?notice=invite_revoked")


# ─── Настройки почты ─────────────────────────────────────────────────────────
#: Пароль в форму не возвращается никогда. Пустое поле означает «оставить
#: прежний», и только явная галочка стирает сохранённый пароль.
KEEP_PASSWORD = ""

TEST_SUBJECT = "Проверка почты RTSP Gateway"


async def _mail_page(
    request: Request, db: AsyncSession, admin: User, *, status_code: int = 200,
    form: dict[str, object] | None = None, **extra: object,
) -> HTMLResponse:
    row = await mail.get_row(db)
    config = mail.config_from_row(row) if row is not None else mail.config_from_env()
    return render(
        request,
        "mail_settings.html",
        status_code=status_code,
        user=admin,
        row=row,
        config=config,
        # После неудачной проверки показываем то, что ввёл администратор,
        # а не то, что лежит в базе: иначе правки пропадают на глазах.
        form=form,
        env_host=get_settings().smtp_host.strip(),
        **extra,
    )


@router.get("/mail", response_class=HTMLResponse)
async def mail_settings_view(
    request: Request, db: DbSession, admin: RequireAdmin
) -> HTMLResponse:
    return await _mail_page(
        request, db, admin, notice=notice(request.query_params.get("notice"))
    )


@router.post("/mail")
async def mail_settings_save(
    request: Request,
    db: DbSession,
    admin: RequireAdmin,
    _: CsrfProtected,
    host: Annotated[str, Form()] = "",
    port: Annotated[str, Form()] = "587",
    security: Annotated[str, Form()] = MailSecurity.starttls.value,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = KEEP_PASSWORD,
    mail_from: Annotated[str, Form()] = "",
    from_name: Annotated[str, Form()] = "RTSP Gateway",
    timeout_seconds: Annotated[str, Form()] = "15",
    enabled: Annotated[str, Form()] = "",
    clear_password: Annotated[str, Form()] = "",
) -> Response:
    form: dict[str, object] = {
        "host": host.strip(), "port": port, "security": security,
        "username": username.strip(), "mail_from": mail_from.strip(),
        "from_name": from_name.strip(), "timeout_seconds": timeout_seconds,
        "enabled": bool(enabled),
    }

    async def fail(message: str) -> HTMLResponse:
        return await _mail_page(request, db, admin, status_code=400, form=form, error=message)

    is_on = bool(enabled)
    host = host.strip()
    if is_on and not host:
        return await fail("Укажите адрес SMTP-сервера или снимите галочку «Включить почту».")
    if any(char.isspace() for char in host):
        return await fail("Адрес сервера не должен содержать пробелов.")

    try:
        chosen_security = MailSecurity(security)
    except ValueError:
        return await fail("Неизвестный режим шифрования.")

    port_value = _positive_int(port, low=1, high=65535)
    if port_value is None:
        return await fail("Порт должен быть числом от 1 до 65535.")
    timeout_value = _positive_int(timeout_seconds, low=1, high=120)
    if timeout_value is None:
        return await fail("Таймаут должен быть числом от 1 до 120 секунд.")

    sender = mail_from.strip()
    if sender:
        try:
            sender = invites.normalize_email(sender)
        except invites.InviteError:
            return await fail("Адрес отправителя указан неверно.")
    elif is_on:
        return await fail("Укажите адрес отправителя — он попадёт в поле «От кого».")

    row = await mail.get_row(db)
    if row is None:
        row = MailSettings(id=1)
        db.add(row)

    row.enabled = is_on
    row.host = host[:255]
    row.port = port_value
    row.security = chosen_security
    row.username = username.strip()[:320]
    row.mail_from = sender[:320]
    row.from_name = (from_name.strip() or "RTSP Gateway")[:200]
    row.timeout_seconds = timeout_value
    row.updated_by_id = admin.id

    if clear_password:
        row.password_enc = None
    elif password != KEEP_PASSWORD:
        row.password_enc = get_cipher().encrypt(password)

    await audit_log.record(
        db, audit_log.MAIL_SETTINGS_UPDATED, actor_id=admin.id, target_type="mail",
        target_id="settings", ip=client_ip(request),
        meta={
            "enabled": row.enabled, "host": row.host, "port": row.port,
            "security": row.security.value, "username": row.username,
            # Пароль в журнал не попадает — только факт, что его меняли.
            "password_changed": bool(clear_password) or password != KEEP_PASSWORD,
        },
    )
    await db.commit()
    return redirect("/admin/mail?notice=mail_saved")


@router.post("/mail/test")
async def mail_settings_test(
    request: Request, db: DbSession, admin: RequireAdmin, _: CsrfProtected
) -> Response:
    """Отправляет проверочное письмо самому администратору.

    Иначе единственный способ проверить настройки — пригласить живого
    человека и ждать, пожалуется он или нет.
    """
    quota = await ratelimit.hit("mail_test", str(admin.id), ratelimit.MAIL_TEST_BY_ACTOR)
    if not quota.allowed:
        return await _mail_page(
            request, db, admin, status_code=429,
            error="Слишком много проверок подряд. Повторите через несколько минут.",
        )

    config = await mail.load_config(db)
    try:
        await mail.send(
            config,
            to=admin.email,
            subject=TEST_SUBJECT,
            text_body=(
                "Это проверочное письмо RTSP Gateway.\n\n"
                f"Если вы его читаете, отправка через {config.summary} работает "
                "и приглашения сотрудникам будут доходить.\n"
            ),
        )
    except MailError as exc:
        await audit_log.record(
            db, audit_log.MAIL_TEST_FAILED, actor_id=admin.id, target_type="mail",
            target_id="settings", ip=client_ip(request), meta={"error": str(exc)[:200]},
        )
        if not isinstance(exc, MailNotConfigured):
            await mail.record_result(db, error=str(exc))
        await db.commit()
        return await _mail_page(request, db, admin, status_code=400, error=str(exc))

    await audit_log.record(
        db, audit_log.MAIL_TEST_SENT, actor_id=admin.id, target_type="mail",
        target_id="settings", ip=client_ip(request), meta={"to": admin.email},
    )
    await mail.record_result(db)
    await db.commit()
    return redirect("/admin/mail?notice=mail_test_sent")


def _positive_int(raw: str, *, low: int, high: int) -> int | None:
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return None
    return value if low <= value <= high else None


@router.post("/users/{user_id}/toggle")
async def user_toggle(
    request: Request, db: DbSession, admin: RequireAdmin, _: CsrfProtected, user_id: uuid.UUID
) -> RedirectResponse:
    target = await db.get(User, user_id)
    if target is None:
        raise Forbidden("Пользователь не найден")
    if target.id == admin.id:
        raise Forbidden("Нельзя отключить собственную учётную запись")

    target.is_active = not target.is_active
    await audit_log.record(
        db,
        audit_log.USER_UPDATED if target.is_active else audit_log.USER_DISABLED,
        actor_id=admin.id, target_type="user", target_id=str(target.id), ip=client_ip(request),
        meta={"is_active": target.is_active},
    )
    await db.commit()

    if not target.is_active:
        # Отключение должно действовать немедленно, а не до истечения сессии.
        await sessions.delete_all_for_user(target.id)
    return redirect("/admin/users?notice=user_updated")


@router.post("/users/{user_id}/role")
async def user_role(
    request: Request,
    db: DbSession,
    admin: RequireAdmin,
    _: CsrfProtected,
    user_id: uuid.UUID,
    role: Annotated[str, Form()],
) -> RedirectResponse:
    target = await db.get(User, user_id)
    if target is None:
        raise Forbidden("Пользователь не найден")
    if target.id == admin.id:
        raise Forbidden("Нельзя изменить собственную роль")

    target.role = Role(role)
    await audit_log.record(
        db, audit_log.USER_UPDATED, actor_id=admin.id, target_type="user",
        target_id=str(target.id), ip=client_ip(request), meta={"role": role},
    )
    await db.commit()
    return redirect("/admin/users?notice=user_updated")


@router.post("/users/{user_id}/reset-2fa")
async def user_reset_totp(
    request: Request, db: DbSession, admin: RequireAdmin, _: CsrfProtected, user_id: uuid.UUID
) -> RedirectResponse:
    """Сброс второго фактора администратором — сценарий «потерял телефон».

    Опасная операция: после сброса вход защищён только паролем, пока
    пользователь не настроит 2FA заново. Поэтому она обязательно в аудите.
    """
    target = await db.get(User, user_id)
    if target is None:
        raise Forbidden("Пользователь не найден")

    target.totp_enabled = False
    target.totp_secret_enc = None
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == target.id))
    await audit_log.record(
        db, audit_log.TOTP_DISABLED, actor_id=admin.id, target_type="user",
        target_id=str(target.id), ip=client_ip(request), meta={"by_admin": True},
    )
    await db.commit()
    await sessions.delete_all_for_user(target.id)
    return redirect("/admin/users?notice=user_updated")


# ─── Журнал ──────────────────────────────────────────────────────────────────
@router.get("/audit", response_class=HTMLResponse)
async def audit_view(
    request: Request,
    db: DbSession,
    admin: RequireAdmin,
    action: str = "",
    page: int = 0,
) -> HTMLResponse:
    page = max(0, page)
    query = select(AuditLog).order_by(AuditLog.created_at.desc())
    if action:
        query = query.where(AuditLog.action == action)
    query = query.offset(page * AUDIT_PAGE_SIZE).limit(AUDIT_PAGE_SIZE)

    entries = list(await db.scalars(query))
    actors = {
        u.id: u.email
        for u in await db.scalars(
            select(User).where(User.id.in_({e.actor_id for e in entries if e.actor_id}))
        )
    }
    return render(
        request,
        "audit.html",
        user=admin,
        entries=entries,
        actors=actors,
        action=action,
        page=page,
        has_next=len(entries) == AUDIT_PAGE_SIZE,
    )
