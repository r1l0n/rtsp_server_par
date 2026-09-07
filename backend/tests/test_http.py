"""Смоук-тесты HTTP-слоя: маршрутизация, заголовки безопасности, редиректы."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.sessions import SESSION_COOKIE
from app.config import get_settings
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_healthz_needs_no_dependencies(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_login_page_renders(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert 'name="password"' in response.text
    assert 'action="/login"' in response.text


def test_panel_redirects_anonymous_to_login(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_admin_area_redirects_anonymous(client: TestClient) -> None:
    response = client.get("/admin/users", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


@pytest.mark.parametrize("path", ["/settings/theme", "/settings/mail"])
def test_settings_area_redirects_anonymous(client: TestClient, path: str) -> None:
    """Заодно доказывает, что раздел вообще подключён к приложению."""
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


def test_login_page_carries_the_theme_attribute(client: TestClient) -> None:
    """Тема должна работать и до входа — страница входа тоже оформлена."""
    assert 'data-theme="dark"' in client.get("/login").text
    response = client.get("/login", cookies={"theme": "light"})
    assert 'data-theme="light"' in response.text


def test_unknown_theme_cookie_falls_back_to_dark(client: TestClient) -> None:
    """Значение приходит из браузера — доверять ему нельзя."""
    response = client.get("/login", cookies={"theme": "../../etc/passwd"})
    assert 'data-theme="dark"' in response.text


def test_forgot_page_is_open_to_anonymous(client: TestClient) -> None:
    response = client.get("/forgot")
    assert response.status_code == 200
    assert 'name="email"' in response.text


def test_login_page_offers_password_recovery(client: TestClient) -> None:
    assert 'href="/forgot"' in client.get("/login").text


def test_login_page_offers_remember_me(client: TestClient) -> None:
    body = client.get("/login").text
    assert 'name="remember"' in body
    assert str(get_settings().remember_me_days) in body


def test_invalid_session_cookie_is_not_fatal(client: TestClient) -> None:
    response = client.get("/", cookies={SESSION_COOKIE: "garbage-value"}, follow_redirects=False)
    assert response.status_code == 303


# ─── Заголовки безопасности ──────────────────────────────────────────────────
def test_csp_is_restrictive_and_uses_nonce(client: TestClient) -> None:
    csp = client.get("/login").headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    assert "nonce-" in csp


def test_nonce_differs_between_responses(client: TestClient) -> None:
    first = client.get("/login").headers["Content-Security-Policy"]
    second = client.get("/login").headers["Content-Security-Policy"]
    assert first != second


def test_panel_forbids_framing(client: TestClient) -> None:
    assert client.get("/login").headers["X-Frame-Options"] == "DENY"


def test_view_cookie_is_cross_site_so_embedding_works(monkeypatch) -> None:
    """Панель выдаёт `<iframe src=".../embed/...">` для чужого сайта.

    С SameSite=Lax браузер не отправил бы cookie доступа из стороннего
    iframe, forward_auth ответил бы 403 на каждый сегмент, и встраивание
    не работало бы ни при каких условиях.
    """
    from starlette.responses import Response

    from app.web import templating

    settings = get_settings()
    monkeypatch.setattr(settings, "session_cookie_secure", True, raising=False)
    response = Response()
    templating.set_view_cookie(response, "viewer-1", 3600)
    cookie = response.headers["set-cookie"]
    assert "samesite=none" in cookie.lower()
    assert "Secure" in cookie
    assert "HttpOnly" in cookie


def test_view_cookie_falls_back_to_lax_without_tls(monkeypatch) -> None:
    """SameSite=None без Secure браузер отвергает целиком — в разработке нельзя."""
    from starlette.responses import Response

    from app.web import templating

    settings = get_settings()
    monkeypatch.setattr(settings, "session_cookie_secure", False, raising=False)
    response = Response()
    templating.set_view_cookie(response, "viewer-1", 3600)
    cookie = response.headers["set-cookie"]
    assert "samesite=lax" in cookie.lower()
    assert "Secure" not in cookie


async def test_panel_session_cookie_stays_lax() -> None:
    """Послабление касается только cookie просмотра, не сессии панели."""
    from starlette.responses import Response

    from app.auth import sessions
    from app.web.templating import set_session_cookie

    session = await sessions.create("00000000-0000-0000-0000-000000000001")
    response = Response()
    set_session_cookie(response, session)
    assert "samesite=lax" in response.headers["set-cookie"].lower()


def test_panel_pages_are_not_cached(client: TestClient) -> None:
    assert client.get("/login").headers["Cache-Control"] == "no-store"


def test_request_id_is_returned(client: TestClient) -> None:
    assert client.get("/healthz").headers["X-Request-Id"]


# ─── Статика ─────────────────────────────────────────────────────────────────
def test_static_assets_served_and_cacheable(client: TestClient) -> None:
    response = client.get("/static/app.css")
    assert response.status_code == 200
    assert "max-age" in response.headers["Cache-Control"]


def test_player_script_is_served(client: TestClient) -> None:
    assert client.get("/static/player.js").status_code == 200


# ─── Ошибки ──────────────────────────────────────────────────────────────────
def test_unknown_page_renders_html_error(client: TestClient) -> None:
    response = client.get("/такой-страницы-нет")
    assert response.status_code == 404
    assert "Страница не найдена" in response.text


def test_internal_endpoints_answer_without_html(client: TestClient) -> None:
    """Caddy ждёт от authz голый статус, а не страницу."""
    response = client.get("/internal/authz", headers={"X-Forwarded-Uri": "/"})
    assert response.status_code == 403
    assert response.text == ""


def test_camera_preview_route_precedes_the_catch_all() -> None:
    """POST /cameras/preview обязан объявляться раньше POST /cameras/{camera_id}.

    Иначе «preview» уедет в camera_id, развалится на разборе UUID, и вместо
    предпросмотра оператор получит «Данные формы заполнены неверно» — ошибку,
    по которой причину не найти.
    """
    from app.web.panel_views import router

    paths = [
        route.path
        for route in router.routes
        if "POST" in getattr(route, "methods", set())
        and route.path in ("/cameras/preview", "/cameras/{camera_id}")
    ]
    assert paths.index("/cameras/preview") < paths.index("/cameras/{camera_id}")
