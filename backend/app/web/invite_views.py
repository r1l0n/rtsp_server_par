"""Принятие приглашения: сотрудник задаёт пароль по ссылке из письма.

Страница открыта без сессии — на ней нет CSRF-токена, как и на форме входа.
Роль токена играет сам адрес: он одноразовый, 32 байта энтропии, и подделать
переход на него со стороны нечем — злоумышленник, знающий токен, и так может
задать пароль напрямую.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from .. import invites
from ..auth import ratelimit
from ..auth.deps import DbSession
from ..auth.passwords import WeakPasswordError, hash_password, validate_password_policy
from ..logging_setup import get_logger
from ..middleware import client_ip
from .templating import redirect, render

log = get_logger("invite")
router = APIRouter(tags=["invite"])

#: Одна формулировка на все причины отказа: по тексту нельзя понять, был ли
#: такой токен вообще, истёк он или уже использован.
GENERIC_INVALID = (
    "Ссылка недействительна: срок её действия истёк, она уже использована "
    "или отозвана. Попросите администратора выслать приглашение заново."
)


def _invalid(request: Request) -> HTMLResponse:
    return render(request, "error.html", status_code=404, message=GENERIC_INVALID)


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_form(request: Request, db: DbSession, token: str) -> HTMLResponse:
    limited = await ratelimit.hit("invite", client_ip(request), ratelimit.INVITE_BY_IP)
    if not limited.allowed:
        return render(
            request, "error.html", status_code=429,
            message="Слишком много попыток. Повторите через несколько минут.",
        )

    invitation = await invites.resolve(db, token)
    if invitation is None:
        return _invalid(request)

    return render(
        request,
        "invite_accept.html",
        token=token,
        email=invitation.email,
        full_name=invitation.full_name,
        role=invitation.role.value,
        expires_at=invitation.expires_at,
    )


@router.post("/invite/{token}")
async def invite_accept(
    request: Request,
    db: DbSession,
    token: str,
    password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
    full_name: Annotated[str, Form()] = "",
) -> Response:
    limited = await ratelimit.hit("invite", client_ip(request), ratelimit.INVITE_BY_IP)
    if not limited.allowed:
        return render(
            request, "error.html", status_code=429,
            message="Слишком много попыток. Повторите через несколько минут.",
        )

    invitation = await invites.resolve(db, token)
    if invitation is None:
        return _invalid(request)

    def fail(message: str) -> HTMLResponse:
        return render(
            request, "invite_accept.html", status_code=400,
            token=token, email=invitation.email, full_name=full_name or invitation.full_name,
            role=invitation.role.value, expires_at=invitation.expires_at, error=message,
        )

    if password != confirm_password:
        return fail("Пароль и подтверждение не совпадают.")
    try:
        validate_password_policy(password, email=invitation.email)
    except WeakPasswordError as exc:
        return fail(f"Пароль не подходит: {exc}.")

    try:
        user = await invites.accept(
            db,
            invitation,
            password_hash=hash_password(password),
            full_name=full_name,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        )
    except invites.InviteError as exc:
        return fail(str(exc))

    await db.commit()
    log.info("invite_accepted", user_id=str(user.id), role=user.role.value)

    # Автоматически не пускаем: пусть первый вход пройдёт обычным путём —
    # с паролем и, если политика требует, с настройкой второго фактора.
    return redirect("/login?notice=invite_accepted")
