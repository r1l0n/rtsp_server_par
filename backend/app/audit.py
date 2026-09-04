"""Журнал аудита.

Таблица только на добавление: события входа, изменения камер и ссылок,
факты просмотра по публичной ссылке. Это то, что спросят первым делом, если
ссылка утечёт наружу.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog

# --- Действия (фиксированный словарь, чтобы по нему можно было фильтровать) ---
LOGIN_OK = "login.ok"
LOGIN_FAILED = "login.failed"
LOGIN_LOCKED = "login.locked"
LOGOUT = "logout"
TOTP_ENABLED = "totp.enabled"
TOTP_DISABLED = "totp.disabled"
TOTP_FAILED = "totp.failed"
TOTP_RECOVERY_USED = "totp.recovery_used"
PASSWORD_CHANGED = "password.changed"  # noqa: S105 — это имя события, а не пароль
SESSION_REVOKED = "session.revoked"

USER_CREATED = "user.created"
USER_UPDATED = "user.updated"
USER_DISABLED = "user.disabled"

INVITE_CREATED = "invite.created"
INVITE_SENT = "invite.sent"
INVITE_SEND_FAILED = "invite.send_failed"
INVITE_REVOKED = "invite.revoked"
INVITE_ACCEPTED = "invite.accepted"

CAMERA_CREATED = "camera.created"
CAMERA_UPDATED = "camera.updated"
CAMERA_DELETED = "camera.deleted"

LINK_CREATED = "link.created"
LINK_REVOKED = "link.revoked"
LINK_ROTATED = "link.rotated"
LINK_DELETED = "link.deleted"
LINK_VIEWED = "link.viewed"
LINK_DENIED = "link.denied"


async def record(
    session: AsyncSession,
    action: str,
    *,
    actor_id: uuid.UUID | None = None,
    actor_label: str = "",
    target_type: str = "",
    target_id: str = "",
    ip: str = "",
    user_agent: str = "",
    meta: dict[str, Any] | None = None,
) -> None:
    """Добавляет запись. Коммит — на стороне вызывающего кода."""
    session.add(
        AuditLog(
            actor_id=actor_id,
            actor_label=actor_label[:320],
            action=action,
            target_type=target_type,
            target_id=str(target_id)[:64],
            ip=ip[:45],
            user_agent=user_agent[:400],
            meta=meta or {},
        )
    )
