"""Миграции обязаны описывать ту же схему, что и модели.

Проверка идёт офлайн: alembic рендерит SQL, модели компилируются в DDL, и мы
сравниваем колонки. Расхождение обычно означает, что кто-то поправил модель и
забыл миграцию — на проде это выглядит как падение при первом же запросе.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.models import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]
_SKIP_PREFIXES = ("PRIMARY KEY", "FOREIGN KEY", "CONSTRAINT", "UNIQUE", "CHECK")


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return parts


def _describe(fragment: str) -> tuple[str, str] | None:
    text = " ".join(fragment.split())
    if not text or text.upper().startswith(_SKIP_PREFIXES):
        return None
    name, _, rest = text.partition(" ")
    nullable = "NOT NULL" not in rest.upper()
    type_ = re.sub(r"\s+(NOT NULL|DEFAULT .*)", "", rest, flags=re.IGNORECASE).strip()
    return name.strip('"'), f"{type_.upper()}|{'NULL' if nullable else 'NOTNULL'}"


def _columns(ddl: str) -> dict[str, str]:
    body = ddl[ddl.index("(") + 1 : ddl.rindex(")")]
    described = (_describe(part) for part in _split_top_level(body))
    return dict(item for item in described if item is not None)


def _from_models() -> dict[str, dict[str, str]]:
    dialect = postgresql.dialect()
    return {
        name: _columns(str(CreateTable(table).compile(dialect=dialect)))
        for name, table in Base.metadata.tables.items()
    }


def _from_migrations() -> dict[str, dict[str, str]]:
    from alembic.config import Config

    from alembic import command

    buffer = io.StringIO()
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.stdout = buffer
    with contextlib.redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)

    tables = {
        match.group(1): _columns("(" + match.group(2) + "\n)")
        for match in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\)", buffer.getvalue(), re.DOTALL)
    }
    tables.pop("alembic_version", None)  # служебная таблица самого alembic
    return tables


def test_migration_creates_every_model_table() -> None:
    assert set(_from_migrations()) == set(_from_models())


def test_every_column_matches_between_models_and_migration() -> None:
    models, migrations = _from_models(), _from_migrations()
    mismatches = [
        f"{table}.{column}: модели={models[table].get(column)} "
        f"миграция={migrations[table].get(column)}"
        for table in sorted(set(models) & set(migrations))
        for column in sorted(set(models[table]) | set(migrations[table]))
        if models[table].get(column) != migrations[table].get(column)
    ]
    assert not mismatches, "\n".join(mismatches)


def test_audit_log_is_protected_from_updates() -> None:
    """Журнал аудита должен быть защищён на уровне БД, а не только кодом."""
    from alembic.config import Config

    from alembic import command

    buffer = io.StringIO()
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.stdout = buffer
    with contextlib.redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)

    sql = buffer.getvalue()
    assert "CREATE TRIGGER audit_log_no_update_delete" in sql
    assert "BEFORE UPDATE OR DELETE ON audit_log" in sql
