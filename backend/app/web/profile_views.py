"""Профиль: пароль, второй фактор, активные сессии."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, select

from .. import audit
from ..auth import sessions
from ..auth.deps import CsrfProtected, CurrentUser, DbSession, SessionDep
from ..auth.passwords import (
    WeakPasswordError,
    hash_password,
    validate_password_policy,
    verify_password,
)
from ..auth.totp import (
    generate_recovery_codes,
    hash_recovery_code,
    new_secret,
    provisioning_uri,
    qr_svg,
    verify_code,
)
from ..config import get_settings
from ..crypto import get_cipher
from ..middleware import client_ip
from ..models import RecoveryCode
from ..redis_client import get_redis
from .auth_views import totp_required_for
from .templating import notice, redirect, render

router = APIRouter(tags=["profile"])

_SETUP_KEY = "totp_setup:"
_SETUP_TTL = 600


@router.get("/profile", response_class=HTMLResponse)
async def profile(request: Request, user: CurrentUser, session: SessionDep) -> HTMLResponse:
    active = await sessions.list_for_user(user.id)
    return render(
        request,
        "profile.html",
        user=user,
        sessions=active,
        current_sid=session.sid if session else "",
        totp_mandatory=totp_required_for(user.role),
        notice=notice(request.query_params.get("notice")),
    )


# ─── Пароль ──────────────────────────────────────────────────────────────────
@router.post("/profile/password")
async def change_password(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    session: SessionDep,
    _: CsrfProtected,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
) -> Response:
    def fail(message: str) -> HTMLResponse:
        return render(
            request, "profile.html", status_code=400, user=user,
            sessions=[], current_sid=session.sid if session else "",
            totp_mandatory=totp_required_for(user.role), password_error=message,
        )

    if not verify_password(user.password_hash, current_password):
        return fail("Текущий пароль указан неверно.")
    if new_password != confirm_password:
        return fail("Новый пароль и подтверждение не совпадают.")
    try:
        validate_password_policy(new_password, email=user.email)
    except WeakPasswordError as exc:
        return fail(str(exc))

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    await audit.record(db, audit.PASSWORD_CHANGED, actor_id=user.id, ip=client_ip(request))
    await db.commit()

    # Смена пароля обесценивает украденные сессии — гасим все, кроме текущей.
    await sessions.delete_all_for_user(user.id, except_sid=session.sid if session else None)
    return redirect("/profile?notice=password_changed")


# ─── Второй фактор ───────────────────────────────────────────────────────────
@router.get("/profile/2fa", response_class=HTMLResponse)
async def totp_setup(request: Request, user: CurrentUser) -> HTMLResponse:
    if user.totp_enabled:
        return render(
            request, "profile_2fa.html", user=user, already_enabled=True,
            totp_mandatory=totp_required_for(user.role),
        )

    secret = new_secret()
    await get_redis().set(f"{_SETUP_KEY}{user.id}", secret, ex=_SETUP_TTL)

    uri = provisioning_uri(secret, user.email, issuer=get_settings().domain)
    return render(
        request,
        "profile_2fa.html",
        user=user,
        already_enabled=False,
        secret=secret,
        qr=qr_svg(uri),
        totp_mandatory=totp_required_for(user.role),
    )


@router.post("/profile/2fa/enable")
async def totp_enable(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _: CsrfProtected,
    code: Annotated[str, Form()],
) -> HTMLResponse:
    redis = get_redis()
    secret = await redis.get(f"{_SETUP_KEY}{user.id}")
    if not secret:
        return render(
            request, "profile_2fa.html", status_code=400, user=user, already_enabled=False,
            totp_mandatory=totp_required_for(user.role),
            error="Время настройки истекло. Откройте страницу заново.",
        )

    if not await verify_code(secret, code, user.id):
        uri = provisioning_uri(secret, user.email, issuer=get_settings().domain)
        return render(
            request, "profile_2fa.html", status_code=400, user=user, already_enabled=False,
            secret=secret, qr=qr_svg(uri), totp_mandatory=totp_required_for(user.role),
            error="Код не подошёл. Проверьте время на телефоне и попробуйте ещё раз.",
        )

    user.totp_secret_enc = get_cipher().encrypt(secret)
    user.totp_enabled = True

    # Старые коды восстановления недействительны после переподключения 2FA.
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    codes = generate_recovery_codes()
    for code_value in codes:
        db.add(RecoveryCode(user_id=user.id, code_hash=hash_recovery_code(code_value)))

    await audit.record(db, audit.TOTP_ENABLED, actor_id=user.id, ip=client_ip(request))
    await db.commit()
    await redis.delete(f"{_SETUP_KEY}{user.id}")

    # Коды показываем ровно один раз — в БД лежат только их хеши.
    return render(request, "profile_2fa_codes.html", user=user, codes=codes)


@router.post("/profile/2fa/disable")
async def totp_disable(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _: CsrfProtected,
    current_password: Annotated[str, Form()],
) -> Response:
    if totp_required_for(user.role):
        return render(
            request, "profile_2fa.html", status_code=403, user=user, already_enabled=True,
            totp_mandatory=True,
            error="Политика компании требует второй фактор для вашей роли — отключить нельзя.",
        )
    if not verify_password(user.password_hash, current_password):
        return render(
            request, "profile_2fa.html", status_code=400, user=user, already_enabled=True,
            totp_mandatory=False, error="Пароль указан неверно.",
        )

    user.totp_enabled = False
    user.totp_secret_enc = None
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    await audit.record(db, audit.TOTP_DISABLED, actor_id=user.id, ip=client_ip(request))
    await db.commit()
    return redirect("/profile?notice=totp_disabled")


@router.get("/profile/2fa/codes", response_class=HTMLResponse)
async def recovery_codes_status(
    request: Request, db: DbSession, user: CurrentUser
) -> HTMLResponse:
    remaining = len(
        list(
            await db.scalars(
                select(RecoveryCode).where(
                    RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None)
                )
            )
        )
    )
    return render(request, "profile_2fa_codes.html", user=user, codes=None, remaining=remaining)


# ─── Сессии ──────────────────────────────────────────────────────────────────
@router.post("/profile/sessions/revoke")
async def revoke_sessions(
    request: Request, db: DbSession, user: CurrentUser, session: SessionDep, _: CsrfProtected
) -> RedirectResponse:
    removed = await sessions.delete_all_for_user(
        user.id, except_sid=session.sid if session else None
    )
    await audit.record(
        db, audit.SESSION_REVOKED, actor_id=user.id, ip=client_ip(request),
        meta={"count": removed, "at": dt.datetime.now(dt.UTC).isoformat()},
    )
    await db.commit()
    return redirect("/profile?notice=sessions_revoked")
