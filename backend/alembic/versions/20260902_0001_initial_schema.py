"""Начальная схема: пользователи, камеры, ссылки, аудит

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    user_role = sa.Enum("admin", "operator", "viewer", name="user_role")
    stream_profile = sa.Enum("passthrough", "transcode", name="stream_profile")
    camera_status = sa.Enum(
        "unknown", "online", "idle", "offline", "error", name="camera_status"
    )

    # ── Пользователи ─────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("totp_secret_enc", sa.LargeBinary(), nullable=True),
        sa.Column("totp_enabled", sa.Boolean(), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", TS, nullable=True),
        sa.Column("last_login_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "recovery_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("used_at", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_recovery_codes_user_id", "recovery_codes", ["user_id"])

    # ── Камеры ───────────────────────────────────────────────────────────────
    op.create_table(
        "cameras",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        # RTSP-URL с логином и паролем, зашифрован SecretBox (app/crypto.py).
        sa.Column("rtsp_url_enc", sa.LargeBinary(), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("mtx_path", sa.String(64), nullable=False),
        sa.Column("profile", stream_profile, nullable=False),
        sa.Column("on_demand", sa.Boolean(), nullable=False),
        sa.Column("audio_enabled", sa.Boolean(), nullable=False),
        sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column("probe", postgresql.JSONB(), nullable=True),
        sa.Column("probed_at", TS, nullable=True),
        sa.Column("status", camera_status, nullable=False),
        sa.Column("status_detail", sa.Text(), nullable=False),
        sa.Column("last_ready_at", TS, nullable=True),
        sa.Column("failure_streak", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("port > 0 AND port <= 65535", name="ck_camera_port"),
    )
    op.create_index("ix_cameras_mtx_path", "cameras", ["mtx_path"], unique=True)
    op.create_index("ix_cameras_node_id", "cameras", ["node_id"])

    # ── Публичные ссылки ─────────────────────────────────────────────────────
    op.create_table(
        "share_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("camera_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        # Только SHA-256 от токена: сам токен не хранится нигде.
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("max_concurrent", sa.Integer(), nullable=False),
        sa.Column("allowed_cidrs", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False),
        sa.Column("last_viewed_at", TS, nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["camera_id"], ["cameras.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_share_links_camera_id", "share_links", ["camera_id"])
    op.create_index("ix_share_links_slug", "share_links", ["slug"], unique=True)
    op.create_index("ix_share_links_token_hash", "share_links", ["token_hash"], unique=True)

    op.create_table(
        "view_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("link_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_key", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=False),
        sa.Column("started_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("ended_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["link_id"], ["share_links.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_view_sessions_link_id", "view_sessions", ["link_id"])
    op.create_index("ix_view_sessions_session_key", "view_sessions", ["session_key"], unique=True)
    op.create_index("ix_view_sessions_active", "view_sessions", ["link_id", "ended_at"])

    # ── Журнал аудита ────────────────────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Без внешнего ключа на users: журнал переживает удаление пользователя,
        # а ON DELETE SET NULL был бы UPDATE и упёрся бы в триггер ниже.
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_label", sa.String(320), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_log_actor_id", "audit_log", ["actor_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    # Журнал только на добавление. Триггер стоит на уровне БД, поэтому запись
    # нельзя «поправить» ни из приложения, ни руками через psql под тем же
    # пользователем — а именно это первым делом сделал бы злоумышленник.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_is_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log является журналом только на добавление';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_is_append_only()")

    op.drop_table("audit_log")
    op.drop_table("view_sessions")
    op.drop_table("share_links")
    op.drop_table("cameras")
    op.drop_table("recovery_codes")
    op.drop_table("users")

    sa.Enum(name="camera_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="stream_profile").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="user_role").drop(op.get_bind(), checkfirst=True)
