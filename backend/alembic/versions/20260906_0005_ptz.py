"""Управление обзором камеры (PTZ)

Revision ID: 0005_ptz
Revises: 0004_password_resets
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_ptz"
down_revision: str | None = "0004_password_resets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # server_default обязателен: таблицы уже не пустые, а все прочие default в
    # этой схеме питоновские и на существующие строки не действуют.
    op.add_column(
        "cameras",
        sa.Column("ptz_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "cameras",
        sa.Column("ptz_driver", sa.String(16), nullable=False, server_default="auto"),
    )
    op.add_column("cameras", sa.Column("ptz_port", sa.Integer(), nullable=True))
    op.add_column(
        "cameras",
        sa.Column("ptz_tls", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "cameras",
        sa.Column("ptz_channel", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    # Отдельные учётные данные для управления, шифруются тем же ключом, что и
    # RTSP-URL. NULL — брать логин и пароль из самой RTSP-ссылки.
    op.add_column("cameras", sa.Column("ptz_credentials_enc", sa.LargeBinary(), nullable=True))
    op.add_column("cameras", sa.Column("ptz_meta", postgresql.JSONB(), nullable=True))
    op.add_column("cameras", sa.Column("ptz_checked_at", TS, nullable=True))

    op.add_column(
        "share_links",
        sa.Column("ptz_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("share_links", "ptz_allowed")
    for column in (
        "ptz_checked_at",
        "ptz_meta",
        "ptz_credentials_enc",
        "ptz_channel",
        "ptz_tls",
        "ptz_port",
        "ptz_driver",
        "ptz_enabled",
    ):
        op.drop_column("cameras", column)
