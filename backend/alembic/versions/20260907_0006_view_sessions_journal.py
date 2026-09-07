"""view_sessions — журнал открытий, а не живой счётчик

Поле last_seen_at не обновляла ни одна строчка кода: оно заполнялось
server_default при вставке и дальше оставалось неизменным. На нём держались
сразу три вещи, и все три работали неправильно — сеанс закрывался ровно через
пять минут независимо от того, смотрит зритель или нет; метрика
rtspgw_active_viewers показывала не зрителей, а число открытий страницы;
таблица при этом не подрезалась ничем и росла вечно.

Сколько человек смотрит прямо сейчас, знает Redis (см. internal/authz), оттуда
метрика теперь и берётся. Колонка убирается, а по started_at добавляется
индекс — по нему worker и закрывает старые записи, и удаляет их по сроку
хранения.

Revision ID: 0006_view_sessions_journal
Revises: 0005_ptz
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_view_sessions_journal"
down_revision: str | None = "0005_ptz"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_index("ix_view_sessions_started_at", "view_sessions", ["started_at"])
    op.drop_column("view_sessions", "last_seen_at")

    # Журнал аудита листается курсором «(created_at, id) < последней
    # показанной» вместо OFFSET. Обе колонки должны лежать в одном индексе,
    # иначе база всё равно сортирует выборку целиком. Прежний индекс по
    # одному created_at этим составным перекрывается полностью.
    op.create_index("ix_audit_log_created_at_id", "audit_log", ["created_at", "id"])
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")


def downgrade() -> None:
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.drop_index("ix_audit_log_created_at_id", table_name="audit_log")

    op.add_column(
        "view_sessions",
        sa.Column("last_seen_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.drop_index("ix_view_sessions_started_at", table_name="view_sessions")
