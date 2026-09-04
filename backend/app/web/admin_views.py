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
from .. import invites
from ..auth import ratelimit, sessions
from ..auth.deps import CsrfProtected, DbSession, Forbidden, RequireAdmin
from ..config import get_settings
from ..mail import MailError, MailNotConfigured
from ..middleware import client_ip
from ..models import AuditLog, Invitation, RecoveryCode, Role, User
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
        mail_enabled=get_settings().mail_enabled,
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
        await invites.send_email(invitation, token, admin)
    except MailError as exc:
        await audit_log.record(
            db, audit_log.INVITE_SEND_FAILED, actor_id=admin.id, target_type="invite",
            target_id=str(invitation.id), ip=ip,
            meta={"email": invitation.email, "error": type(exc).__name__},
        )
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
