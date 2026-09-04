"""Конфигурация приложения. Всё берётся из окружения (12-factor)."""

from __future__ import annotations

import base64
import binascii
import functools
import ipaddress
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_KEY_BYTES = 32

type IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class ConfigError(RuntimeError):
    """Конфигурация непригодна для запуска."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Домен и публичный адрес ---------------------------------------------
    domain: str = "localhost"
    public_host: str = "localhost"

    # --- Хранилища -----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://rtspgw:rtspgw@postgres:5432/rtspgw"
    redis_url: str = "redis://redis:6379/0"
    mtx_api_url: str = "http://mediamtx:9997"
    # Те же адреса, но со стороны сети core — ими диагностика проверяет отдачу
    # потока в обход Caddy, чтобы отделить «MediaMTX не отдаёт» от «не пускает
    # forward_auth».
    mtx_hls_url: str = "http://mediamtx:8888"
    mtx_webrtc_url: str = "http://mediamtx:8889"

    # --- Секреты -------------------------------------------------------------
    # В проде ключ приходит файлом (docker secret). APP_SECRET_KEY — только для
    # локальной разработки и тестов.
    app_secret_key_file: Path | None = None
    app_secret_key: str | None = None

    # --- Поведение -----------------------------------------------------------
    log_level: str = "INFO"
    session_cookie_secure: bool = True
    session_ttl_minutes: int = 720
    default_link_ttl_hours: int = 24
    totp_policy: Literal["optional", "admins", "all"] = "admins"

    # Ссылка на просмотр отдаёт короткоживущую cookie, чтобы токен не ходил
    # в каждом запросе за HLS-сегментом.
    view_cookie_ttl_minutes: int = 60

    # --- Приглашения сотрудников ---------------------------------------------
    #: Сколько живёт ссылка из письма-приглашения.
    invite_ttl_hours: int = 72

    # --- Почта (SMTP) --------------------------------------------------------
    # Пустой SMTP_HOST = почта не настроена. Это не ошибка: приглашение всё
    # равно создаётся, а ссылку панель показывает администратору, чтобы он
    # передал её сотруднику сам.
    smtp_host: str = ""
    smtp_port: int = 587
    #: starttls — обычный submission-порт 587; ssl — implicit TLS на 465;
    #: none — без шифрования (только для локального релея в той же сети).
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str = ""
    #: Пароль приходит либо файлом (docker secret), либо переменной окружения.
    smtp_password_file: Path | None = None
    smtp_password: str | None = None
    #: Отправитель письма. Пусто — noreply@<DOMAIN>.
    mail_from: str = ""
    mail_from_name: str = "RTSP"
    smtp_timeout_seconds: int = 15

    # --- Безопасность добавления камер (SSRF) --------------------------------
    allow_private_camera_hosts: bool = False
    camera_host_allowlist: str = ""

    # --- Тайминги ------------------------------------------------------------
    reconcile_interval_seconds: int = 15
    authz_cache_seconds: int = 20
    stream_unhealthy_after_seconds: int = 60

    snapshot_dir: Path = Path("/var/lib/rtspgw/snapshots")

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    # -------------------------------------------------------------------------
    @functools.cached_property
    def secret_key(self) -> bytes:
        """32-байтный ключ для шифрования кредов камер.

        Ключ читается один раз при старте: если он битый, процесс должен
        падать сразу, а не в момент добавления первой камеры.
        """
        raw: str | None = None
        if self.app_secret_key_file is not None:
            try:
                raw = self.app_secret_key_file.read_text(encoding="utf-8")
            except PermissionError as exc:
                # Типовая причина: файл на хосте принадлежит root с правами 600,
                # а процесс в контейнере работает под непривилегированным uid.
                # Docker монтирует секрет как есть, права хоста сохраняются.
                raise ConfigError(
                    f"нет прав на чтение APP_SECRET_KEY_FILE={self.app_secret_key_file}. "
                    f"На хосте выполните: chown 10001:10001 secrets/app_key && "
                    f"chmod 400 secrets/app_key"
                ) from exc
            except OSError as exc:
                raise ConfigError(
                    f"не удалось прочитать APP_SECRET_KEY_FILE={self.app_secret_key_file}: {exc}"
                ) from exc
        elif self.app_secret_key:
            raw = self.app_secret_key

        if not raw or not raw.strip():
            raise ConfigError(
                "не задан ключ шифрования: укажите APP_SECRET_KEY_FILE или APP_SECRET_KEY "
                "(сгенерировать: python -m app.cli gen-key)"
            )

        try:
            key = base64.b64decode(raw.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ConfigError("ключ шифрования должен быть base64") from exc

        if len(key) != SECRET_KEY_BYTES:
            raise ConfigError(
                f"ключ шифрования должен быть ровно {SECRET_KEY_BYTES} байта после "
                f"base64-декодирования, получено {len(key)}"
            )
        return key

    @functools.cached_property
    def camera_allowlist_networks(self) -> tuple[IPNetwork, ...]:
        """Приватные подсети, которым в виде исключения разрешено быть камерой."""
        nets: list[IPNetwork] = []
        for item in self.camera_host_allowlist.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                nets.append(ipaddress.ip_network(item, strict=False))
            except ValueError as exc:
                raise ConfigError(f"CAMERA_HOST_ALLOWLIST: '{item}' не является IP/CIDR") from exc
        return tuple(nets)

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}"

    # --- Почта ---------------------------------------------------------------
    @property
    def mail_enabled(self) -> bool:
        return bool(self.smtp_host.strip())

    @property
    def mail_sender(self) -> str:
        """Адрес в поле From. По умолчанию — noreply на своём домене."""
        return self.mail_from.strip() or f"noreply@{self.domain}"

    @functools.cached_property
    def smtp_secret(self) -> str:
        """Пароль SMTP. Файл имеет приоритет над переменной окружения."""
        if self.smtp_password_file is not None:
            try:
                return self.smtp_password_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(
                    f"не удалось прочитать SMTP_PASSWORD_FILE={self.smtp_password_file}: {exc}"
                ) from exc
        return (self.smtp_password or "").strip()


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
