"""audit_log: срок хранения при сохранённой защите от правки

Журнал был защищён триггером, запрещающим и UPDATE, и DELETE, — и это верно:
запись о входе или о выдаче ссылки не должна меняться задним числом. Но срока
хранения у таблицы не было ни одного, а строка в неё пишется на каждое
открытие публичной ссылки и на каждый отказ. То есть она росла быстрее всех
остальных, не подрезалась никогда и подрезана быть не могла: DELETE поднимал
исключение. Единственным выходом для администратора, упёршегося в диск, было
снять триггер — то есть отключить контроль целостности ради уборки.

Триггер остаётся на месте, но получает ровно одну именованную дверь: DELETE
проходит, если в текущей транзакции выставлен параметр
`rtspgw.audit_retention = 'on'`. Ставит его только уборщик worker'а и только
через SET LOCAL, поэтому дверь закрывается вместе с транзакцией и не может
утечь в соседнюю через пул соединений.

UPDATE остаётся запрещённым безусловно: у уборки нет причин править строки,
а «поправить запись задним числом» — ровно то, от чего журнал защищают.

Revision ID: 0007_audit_retention
Revises: 0006_view_sessions_journal
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_audit_retention"
down_revision: str | None = "0006_view_sessions_journal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Имя параметра сессии. Продублировано в app/worker.py — там же, где его
#: выставляют. Менять только вместе.
FLAG = "rtspgw.audit_retention"


def upgrade() -> None:
    # CREATE OR REPLACE, а не DROP + CREATE: триггер ссылается на функцию по
    # имени, и промежуточного состояния «триггер есть, функции нет» быть
    # не должно — в него попадёт любая вставка, идущая параллельно миграции.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION audit_log_is_append_only()
        RETURNS TRIGGER AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               AND current_setting('{FLAG}', true) = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'audit_log является журналом только на добавление';
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
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
