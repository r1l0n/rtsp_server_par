"""Все шаблоны должны компилироваться и не содержать инлайновых обработчиков.

Ошибку в Jinja видно только в момент рендера конкретной страницы — а страницы
вроде «коды восстановления» открываются раз в год. Компилируем все разом.
"""

from __future__ import annotations

import re

import pytest

from app.web.templating import TEMPLATE_DIR, templates

TEMPLATE_FILES = sorted(path.name for path in TEMPLATE_DIR.glob("*.html"))

#: CSP запрещает инлайновый JavaScript. Атрибуты onclick/onfocus и т.п. не
#: спасает даже nonce — они просто не выполнятся, и кнопка молча умрёт.
INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)


def test_templates_exist() -> None:
    assert len(TEMPLATE_FILES) >= 12


@pytest.mark.parametrize("name", TEMPLATE_FILES)
def test_template_compiles(name: str) -> None:
    templates.env.get_template(name)


@pytest.mark.parametrize("name", TEMPLATE_FILES)
def test_no_inline_event_handlers(name: str) -> None:
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    found = INLINE_HANDLER.findall(text)
    assert not found, f"инлайновые обработчики запрещены CSP: {found}"


#: Страницы, которые открываются без сессии: CSRF-токену там взяться неоткуда.
#: Вход защищён от «login CSRF» атрибутом SameSite=Lax на cookie сессии,
#: а приглашение — тем, что его адрес сам по себе одноразовый секрет.
ANONYMOUS_PAGES = frozenset(
    {"login.html", "link_password.html", "invite_accept.html",
     "forgot.html", "reset_password.html"}
)


@pytest.mark.parametrize("name", [n for n in TEMPLATE_FILES if n not in ANONYMOUS_PAGES])
def test_forms_carry_csrf_token(name: str) -> None:
    """Каждая POST-форма на странице с сессией должна нести CSRF-токен."""
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    forms = re.findall(r"<form[^>]*method=\"post\"[^>]*>(.*?)</form>", text, re.DOTALL)
    missing = [form for form in forms if "csrf_token" not in form]
    assert not missing, f"{name}: форм без CSRF-токена — {len(missing)}"


# ─── Рендер страниц приглашения ──────────────────────────────────────────────
# Компиляция ловит только синтаксис. Обращение к несуществующему атрибуту
# (`inv.role.value` вместо `inv.role`) проявляется лишь при настоящем рендере,
# а эти две страницы открываются реже всех остальных.
import datetime as dt  # noqa: E402
import types  # noqa: E402
import uuid  # noqa: E402

from app.crypto import get_cipher  # noqa: E402
from app.models import (  # noqa: E402
    Invitation,
    MailSecurity,
    MailSettings,
    Role,
    User,
)


def _request_stub() -> types.SimpleNamespace:
    return types.SimpleNamespace(url=types.SimpleNamespace(path="/admin/users"))


def _invitation() -> Invitation:
    return Invitation(
        id=uuid.uuid4(),
        email="new@example.com",
        full_name="Анна",
        role=Role.viewer,
        token_hash="0" * 64,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=72),
    )


def test_users_page_renders_pending_invitations() -> None:
    admin = User(id=uuid.uuid4(), email="admin@example.com", role=Role.admin)
    html = templates.env.get_template("users.html").render(
        request=_request_stub(),
        user=admin,
        users=[admin],
        invitations=[_invitation()],
        mail_enabled=True,
        invite_ttl_hours=72,
        now=dt.datetime.now(dt.UTC),
        csrf_token="t",
    )
    assert "new@example.com" in html
    assert "/admin/users/invite" in html
    assert "Выслать заново" in html


def test_users_page_offers_the_link_when_mail_failed() -> None:
    """Отказ SMTP не должен оставлять администратора без ссылки."""
    admin = User(id=uuid.uuid4(), email="admin@example.com", role=Role.admin)
    html = templates.env.get_template("users.html").render(
        request=_request_stub(),
        user=admin,
        users=[admin],
        invitations=[],
        mail_enabled=False,
        invite_ttl_hours=72,
        now=dt.datetime.now(dt.UTC),
        csrf_token="t",
        invite_link="https://cam.test/invite/TOKEN",
        invite_link_email="new@example.com",
    )
    assert "https://cam.test/invite/TOKEN" in html
    assert "copybutton" in html


def test_mail_settings_page_never_returns_the_password() -> None:
    """Сохранённый пароль SMTP не должен уезжать обратно в браузер."""
    from app import mail

    admin = User(id=uuid.uuid4(), email="admin@example.com", role=Role.admin)
    row = MailSettings(
        id=1, enabled=True, host="smtp.panel.example", port=465,
        security=MailSecurity.ssl, username="bot",
        password_enc=get_cipher().encrypt("секрет-smtp"),
        mail_from="noreply@panel.example", from_name="Панель",
        timeout_seconds=20, last_error="",
    )
    html = templates.env.get_template("mail_settings.html").render(
        request=_request_stub(),
        user=admin,
        row=row,
        config=mail.config_from_row(row),
        form=None,
        env_host="",
        csrf_token="t",
    )
    assert "секрет-smtp" not in html
    assert "smtp.panel.example" in html
    assert 'action="/settings/mail/test"' in html


def test_mail_settings_page_works_without_a_saved_row() -> None:
    """Пока настроек нет, форма показывает то, что задано окружением."""
    from app import mail
    from app.config import Settings

    admin = User(id=uuid.uuid4(), email="admin@example.com", role=Role.admin)
    html = templates.env.get_template("mail_settings.html").render(
        request=_request_stub(),
        user=admin,
        row=None,
        config=mail.config_from_env(Settings(smtp_host="smtp.env.example")),
        form=None,
        env_host="smtp.env.example",
        csrf_token="t",
    )
    assert "smtp.env.example" in html
    assert ".env" in html


def test_sidebar_hides_admin_sections_from_operators() -> None:
    """Меню не должно показывать то, куда всё равно не пустят."""
    from app.web.templating import THEMES

    operator = User(id=uuid.uuid4(), email="op@example.com", role=Role.operator)
    html = templates.env.get_template("settings_theme.html").render(
        request=types.SimpleNamespace(url=types.SimpleNamespace(path="/settings/theme")),
        user=operator, themes=THEMES, theme="dark", csrf_token="t",
    )
    assert "/admin/users" not in html
    assert "/admin/audit" not in html


def test_sidebar_shows_every_section_to_admins() -> None:
    from app.web.templating import THEMES

    admin = User(id=uuid.uuid4(), email="admin@example.com", role=Role.admin)
    html = templates.env.get_template("settings_theme.html").render(
        request=types.SimpleNamespace(url=types.SimpleNamespace(path="/settings/theme")),
        user=admin, themes=THEMES, theme="dark", csrf_token="t",
    )
    for link in ("/admin/users", "/admin/audit", "/profile"):
        assert link in html, link


def test_settings_open_as_a_dialog_not_a_menu_item() -> None:
    """Настройки вызываются шестерёнкой рядом с профилем, а не пунктом меню."""
    from app.web.templating import THEMES

    admin = User(id=uuid.uuid4(), email="admin@example.com", role=Role.admin)
    html = templates.env.get_template("settings_theme.html").render(
        request=types.SimpleNamespace(url=types.SimpleNamespace(path="/")),
        user=admin, themes=THEMES, theme="dark", csrf_token="t",
    )
    assert 'data-dialog-open="settings"' in html
    assert "data-dialog-src" in html
    assert "#i-gear" in html
    # Группы «Настройки» в списке разделов быть не должно.
    assert "nav-group" not in html


def test_settings_dialog_shows_mail_only_to_admins() -> None:
    from app import mail
    from app.config import Settings
    from app.web.templating import THEMES

    def render_for(role: Role) -> str:
        who = User(id=uuid.uuid4(), email="u@example.com", role=role)
        return templates.env.get_template("_settings_dialog.html").render(
            request=_request_stub(), user=who, themes=THEMES, theme="dark",
            row=None, config=mail.config_from_env(Settings()), next="/", csrf_token="t",
        )

    assert 'action="/settings/mail"' in render_for(Role.admin)
    assert 'action="/settings/mail"' not in render_for(Role.operator)


def test_settings_dialog_returns_to_the_page_it_was_opened_from() -> None:
    """Окно поверх страницы: после «Применить» пользователь остаётся на месте."""
    from app import mail
    from app.config import Settings
    from app.web.templating import THEMES

    admin = User(id=uuid.uuid4(), email="a@example.com", role=Role.admin)
    html = templates.env.get_template("_settings_dialog.html").render(
        request=_request_stub(), user=admin, themes=THEMES, theme="dark",
        row=None, config=mail.config_from_env(Settings()),
        next="/cameras/42", csrf_token="t",
    )
    assert html.count('name="next" value="/cameras/42"') >= 2


def test_theme_reaches_the_html_tag() -> None:
    """Атрибут data-theme — единственное, чем страница выбирает палитру."""
    from app.web.templating import THEMES

    admin = User(id=uuid.uuid4(), email="admin@example.com", role=Role.admin)
    html = templates.env.get_template("settings_theme.html").render(
        request=types.SimpleNamespace(url=types.SimpleNamespace(path="/settings/theme")),
        user=admin, themes=THEMES, theme="light", csrf_token="t",
    )
    assert '<html lang="ru" data-theme="light">' in html


def test_invite_page_posts_back_to_its_own_token() -> None:
    html = templates.env.get_template("invite_accept.html").render(
        request=_request_stub(),
        user=None,
        token="TOKEN",
        email="new@example.com",
        full_name="Анна",
        role="operator",
        expires_at=dt.datetime.now(dt.UTC),
    )
    assert 'action="/invite/TOKEN"' in html
    assert 'name="confirm_password"' in html
    # Адрес задан приглашением — поле только для показа.
    assert "readonly" in html


# ─── Время и названия действий ───────────────────────────────────────────────
def test_time_is_shown_in_the_configured_zone_not_utc() -> None:
    """В базе всё в UTC, человеку показываем местное.

    Ошибка здесь тихая: журнал выглядит правдоподобно, просто события в нём
    на несколько часов «не тогда», и это замечают, сверяя с чужими часами.
    """
    from app.web.templating import format_datetime

    moment = dt.datetime(2026, 9, 4, 9, 40, 39, tzinfo=dt.UTC)
    assert format_datetime(moment, "%d.%m.%Y %H:%M") == "04.09.2026 14:40"


def test_naive_datetime_is_treated_as_utc() -> None:
    from app.web.templating import format_datetime

    assert format_datetime(dt.datetime(2026, 9, 4, 9, 0)) == "04.09.2026 14:00"


def test_timezone_label_shows_the_offset() -> None:
    from app.web.templating import timezone_label

    assert timezone_label() == "UTC+5"


def test_every_audit_action_has_a_russian_name() -> None:
    """Журнал читают люди. Событие без перевода покажется как код."""
    from app import audit
    from app.web.templating import AUDIT_ACTION_LABELS

    codes = {
        value for name, value in vars(audit).items()
        if name.isupper() and isinstance(value, str) and "." in value
    }
    assert not codes - set(AUDIT_ACTION_LABELS), "нет русского названия"
