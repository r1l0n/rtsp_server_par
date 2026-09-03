"""Рендеринг шаблонов и работа с cookie сессии."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..auth.sessions import SESSION_COOKIE, SessionData
from ..config import get_settings
from ..models import Role, User

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["app_name"] = "RTSP Gateway"
templates.env.globals["Role"] = Role


def _format_timestamp(value: float) -> str:
    """Unix-время из Redis-сессии -> локальная строка."""
    import datetime as dt

    if not value:
        return "—"
    return dt.datetime.fromtimestamp(value, dt.UTC).strftime("%d.%m.%Y %H:%M")


templates.env.filters["timestamp"] = _format_timestamp


def render(
    request: Request,
    template: str,
    *,
    status_code: int = 200,
    user: User | None = None,
    **context: Any,
) -> HTMLResponse:
    session: SessionData | None = getattr(request.state, "session", None)
    context.setdefault("user", user or getattr(request.state, "user", None))
    context.setdefault("status_code", status_code)
    context["csp_nonce"] = getattr(request.state, "csp_nonce", "")
    context["request_id"] = getattr(request.state, "request_id", "")
    context["csrf_token"] = session.csrf if session else ""
    return templates.TemplateResponse(request, template, context, status_code=status_code)


# ─── cookie ──────────────────────────────────────────────────────────────────
def set_session_cookie(response: Response, sid: str) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        max_age=settings.session_ttl_minutes * 60,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def redirect(url: str, *, status_code: int = 303) -> RedirectResponse:
    """303 See Other: после POST браузер обязан перейти на GET."""
    return RedirectResponse(url, status_code=status_code)


#: Сообщения после редиректа передаём кодом, а не текстом из запроса —
#: так в страницу физически не может попасть чужая строка.
NOTICES: dict[str, str] = {
    "camera_created": "Камера добавлена. Проба кодеков идёт в фоне.",
    "camera_updated": "Изменения сохранены.",
    "profile_changed": "Профиль потока изменён. Проверьте камеру заново.",
    "camera_deleted": "Камера удалена, поток остановлен.",
    "link_created": "Ссылка создана.",
    "link_revoked": "Ссылка отозвана. Активные зрители отключены.",
    "link_rotated": "Токен ссылки перевыпущен. Старый адрес больше не работает.",
    "link_deleted": "Ссылка удалена.",
    "totp_enabled": "Двухфакторная аутентификация включена.",
    "totp_disabled": "Двухфакторная аутентификация выключена.",
    "password_changed": "Пароль изменён.",
    "sessions_revoked": "Остальные сессии завершены.",
    "user_created": "Пользователь создан.",
    "user_updated": "Пользователь обновлён.",
    "logged_out": "Вы вышли из системы.",
}


def notice(code: str | None) -> str:
    return NOTICES.get(code or "", "")
