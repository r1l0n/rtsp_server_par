"""Вход, второй фактор, выход."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from .. import audit, mail, recovery
from ..auth import ratelimit, sessions
from ..auth.deps import CsrfProtected, DbSession, SessionDep
from ..auth.passwords import (
    WeakPasswordError,
    hash_password,
    needs_rehash,
    validate_password_policy,
    verify_password,
)
from ..auth.totp import verify_code, verify_recovery_code
from ..config import get_settings
from ..crypto import get_cipher
from ..logging_setup import get_logger
from ..mail import MailError
from ..middleware import client_ip
from ..models import RecoveryCode, Role, User
from .templating import (
    clear_session_cookie,
    notice,
    redirect,
    render,
    set_session_cookie,
)

log = get_logger("auth")
router = APIRouter(tags=["auth"])

MAX_FAILED_ATTEMPTS = 10
LOCKOUT_MINUTES = 15

#: Одна и та же формулировка на «нет такого пользователя» и «неверный пароль»:
#: иначе форма входа превращается в справочник корпоративных учёток.
INVALID_CREDENTIALS = "Неверный адрес или пароль."

#: Хеш-заглушка: проверяем её, когда пользователь не найден, чтобы время
#: ответа не выдавало существование учётной записи.
_DUMMY_HASH = hash_password("dummy-password-for-constant-time-check")


def safe_next(value: str | None) -> str:
    """Защита от open redirect: принимаем только локальные пути."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


def totp_required_for(role: Role) -> bool:
    policy = get_settings().totp_policy
    if policy == "all":
        return True
    if policy == "admins":
        return role is Role.admin
    return False


# ─── Вход по паролю ──────────────────────────────────────────────────────────
@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, session: SessionDep, next: str = "/") -> HTMLResponse:
    if session is not None and not session.pending_2fa:
        return redirect(safe_next(next))  # type: ignore[return-value]
    return render(
        request,
        "login.html",
        next=safe_next(next),
        notice=notice(request.query_params.get("notice")),
    )


@router.post("/login")
async def login(
    request: Request,
    db: DbSession,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    ip = client_ip(request)
    email = email.strip().lower()
    next_url = safe_next(next)

    by_ip = await ratelimit.hit("login_ip", ip, ratelimit.LOGIN_BY_IP)
    by_account = await ratelimit.hit("login_acct", email, ratelimit.LOGIN_BY_ACCOUNT)
    if not by_ip.allowed or not by_account.allowed:
        wait = max(by_ip.retry_after, by_account.retry_after)
        return render(
            request,
            "login.html",
            status_code=429,
            next=next_url,
            email=email,
            error=f"Слишком много попыток. Повторите через {wait // 60 + 1} мин.",
        )

    user = await db.scalar(select(User).where(User.email == email))
    now = dt.datetime.now(dt.UTC)

    if user is None or not user.is_active:
        verify_password(_DUMMY_HASH, password)
        await audit.record(
            db, audit.LOGIN_FAILED, actor_label=email, ip=ip, meta={"reason": "unknown_or_disabled"}
        )
        await db.commit()
        return render(
            request, "login.html", status_code=401, next=next_url, email=email,
            error=INVALID_CREDENTIALS,
        )

    if user.locked_until is not None and user.locked_until > now:
        minutes = int((user.locked_until - now).total_seconds() // 60) + 1
        await audit.record(db, audit.LOGIN_LOCKED, actor_id=user.id, actor_label=email, ip=ip)
        await db.commit()
        return render(
            request, "login.html", status_code=429, next=next_url, email=email,
            error=f"Учётная запись временно заблокирована. Повторите через {minutes} мин.",
        )

    if not verify_password(user.password_hash, password):
        user.failed_attempts += 1
        reason = "bad_password"
        if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
            user.locked_until = now + dt.timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_attempts = 0
            reason = "locked_out"
        await audit.record(
            db, audit.LOGIN_FAILED, actor_id=user.id, actor_label=email, ip=ip,
            meta={"reason": reason},
        )
        await db.commit()
        return render(
            request, "login.html", status_code=401, next=next_url, email=email,
            error=INVALID_CREDENTIALS,
        )

    # Пароль верный.
    user.failed_attempts = 0
    user.locked_until = None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    user_agent = request.headers.get("user-agent", "")

    if user.totp_enabled:
        pending = await sessions.create(user.id, ip=ip, user_agent=user_agent, pending_2fa=True)
        await db.commit()
        response = redirect(f"/login/2fa?next={next_url}")
        set_session_cookie(response, pending.sid)
        return response

    user.last_login_at = now
    await audit.record(db, audit.LOGIN_OK, actor_id=user.id, actor_label=email, ip=ip,
                       user_agent=user_agent, meta={"totp": False})
    await db.commit()
    await ratelimit.reset("login_acct", email)

    session = await sessions.create(user.id, ip=ip, user_agent=user_agent)
    # Политика требует второй фактор, но он ещё не настроен — ведём настраивать.
    target = "/profile/2fa" if totp_required_for(user.role) else next_url
    response = redirect(target)
    set_session_cookie(response, session.sid)
    return response


# ─── Второй фактор ───────────────────────────────────────────────────────────
@router.get("/login/2fa", response_class=HTMLResponse)
async def totp_form(request: Request, session: SessionDep, next: str = "/") -> HTMLResponse:
    if session is None:
        return redirect("/login")  # type: ignore[return-value]
    if not session.pending_2fa:
        return redirect(safe_next(next))  # type: ignore[return-value]
    return render(request, "totp_verify.html", next=safe_next(next))


@router.post("/login/2fa")
async def totp_verify(
    request: Request,
    db: DbSession,
    session: SessionDep,
    _: CsrfProtected,
    code: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    if session is None or not session.pending_2fa:
        return redirect("/login")
    ip = client_ip(request)
    next_url = safe_next(next)

    limited = await ratelimit.hit("totp", session.sid, ratelimit.TOTP_BY_SESSION)
    if not limited.allowed:
        await sessions.delete(session.sid)
        response = redirect("/login")
        clear_session_cookie(response)
        return response

    user = await db.get(User, uuid.UUID(session.user_id))
    if user is None or user.totp_secret_enc is None:
        await sessions.delete(session.sid)
        return redirect("/login")

    secret = get_cipher().decrypt(user.totp_secret_enc)
    ok = await verify_code(secret, code, user.id)
    used_recovery = False

    if not ok:
        ok, used_recovery = await _try_recovery_code(db, user, code)

    if not ok:
        await audit.record(db, audit.TOTP_FAILED, actor_id=user.id, actor_label=user.email, ip=ip)
        await db.commit()
        return render(
            request, "totp_verify.html", status_code=401, next=next_url,
            error="Неверный код. Осталось попыток: " f"{limited.remaining}.",
        )

    user.last_login_at = dt.datetime.now(dt.UTC)
    await audit.record(
        db, audit.TOTP_RECOVERY_USED if used_recovery else audit.LOGIN_OK,
        actor_id=user.id, actor_label=user.email, ip=ip,
        user_agent=request.headers.get("user-agent", ""), meta={"totp": True},
    )
    await db.commit()
    await ratelimit.reset("login_acct", user.email)

    # Смена уровня привилегий — новый идентификатор сессии (session fixation).
    fresh = await sessions.rotate(session, pending_2fa=False)
    response = redirect(next_url)
    set_session_cookie(response, fresh.sid)
    return response


async def _try_recovery_code(db: DbSession, user: User, code: str) -> tuple[bool, bool]:
    """Проверяет код восстановления и гасит его при совпадении."""
    codes = await db.scalars(
        select(RecoveryCode).where(
            RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None)
        )
    )
    for candidate in codes:
        if verify_recovery_code(candidate.code_hash, code):
            candidate.used_at = dt.datetime.now(dt.UTC)
            return True, True
    return False, False


# ─── Забыл пароль ────────────────────────────────────────────────────────────
#: Один и тот же ответ на любой введённый адрес. Иначе форма превращается
#: в справочник: «письмо отправлено» против «нет такого» выдаёт, кто работает
#: в компании, а перебором — весь список сотрудников.
RESET_SENT = (
    "Если такая учётная запись существует, письмо со ссылкой уже отправлено. "
    "Проверьте почту, в том числе папку «Спам»."
)


@router.get("/forgot", response_class=HTMLResponse)
async def forgot_form(request: Request) -> HTMLResponse:
    return render(request, "forgot.html")


@router.post("/forgot", response_class=HTMLResponse)
async def forgot_submit(
    request: Request, db: DbSession, email: Annotated[str, Form()] = ""
) -> HTMLResponse:
    ip = client_ip(request)
    email = email.strip().lower()

    by_ip = await ratelimit.hit("reset_ip", ip, ratelimit.RESET_BY_IP)
    by_account = await ratelimit.hit("reset_acct", email, ratelimit.RESET_BY_ACCOUNT)
    if not by_ip.allowed or not by_account.allowed:
        # И здесь ответ прежний: «слишком часто» на чужой адрес тоже сообщало бы,
        # что адрес существует.
        return render(request, "forgot.html", done=True, message=RESET_SENT)

    user = await recovery.user_by_email(db, email)
    await audit.record(
        db, audit.PASSWORD_RESET_REQUESTED,
        actor_id=user.id if user else None, actor_label=email, ip=ip,
        meta={"known": user is not None},
    )

    if user is not None:
        token = await recovery.create(db, user, ip=ip)
        await db.commit()
        try:
            await recovery.send_email(user, token, await mail.load_config(db))
        except MailError as exc:
            # Ссылку показать нельзя даже администратору: её запросил кто угодно
            # с формы входа. Пишем в журнал сервиса и молчим в интерфейсе.
            log.error("reset_mail_failed", email=email, error=str(exc))
    else:
        await db.commit()

    return render(request, "forgot.html", done=True, message=RESET_SENT)


@router.get("/reset/{token}", response_class=HTMLResponse)
async def reset_form(request: Request, db: DbSession, token: str) -> HTMLResponse:
    found = await recovery.resolve(db, token)
    if found is None:
        return render(
            request, "error.html", status_code=404,
            message="Ссылка недействительна или срок её действия истёк. "
                    "Запросите восстановление пароля заново.",
        )
    _, user = found
    return render(request, "reset_password.html", token=token, email=user.email)


@router.post("/reset/{token}")
async def reset_submit(
    request: Request,
    db: DbSession,
    token: str,
    password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
) -> Response:
    found = await recovery.resolve(db, token)
    if found is None:
        return render(
            request, "error.html", status_code=404,
            message="Ссылка недействительна или срок её действия истёк. "
                    "Запросите восстановление пароля заново.",
        )
    row, user = found

    def fail(message: str) -> HTMLResponse:
        return render(
            request, "reset_password.html", status_code=400,
            token=token, email=user.email, error=message,
        )

    if password != confirm_password:
        return fail("Пароль и подтверждение не совпадают.")
    try:
        validate_password_policy(password, email=user.email)
    except WeakPasswordError as exc:
        return fail(str(exc))

    user.password_hash = hash_password(password)
    user.must_change_password = False
    user.failed_attempts = 0
    user.locked_until = None
    row.used_at = dt.datetime.now(dt.UTC)

    await audit.record(
        db, audit.PASSWORD_RESET_DONE, actor_id=user.id, actor_label=user.email,
        ip=client_ip(request),
    )
    await db.commit()

    # Смена пароля обрывает все сеансы: если в учётную запись уже зашли чужие,
    # восстановление пароля должно их выкинуть.
    await sessions.delete_all_for_user(user.id)
    return redirect("/login?notice=password_reset")


# ─── Выход ───────────────────────────────────────────────────────────────────
@router.post("/logout")
async def logout(request: Request, db: DbSession, session: SessionDep) -> RedirectResponse:
    if session is not None:
        if session.user_id:
            await audit.record(
                db, audit.LOGOUT, actor_id=uuid.UUID(session.user_id), ip=client_ip(request)
            )
            await db.commit()
        await sessions.delete(session.sid)
    response = redirect("/login?notice=logged_out")
    clear_session_cookie(response)
    return response
