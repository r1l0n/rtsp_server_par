"""Сквозные middleware: контекст запроса и заголовки безопасности."""

from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .logging_setup import get_logger

log = get_logger("http")

Handler = Callable[[Request], Awaitable[Response]]

#: Пути, которые можно встраивать в чужие страницы (iframe).
EMBEDDABLE_PREFIXES = ("/embed/",)

#: Шум в логах: healthcheck'и ходят каждые 15 секунд.
QUIET_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


def client_ip(request: Request) -> str:
    """Реальный IP клиента.

    Caddy — единственный прокси перед приложением, и он выставляет X-Real-IP
    из своего {client_ip}. Заголовку доверяем только потому, что порт 8000
    не опубликован наружу и достучаться до него, минуя Caddy, нельзя.
    """
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Request-id, IP клиента, CSP-nonce и access-лог."""

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        ip = client_ip(request)

        request.state.request_id = request_id
        request.state.client_ip = ip
        request.state.csp_nonce = secrets.token_urlsafe(16)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, ip=ip)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        if request.url.path not in QUIET_PATHS:
            log.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
        response.headers["X-Request-Id"] = request_id
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """CSP с per-response nonce и остальные защитные заголовки.

    Транспортные заголовки (HSTS, nosniff, Referrer-Policy) ставит Caddy;
    здесь то, что зависит от конкретного ответа.
    """

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        response = await call_next(request)

        nonce = getattr(request.state, "csp_nonce", "")
        embeddable = request.url.path.startswith(EMBEDDABLE_PREFIXES)

        # frame-ancestors: панель не встраивается никуда, embed-страница — куда угодно.
        frame_ancestors = "*" if embeddable else "'none'"

        csp = "; ".join(
            (
                "default-src 'none'",
                "base-uri 'none'",
                "form-action 'self'",
                f"frame-ancestors {frame_ancestors}",
                f"script-src 'self' 'nonce-{nonce}'",
                f"style-src 'self' 'nonce-{nonce}'",
                "img-src 'self' data:",
                "font-src 'self'",
                "connect-src 'self'",
                # hls.js создаёт MediaSource и воркер через blob:
                "media-src 'self' blob:",
                "worker-src 'self' blob:",
                "object-src 'none'",
            )
        )
        response.headers["Content-Security-Policy"] = csp
        if not embeddable:
            response.headers["X-Frame-Options"] = "DENY"

        if request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        else:
            # Страницы панели и плеера не должны оседать в промежуточных кэшах.
            response.headers.setdefault("Cache-Control", "no-store")
        return response
