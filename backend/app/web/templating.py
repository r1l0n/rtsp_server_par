"""Рендеринг шаблонов и работа с cookie сессии."""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..auth.sessions import SESSION_COOKIE, SessionData
from ..config import get_settings
from ..models import Role, User

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["app_name"] = "RTSP"


def _asset_version() -> str:
    """Отпечаток статики для ?v= в ссылках на css и js.

    Caddy отдаёт /static/ с `max-age=3600`, поэтому без этого браузер ещё час
    после обновления рисует страницу старой таблицей стилей — и выглядит это
    как «вёрстка поехала», а не как «файл из кэша».
    """
    digest = hashlib.sha256()
    for name in sorted(p.name for p in STATIC_DIR.glob("*.css")) + sorted(
        p.name for p in STATIC_DIR.glob("*.js")
    ):
        digest.update((STATIC_DIR / name).read_bytes())
    return digest.hexdigest()[:10]


templates.env.globals["asset_v"] = _asset_version()

#: Русские числительные: «1 ссылка», «2 ссылки», «5 ссылок». Без этого
#: в интерфейсе висело «1 ссылок».
def plural(count: int, one: str, few: str, many: str) -> str:
    tail_100 = count % 100
    tail_10 = count % 10
    if 11 <= tail_100 <= 14:
        word = many
    elif tail_10 == 1:
        word = one
    elif 2 <= tail_10 <= 4:
        word = few
    else:
        word = many
    return f"{count} {word}"


templates.env.globals["plural"] = plural
templates.env.globals["Role"] = Role

#: Состояния камеры и роли по-русски. Раньше в интерфейс попадали сами значения
#: enum («idle», «operator»), и оператору приходилось догадываться, что idle —
#: это норма для камеры on-demand, а не поломка.
CAMERA_STATUS_LABELS: dict[str, str] = {
    "online": "в эфире",
    "idle": "ждёт зрителя",
    "offline": "нет связи",
    "error": "ошибка",
    "unknown": "не проверена",
}

ROLE_LABELS: dict[str, str] = {
    "admin": "администратор",
    "operator": "оператор",
    "viewer": "наблюдатель",
}

#: Журнал читают люди, а не грепают машины. Ключи остаются английскими —
#: по ним фильтруют и их же видно в логах сервиса.
AUDIT_ACTION_LABELS: dict[str, str] = {
    "login.ok": "вход выполнен",
    "login.failed": "неудачный вход",
    "login.locked": "учётная запись заблокирована",
    "logout": "выход",
    "totp.enabled": "двухфакторная включена",
    "totp.disabled": "двухфакторная выключена",
    "totp.failed": "неверный код подтверждения",
    "totp.recovery_used": "вход по коду восстановления",
    "password.changed": "пароль изменён",
    "password.reset_requested": "запрошено восстановление пароля",
    "password.reset_done": "пароль восстановлен по ссылке",
    "session.revoked": "остальные сеансы завершены",
    "user.created": "пользователь создан",
    "user.updated": "пользователь изменён",
    "user.disabled": "пользователь отключён",
    "invite.created": "приглашение выписано",
    "invite.sent": "приглашение отправлено",
    "invite.send_failed": "письмо с приглашением не ушло",
    "invite.revoked": "приглашение отозвано",
    "invite.accepted": "приглашение принято",
    "camera.created": "камера добавлена",
    "camera.updated": "камера изменена",
    "camera.deleted": "камера удалена",
    "link.created": "ссылка выдана",
    "link.revoked": "ссылка отозвана",
    "link.rotated": "адрес ссылки перевыпущен",
    "link.deleted": "ссылка удалена",
    "link.viewed": "просмотр по ссылке",
    "link.denied": "отказано в доступе",
    "mail.settings_updated": "настройки почты изменены",
    "mail.test_sent": "проверочное письмо отправлено",
    "mail.test_failed": "проверочное письмо не ушло",
}

templates.env.globals["audit_action_labels"] = AUDIT_ACTION_LABELS
templates.env.globals["camera_status_labels"] = CAMERA_STATUS_LABELS
templates.env.globals["role_labels"] = ROLE_LABELS


#: Время человеку показываем местное, в БД и логах всё остаётся в UTC.
#: Отдельный фильтр, а не strftime по месту: иначе часть страниц осталась бы
#: в UTC, и никто бы не заметил — расхождение видно только рядом с часами.
DATETIME_FORMAT = "%d.%m.%Y %H:%M"


@functools.lru_cache(maxsize=4)
def display_tz(name: str) -> ZoneInfo | dt.tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # База часовых поясов не установлена — показываем UTC, но не падаем:
        # неверное время в журнале лучше, чем пятисотка на каждой странице.
        return dt.UTC


def format_datetime(value: dt.datetime | None, fmt: str = DATETIME_FORMAT) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(display_tz(get_settings().display_timezone)).strftime(fmt)


def _format_timestamp(value: float) -> str:
    """Unix-время из Redis-сессии -> местная строка."""
    if not value:
        return "—"
    return format_datetime(dt.datetime.fromtimestamp(value, dt.UTC))


templates.env.filters["timestamp"] = _format_timestamp
templates.env.filters["dt"] = format_datetime
def timezone_label() -> str:
    """Смещение вместо имени зоны: «UTC+5» понятно без карты часовых поясов."""
    offset = dt.datetime.now(display_tz(get_settings().display_timezone)).utcoffset()
    if offset is None:
        return "UTC"
    hours, remainder = divmod(int(offset.total_seconds()), 3600)
    minutes = remainder // 60
    return f"UTC{hours:+d}" + (f":{minutes:02d}" if minutes else "")


templates.env.globals["timezone_label"] = timezone_label


#: Тема оформления. Хранится в cookie, а не в профиле: она про устройство,
#: а не про человека — в тёмной аппаратной и на светлом ноутбуке удобны разные.
THEME_COOKIE = "theme"
DEFAULT_THEME = "dark"

THEMES: dict[str, str] = {
    "dark": "Тёмная",
    "light": "Светлая",
    "auto": "Как в системе",
}


def current_theme(request: Request) -> str:
    """Тема из cookie. Незнакомое значение молча превращается в тёмную."""
    value = request.cookies.get(THEME_COOKIE, "")
    return value if value in THEMES else DEFAULT_THEME


def set_theme_cookie(response: Response, theme: str) -> None:
    settings = get_settings()
    response.set_cookie(
        THEME_COOKIE,
        theme if theme in THEMES else DEFAULT_THEME,
        # Год: настройка оформления не должна теряться вместе с сессией.
        max_age=365 * 24 * 3600,
        httponly=False,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


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
    context.setdefault("theme", current_theme(request))
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
    "theme_saved": "Тема сохранена.",
    "user_updated": "Пользователь обновлён.",
    "invite_sent": "Приглашение отправлено на указанный адрес.",
    "invite_revoked": "Приглашение отозвано. Ссылка из письма больше не работает.",
    "invite_accepted": "Пароль задан. Войдите с вашим адресом и новым паролем.",
    "mail_saved": "Настройки почты сохранены.",
    "mail_test_sent": "Проверочное письмо отправлено. Проверьте ящик — и папку «Спам».",
    "logged_out": "Вы вышли из системы.",
    "password_reset": "Пароль изменён. Войдите с новым паролем.",
}


def notice(code: str | None) -> str:
    return NOTICES.get(code or "", "")
