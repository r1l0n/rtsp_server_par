"""Администрирование: пользователи и журнал аудита."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, select

from .. import audit as audit_log
from ..auth import sessions
from ..auth.deps import CsrfProtected, DbSession, Forbidden, RequireAdmin
from ..auth.passwords import WeakPasswordError, hash_password, validate_password_policy
from ..middleware import client_ip
from ..models import AuditLog, RecoveryCode, Role, User
from .templating import notice, redirect, render

router = APIRouter(prefix="/admin", tags=["admin"])

AUDIT_PAGE_SIZE = 100


@router.get("/users", response_class=HTMLResponse)
async def users_list(request: Request, db: DbSession, admin: RequireAdmin) -> HTMLResponse:
    users = list(await db.scalars(select(User).order_by(User.email)))
    return render(
        request,
        "users.html",
        user=admin,
        users=users,
        notice=notice(request.query_params.get("notice")),
    )


@router.post("/users")
async def user_create(
    request: Request,
    db: DbSession,
    admin: RequireAdmin,
    _: CsrfProtected,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()] = Role.operator.value,
    full_name: Annotated[str, Form()] = "",
) -> Response:
    email = email.strip().lower()

    async def fail(message: str) -> HTMLResponse:
        users = list(await db.scalars(select(User).order_by(User.email)))
        return render(
            request, "users.html", status_code=400, user=admin, users=users, error=message,
        )

    if "@" not in email or len(email) > 320:
        return await fail("Укажите корректный адрес электронной почты.")
    if await db.scalar(select(User).where(User.email == email)) is not None:
        return await fail("Пользователь с таким адресом уже существует.")
    try:
        validate_password_policy(password, email=email)
    except WeakPasswordError as exc:
        return await fail(str(exc))

    user = User(
        email=email,
        full_name=full_name.strip()[:200],
        password_hash=hash_password(password),
        role=Role(role),
        # Пароль задал администратор — при первом входе пользователь его меняет.
        must_change_password=True,
    )
    db.add(user)
    await db.flush()
    await audit_log.record(
        db, audit_log.USER_CREATED, actor_id=admin.id, target_type="user",
        target_id=str(user.id), ip=client_ip(request), meta={"email": email, "role": role},
    )
    await db.commit()
    return redirect("/admin/users?notice=user_created")


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
