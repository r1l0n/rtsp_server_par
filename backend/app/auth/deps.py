"""FastAPI-зависимости авторизации."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Role, User
from . import sessions
from .sessions import SESSION_COOKIE, SessionData


class AuthRequired(Exception):
    """Нет действительной сессии — отправляем на страницу входа."""

    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


class TwoFactorRequired(Exception):
    """Пароль принят, но второй фактор ещё не подтверждён."""


class Forbidden(Exception):
    """Аутентифицирован, но не имеет права на это действие."""

    def __init__(self, detail: str = "Недостаточно прав") -> None:
        self.detail = detail


class CsrfError(Exception):
    """Не совпал CSRF-токен."""


DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_session_data(request: Request) -> SessionData | None:
    session = await sessions.load(request.cookies.get(SESSION_COOKIE))
    request.state.session = session
    return session


SessionDep = Annotated[SessionData | None, Depends(get_session_data)]


async def current_user_optional(
    request: Request, db: DbSession, session: SessionDep
) -> User | None:
    if session is None or session.pending_2fa:
        return None
    try:
        user_id = uuid.UUID(session.user_id)
    except ValueError:
        return None
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        # Пользователь удалён или отключён, пока сессия была жива.
        await sessions.delete(session.sid)
        return None
    request.state.user = user
    return user


OptionalUser = Annotated[User | None, Depends(current_user_optional)]


async def require_user(request: Request, session: SessionDep, user: OptionalUser) -> User:
    if session is not None and session.pending_2fa:
        raise TwoFactorRequired
    if user is None:
        raise AuthRequired(next_url=request.url.path)
    # Скользящее окно: активный пользователь не выкидывается по таймауту.
    await sessions.touch(session.sid)  # type: ignore[union-attr]
    return user


CurrentUser = Annotated[User, Depends(require_user)]


def require_role(*roles: Role):
    """Зависимость, пускающая только перечисленные роли."""

    async def dependency(user: CurrentUser) -> User:
        if user.role not in roles:
            raise Forbidden(f"Требуется роль: {', '.join(r.value for r in roles)}")
        return user

    return dependency


RequireAdmin = Annotated[User, Depends(require_role(Role.admin))]
RequireOperator = Annotated[User, Depends(require_role(Role.admin, Role.operator))]


async def require_csrf(request: Request, session: SessionDep) -> None:
    """Проверяет CSRF-токен на любом изменяющем состояние запросе.

    Токен лежит в серверной сессии и приходит скрытым полем формы либо
    заголовком X-CSRF-Token. Вместе с SameSite=Lax это закрывает CSRF даже
    при наличии XSS-независимых межсайтовых POST'ов.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if session is None:
        raise AuthRequired(next_url=request.url.path)

    supplied = request.headers.get("x-csrf-token", "")
    if not supplied:
        form = await request.form()
        raw = form.get("csrf_token", "")
        supplied = raw if isinstance(raw, str) else ""

    import hmac

    if not supplied or not hmac.compare_digest(supplied, session.csrf):
        raise CsrfError


CsrfProtected = Annotated[None, Depends(require_csrf)]
