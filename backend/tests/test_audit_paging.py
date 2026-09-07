"""Пагинация журнала аудита по курсору.

Журнал — таблица только на добавление, в неё пишется каждый вход и каждый
просмотр по ссылке. OFFSET на дальней странице заставлял PostgreSQL прочитать
и выбросить всё, что до неё, поэтому листаем курсором (created_at, id).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, tuple_
from sqlalchemy.dialects import postgresql

from app.models import AuditLog
from app.web.admin_views import _format_cursor, _parse_cursor


class _Row:
    """Ровно то, что нужно `_format_cursor`, без обращения к базе."""

    def __init__(self, created_at: dt.datetime, entry_id: uuid.UUID) -> None:
        self.created_at = created_at
        self.id = entry_id


# ─── Курсор ──────────────────────────────────────────────────────────────────
def test_cursor_roundtrip() -> None:
    moment = dt.datetime(2026, 9, 7, 12, 34, 56, tzinfo=dt.UTC)
    entry_id = uuid.uuid4()
    parsed = _parse_cursor(_format_cursor(_Row(moment, entry_id)))  # type: ignore[arg-type]
    assert parsed == (moment, entry_id)


def test_cursor_keeps_the_timezone() -> None:
    """Наивная дата в сравнении с timestamptz — это тихо неверная страница."""
    moment = dt.datetime(2026, 9, 7, 12, 0, tzinfo=dt.UTC)
    parsed = _parse_cursor(_format_cursor(_Row(moment, uuid.uuid4())))  # type: ignore[arg-type]
    assert parsed is not None
    assert parsed[0].tzinfo is not None


def test_garbage_cursor_is_treated_as_absent() -> None:
    """Курсор приходит из адресной строки — подделать его может кто угодно."""
    for raw in ("", "мусор", "|", "не-дата|не-uuid", "2026-09-07T12:00:00+00:00|нет",
                "2026-09-07T12:00:00+00:00", f"нет|{uuid.uuid4()}"):
        assert _parse_cursor(raw) is None


# ─── Запрос ──────────────────────────────────────────────────────────────────
def _sql(where) -> str:
    return str(
        select(AuditLog).where(where).compile(dialect=postgresql.dialect())
    )


def test_cursor_compiles_to_a_row_value_comparison() -> None:
    """Сравнение должно быть по паре целиком, а не по колонкам по отдельности.

    Разложенное на AND/OR условие база не умеет проходить по индексу одним
    поиском — ради этого keyset и затевался.
    """
    cursor = (dt.datetime(2026, 9, 7, tzinfo=dt.UTC), uuid.uuid4())
    sql = _sql(tuple_(AuditLog.created_at, AuditLog.id) < cursor)
    assert "(audit_log.created_at, audit_log.id) <" in sql


def test_composite_index_backs_the_cursor() -> None:
    """Без составного индекса keyset не быстрее OFFSET."""
    indexes = {index.name: [c.name for c in index.columns] for index in AuditLog.__table__.indexes}
    assert indexes.get("ix_audit_log_created_at_id") == ["created_at", "id"]
