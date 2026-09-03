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
#: Вход защищён от «login CSRF» атрибутом SameSite=Lax на cookie сессии.
ANONYMOUS_PAGES = frozenset({"login.html", "link_password.html"})


@pytest.mark.parametrize("name", [n for n in TEMPLATE_FILES if n not in ANONYMOUS_PAGES])
def test_forms_carry_csrf_token(name: str) -> None:
    """Каждая POST-форма на странице с сессией должна нести CSRF-токен."""
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    forms = re.findall(r"<form[^>]*method=\"post\"[^>]*>(.*?)</form>", text, re.DOTALL)
    missing = [form for form in forms if "csrf_token" not in form]
    assert not missing, f"{name}: форм без CSRF-токена — {len(missing)}"
