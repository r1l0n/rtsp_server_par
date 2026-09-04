"""Схема БД.

PostgreSQL — источник истины по камерам и ссылкам. MediaMTX держит только
производное состояние, которое реконсилятор восстанавливает из этих таблиц.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


#: Все временные метки — с часовым поясом. Наивных дат в схеме нет.
TS = DateTime(timezone=True)


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Role(enum.StrEnum):
    admin = "admin"
    operator = "operator"
    viewer = "viewer"


class StreamProfile(enum.StrEnum):
    #: Отдаём поток камеры как есть — 0% CPU.
    passthrough = "passthrough"
    #: Перекодируем в H.264/Opus — примерно 1 ядро на 1080p@15fps.
    transcode = "transcode"


class CameraStatus(enum.StrEnum):
    unknown = "unknown"
    #: Поток идёт, есть зрители.
    online = "online"
    #: Камера настроена как on-demand и сейчас никто не смотрит — это норма,
    #: а не авария: соединение с камерой намеренно закрыто.
    idle = "idle"
    offline = "offline"
    error = "error"


# ─────────────────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[Role] = mapped_column(Enum(Role, name="user_role"), default=Role.operator)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # TOTP-секрет шифруется тем же ключом, что и креды камер.
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(TS, default=None)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(TS, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now()
    )

    recovery_codes: Mapped[list[RecoveryCode]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Invitation(Base):
    """Приглашение сотрудника: письмо со ссылкой, по которой он задаёт пароль.

    Учётная запись создаётся только в момент принятия приглашения — до тех пор
    в `users` нет строки с пустым или заведомо известным паролем, а значит
    и войти под приглашённым адресом нельзя.
    """

    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), index=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[Role] = mapped_column(Enum(Role, name="user_role"), default=Role.operator)

    #: SHA-256 от токена из письма. Сам токен не хранится — как и у ссылок.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    expires_at: Mapped[dt.datetime] = mapped_column(TS)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(TS, default=None)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TS, default=None)
    #: Когда письмо реально ушло. NULL — почта не настроена или SMTP отказал,
    #: ссылку администратор передал сам.
    sent_at: Mapped[dt.datetime | None] = mapped_column(TS, default=None)

    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())

    @property
    def is_pending(self) -> bool:
        return self.accepted_at is None and self.revoked_at is None

    def is_expired(self, now: dt.datetime | None = None) -> bool:
        return self.expires_at <= (now or dt.datetime.now(dt.UTC))


class RecoveryCode(Base):
    """Одноразовый код восстановления на случай потери TOTP-устройства."""

    __tablename__ = "recovery_codes"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(Text)
    used_at: Mapped[dt.datetime | None] = mapped_column(TS, default=None)
    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())

    user: Mapped[User] = relationship(back_populates="recovery_codes")


# ─────────────────────────────────────────────────────────────────────────────
class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")

    #: Полный RTSP-URL с логином и паролем, зашифрован SecretBox.
    #: Наружу через API не отдаётся никогда.
    rtsp_url_enc: Mapped[bytes] = mapped_column(LargeBinary)
    #: Хост и порт продублированы открыто — для диагностики и SSRF-проверок.
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=554)

    #: Имя пути в MediaMTX. Случайное и неугадываемое: даже при ошибке в
    #: forward_auth перебрать пути не получится.
    mtx_path: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    profile: Mapped[StreamProfile] = mapped_column(
        Enum(StreamProfile, name="stream_profile"), default=StreamProfile.passthrough
    )
    #: on_demand=True — тянем поток с камеры только пока есть зритель.
    on_demand: Mapped[bool] = mapped_column(Boolean, default=True)
    audio_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    #: Задел на горизонтальное масштабирование: какой узел обслуживает камеру.
    node_id: Mapped[str] = mapped_column(String(64), default="default", index=True)

    #: Результат ffprobe: кодеки, разрешение, fps, признак совместимости.
    probe: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    probed_at: Mapped[dt.datetime | None] = mapped_column(TS, default=None)

    status: Mapped[CameraStatus] = mapped_column(
        Enum(CameraStatus, name="camera_status"), default=CameraStatus.unknown
    )
    status_detail: Mapped[str] = mapped_column(Text, default="")
    last_ready_at: Mapped[dt.datetime | None] = mapped_column(TS, default=None)
    #: Сколько подряд циклов watchdog видел путь неготовым — для backoff.
    failure_streak: Mapped[int] = mapped_column(Integer, default=0)

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now()
    )

    links: Mapped[list[ShareLink]] = relationship(
        back_populates="camera", cascade="all, delete-orphan"
    )

    __table_args__ = (CheckConstraint("port > 0 AND port <= 65535", name="ck_camera_port"),)


# ─────────────────────────────────────────────────────────────────────────────
class ShareLink(Base):
    __tablename__ = "share_links"

    id: Mapped[uuid.UUID] = _pk()
    camera_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(200), default="")

    #: Публичная часть URL: /v/<slug>
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: SHA-256 от токена. Сам токен не хранится нигде.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    #: NULL = бессрочная ссылка.
    expires_at: Mapped[dt.datetime | None] = mapped_column(TS, default=None)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(TS, default=None)

    #: 0 = без ограничения одновременных зрителей.
    max_concurrent: Mapped[int] = mapped_column(Integer, default=0)
    #: Пустой список = доступ с любого IP.
    allowed_cidrs: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    #: Необязательный пароль на саму ссылку (argon2).
    password_hash: Mapped[str | None] = mapped_column(Text, default=None)

    view_count: Mapped[int] = mapped_column(Integer, default=0)
    last_viewed_at: Mapped[dt.datetime | None] = mapped_column(TS, default=None)

    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())

    camera: Mapped[Camera] = relationship(back_populates="links")


class ViewSession(Base):
    """Один сеанс просмотра по публичной ссылке — для аудита и лимитов."""

    __tablename__ = "view_sessions"

    id: Mapped[uuid.UUID] = _pk()
    link_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("share_links.id", ondelete="CASCADE"), index=True
    )
    session_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ip: Mapped[str] = mapped_column(String(45), default="")
    user_agent: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())
    last_seen_at: Mapped[dt.datetime] = mapped_column(TS, server_default=func.now())
    ended_at: Mapped[dt.datetime | None] = mapped_column(TS, default=None)

    __table_args__ = (Index("ix_view_sessions_active", "link_id", "ended_at"),)


# ─────────────────────────────────────────────────────────────────────────────
class AuditLog(Base):
    """Только INSERT — на уровне БД это закреплено триггером (см. миграцию 0001)."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _pk()
    #: Намеренно без внешнего ключа: журнал должен переживать удаление
    #: пользователя, а ON DELETE SET NULL сделал бы UPDATE и упёрся бы
    #: в триггер append-only.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), default=None, index=True
    )
    #: Логин, введённый при неудачной попытке входа (актора ещё нет).
    actor_label: Mapped[str] = mapped_column(String(320), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[str] = mapped_column(String(64), default="")
    ip: Mapped[str] = mapped_column(String(45), default="")
    user_agent: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        TS, server_default=func.now(), index=True
    )
