"""Настройки: оформление и почта.

Раздел отделён от «Пользователей» и «Журнала» намеренно: там смотрят, кто и
что сделал, а здесь меняют поведение самого сервиса. Тема доступна каждому —
это личная настройка браузера; почта только администратору — она общая
и затрагивает всех.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit as audit_log
from .. import invites, mail
from ..auth import ratelimit
from ..auth.deps import CsrfProtected, CurrentUser, DbSession, RequireAdmin
from ..config import get_settings
from ..crypto import get_cipher
from ..mail import MailError, MailNotConfigured
from ..middleware import client_ip
from ..models import MailSecurity, MailSettings, Role, User
from .templating import THEMES, current_theme, notice, redirect, render, set_theme_cookie

router = APIRouter(prefix="/settings", tags=["settings"])


#: Куда вернуть пользователя после сохранения. Настройки открываются окном
#: поверх страницы, и уводить с неё после «Применить» нельзя.
def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


@router.get("", include_in_schema=False)
async def settings_root() -> RedirectResponse:
    return redirect("/settings/theme", status_code=307)


@router.get("/dialog", response_class=HTMLResponse)
async def settings_dialog(
    request: Request, db: DbSession, user: CurrentUser, next: str = "/"
) -> HTMLResponse:
    """Содержимое окна настроек — без каркаса страницы.

    Форма почты видна только администратору, и данные для неё читаются
    только когда он действительно открыл окно.
    """
    row = await mail.get_row(db) if user.role is Role.admin else None
    config = (
        mail.config_from_row(row)
        if row is not None
        else (mail.config_from_env() if user.role is Role.admin else None)
    )
    return render(
        request,
        "_settings_dialog.html",
        user=user,
        themes=THEMES,
        theme=current_theme(request),
        row=row,
        config=config,
        next=_safe_next(next),
    )


# ─── Тема ────────────────────────────────────────────────────────────────────
@router.get("/theme", response_class=HTMLResponse)
async def theme_view(request: Request, user: CurrentUser) -> HTMLResponse:
    return render(
        request,
        "settings_theme.html",
        user=user,
        themes=THEMES,
        notice=notice(request.query_params.get("notice")),
    )


@router.post("/theme")
async def theme_save(
    request: Request,
    user: CurrentUser,
    _: CsrfProtected,
    theme: Annotated[str, Form()] = "dark",
    next: Annotated[str, Form()] = "/",
) -> Response:
    """Тема живёт в cookie, а не в профиле.

    Она про устройство, а не про человека: с рабочего монитора в тёмной
    аппаратной и с ноутбука на свету удобны разные. Заодно не нужна ни колонка
    в БД, ни миграция.
    """
    if theme not in THEMES:
        theme = "dark"
    response = redirect(_safe_next(next))
    set_theme_cookie(response, theme)
    return response


# ─── Почта ───────────────────────────────────────────────────────────────────
#: Пароль в форму не возвращается никогда. Пустое поле означает «оставить
#: прежний», и только явная галочка стирает сохранённый пароль.
KEEP_PASSWORD = ""

TEST_SUBJECT = "Проверка почты RTSP"


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
    from_name: Annotated[str, Form()] = "RTSP",
    timeout_seconds: Annotated[str, Form()] = "15",
    enabled: Annotated[str, Form()] = "",
    clear_password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
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
        return await fail("Укажите адрес SMTP-сервера или снимите галочку «Отправлять письма».")
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
    row.from_name = (from_name.strip() or "RTSP")[:200]
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
    return redirect(_with_notice(next, "mail_saved"))


@router.post("/mail/test")
async def mail_settings_test(
    request: Request,
    db: DbSession,
    admin: RequireAdmin,
    _: CsrfProtected,
    next: Annotated[str, Form()] = "/",
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
                "Это проверочное письмо RTSP.\n\n"
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
    return redirect(_with_notice(next, "mail_test_sent"))


def _with_notice(next_url: str, code: str) -> str:
    target = _safe_next(next_url)
    separator = "&" if "?" in target else "?"
    return f"{target}{separator}notice={code}"


def _positive_int(raw: str, *, low: int, high: int) -> int | None:
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return None
    return value if low <= value <= high else None
