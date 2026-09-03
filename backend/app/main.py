"""Точка входа control-plane."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth.deps import AuthRequired, CsrfError, Forbidden, TwoFactorRequired
from .config import get_settings
from .db import dispose_engine, get_sessionmaker
from .internal import authz
from .logging_setup import configure_logging, get_logger
from .media.mtx_client import close_mtx, get_mtx
from .middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from .redis_client import close_redis, get_redis
from .web import admin_views, auth_views, panel_views, profile_views, public_views
from .web.templating import STATIC_DIR, redirect, render

log = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # Ключ шифрования читаем на старте: битый ключ должен ронять процесс
    # сразу, а не в момент добавления первой камеры.
    _ = settings.secret_key

    log.info("startup", domain=settings.domain, totp_policy=settings.totp_policy)
    try:
        yield
    finally:
        await close_mtx()
        await close_redis()
        await dispose_engine()
        log.info("shutdown")


health = APIRouter(tags=["health"])


@health.get("/healthz", include_in_schema=False)
async def healthz() -> PlainTextResponse:
    """Liveness: процесс жив и отвечает. Без обращения к зависимостям."""
    return PlainTextResponse("ok")


@health.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    """Readiness: доступны ли Postgres, Redis и MediaMTX."""
    checks: dict[str, str] = {}

    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"

    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"

    try:
        await get_mtx().list_active_paths()
        checks["mediamtx"] = "ok"
    except Exception as exc:
        checks["mediamtx"] = f"error: {type(exc).__name__}"

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse({"ready": healthy, "checks": checks}, status_code=200 if healthy else 503)


@health.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Метрики для Prometheus.

    Наружу не публикуются: Caddy отдаёт на /metrics 404, а Prometheus ходит
    напрямую в docker-сеть core. Значения считаются на момент запроса —
    при интервале сбора 15 с это дешевле, чем держать счётчики в памяти
    и синхронизировать их между api и worker.
    """
    from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest

    registry = CollectorRegistry()
    cameras = Gauge("rtspgw_cameras", "Камеры по статусам", ["status"], registry=registry)
    links = Gauge("rtspgw_active_links", "Действующие публичные ссылки", registry=registry)
    viewers = Gauge("rtspgw_active_viewers", "Открытые сеансы просмотра", registry=registry)

    async with get_sessionmaker()() as session:
        rows = await session.execute(
            text("SELECT status::text, count(*) FROM cameras WHERE is_enabled GROUP BY status")
        )
        for status, count in rows:
            cameras.labels(status=status).set(count)

        links.set(
            await session.scalar(
                text(
                    "SELECT count(*) FROM share_links "
                    "WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())"
                )
            )
            or 0
        )
        viewers.set(
            await session.scalar(text("SELECT count(*) FROM view_sessions WHERE ended_at IS NULL"))
            or 0
        )

    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="RTSP Gateway",
        version="0.1.0",
        lifespan=lifespan,
        # Схема API наружу не публикуется: сервис не для внешних интеграций.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Порядок важен: Starlette оборачивает middleware в обратном порядке
    # добавления, поэтому RequestContext (добавлен последним) выполняется
    # первым и успевает создать nonce до SecurityHeaders.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health)
    app.include_router(authz.router)
    app.include_router(auth_views.router)
    app.include_router(public_views.router)
    app.include_router(profile_views.router)
    app.include_router(admin_views.router)
    app.include_router(panel_views.router)

    _register_error_handlers(app)

    log.info("app_created", cookie_secure=settings.session_cookie_secure)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    """Ошибки авторизации в HTML-приложении — это переходы, а не JSON с кодом."""

    @app.exception_handler(AuthRequired)
    async def _auth_required(request: Request, exc: AuthRequired) -> Response:
        return redirect(f"/login?next={exc.next_url}")

    @app.exception_handler(TwoFactorRequired)
    async def _two_factor(request: Request, exc: TwoFactorRequired) -> Response:
        return redirect("/login/2fa")

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden) -> Response:
        return render(request, "error.html", status_code=403, message=exc.detail)

    @app.exception_handler(CsrfError)
    async def _csrf(request: Request, exc: CsrfError) -> Response:
        return render(
            request,
            "error.html",
            status_code=400,
            message=(
                "Форма устарела или была отправлена не с этой страницы. "
                "Обновите страницу и повторите действие."
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if request.url.path.startswith(("/internal/", "/static/")):
            return Response(status_code=exc.status_code)
        return render(
            request,
            "error.html",
            status_code=exc.status_code,
            message=str(exc.detail) if exc.status_code != 404 else "Страница не найдена.",
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        """Последний рубеж: страница с кодом запроса вместо «Internal Server Error».

        Трассировка уже записана middleware'ом под тем же request_id, поэтому
        по коду со страницы причина находится одной командой. Без этого
        пятисотка выглядит как строка текста в браузере и не оставляет
        никакой зацепки — ни пользователю, ни тому, кто будет чинить.
        """
        request_id = getattr(request.state, "request_id", "")

        if request.url.path.startswith(("/internal/", "/static/")):
            # Тут HTML некому читать: forward_auth разбирает код, а не страницу.
            return Response(status_code=500, headers={"X-Request-Id": request_id})

        response = render(
            request,
            "error.html",
            status_code=500,
            message=(
                "Внутренняя ошибка сервиса. Действие не выполнено — "
                "повторите попытку или сообщите код запроса администратору."
            ),
        )
        # Заголовок обычно проставляет middleware, но при необработанной
        # ошибке он до этого не доходит: ответ рождается уже здесь.
        response.headers["X-Request-Id"] = request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        # Тело ошибки FastAPI показывает имена и значения полей — наружу это
        # не отдаём, чтобы не раскрывать внутреннее устройство форм.
        return render(
            request, "error.html", status_code=400,
            message="Данные формы заполнены неверно.",
        )


app = create_app()
