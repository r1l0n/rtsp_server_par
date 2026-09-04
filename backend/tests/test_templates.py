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
ANONYMOUS_PAGES = frozenset({"login.html", "link_password.html", "invite_accept.html"})


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
    assert "/settings/theme" in html
    assert "/settings/mail" not in html
    assert "/admin/users" not in html
    assert "/admin/audit" not in html


def test_sidebar_shows_every_section_to_admins() -> None:
    from app.web.templating import THEMES

    admin = User(id=uuid.uuid4(), email="admin@example.com", role=Role.admin)
    html = templates.env.get_template("settings_theme.html").render(
        request=types.SimpleNamespace(url=types.SimpleNamespace(path="/settings/theme")),
        user=admin, themes=THEMES, theme="dark", csrf_token="t",
    )
    for link in ("/admin/users", "/admin/audit", "/settings/theme", "/settings/mail", "/profile"):
        assert link in html, link


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
