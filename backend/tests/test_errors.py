"""Необработанная ошибка обязана оставлять зацепку.

Голое «Internal Server Error» — это строка текста в браузере: пользователь
не знает, что делать, а тот, кто чинит, не знает, где искать. Страница с
кодом запроса связывает увиденное с трассировкой в логе одной командой.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from app.main import _register_error_handlers
from app.middleware import RequestContextMiddleware, SecurityHeadersMiddleware


def _app_that_breaks() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    _register_error_handlers(app)

    @app.get("/boom")
    async def boom() -> Response:
        raise RuntimeError("что-то пошло не так внутри")

    @app.get("/internal/boom")
    async def internal_boom() -> Response:
        raise RuntimeError("то же самое, но на служебном эндпоинте")

    return app


def test_unhandled_error_renders_a_page_with_the_request_id() -> None:
    client = TestClient(_app_that_breaks(), raise_server_exceptions=False)
    response = client.get("/boom")

    assert response.status_code == 500
    assert "text/html" in response.headers["content-type"]
    # Код запроса на странице и в заголовке — один и тот же.
    assert response.headers["X-Request-Id"] in response.text


def test_unhandled_error_never_leaks_the_exception_text() -> None:
    client = TestClient(_app_that_breaks(), raise_server_exceptions=False)
    response = client.get("/boom")

    assert "RuntimeError" not in response.text
    assert "что-то пошло не так внутри" not in response.text


def test_internal_endpoints_get_a_bare_code() -> None:
    """forward_auth разбирает код ответа, а не HTML."""
    client = TestClient(_app_that_breaks(), raise_server_exceptions=False)
    response = client.get("/internal/boom")

    assert response.status_code == 500
    assert response.text == ""
    assert response.headers["X-Request-Id"]
