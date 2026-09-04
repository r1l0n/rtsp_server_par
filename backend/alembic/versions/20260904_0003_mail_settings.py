"""Настройки SMTP, задаваемые из панели

Revision ID: 0003_mail_settings
Revises: 0002_invitations
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_mail_settings"
down_revision: str | None = "0002_invitations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    mail_security = sa.Enum("starttls", "ssl", "none", name="mail_security")

    op.create_table(
        "mail_settings",
        # autoincrement=False: колонка должна быть INTEGER, а не SERIAL —
        # строка тут ровно одна и последовательность ей не нужна.
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("security", mail_security, nullable=False),
        sa.Column("username", sa.String(320), nullable=False),
        # Пароль SMTP шифруется SecretBox (app/crypto.py), как и креды камер.
        sa.Column("password_enc", sa.LargeBinary(), nullable=True),
        sa.Column("mail_from", sa.String(320), nullable=False),
        sa.Column("from_name", sa.String(200), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("last_success_at", TS, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], ondelete="SET NULL"),
        # Вторая строка настроек означала бы «какая из них действует?» —
        # запрещаем её на уровне БД, а не договорённостью в коде.
        sa.CheckConstraint("id = 1", name="ck_mail_settings_singleton"),
        sa.CheckConstraint("port > 0 AND port <= 65535", name="ck_mail_settings_port"),
    )


def downgrade() -> None:
    op.drop_table("mail_settings")
    sa.Enum(name="mail_security").drop(op.get_bind(), checkfirst=True)
