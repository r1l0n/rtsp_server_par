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

    sql = buffer.getvalue()
    tables = {
        match.group(1): _columns("(" + match.group(2) + "\n)")
        for match in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\)", sql, re.DOTALL)
    }

    # Колонки, добавленные в уже существующую таблицу. Без этого разбора любое
    # ALTER TABLE проходит мимо теста: колонка есть в моделях, а в схеме,
    # собранной по миграциям, её нет — и тест падает на ровном месте, хотя
    # миграция написана верно.
    for match in re.finditer(r"ALTER TABLE (\w+) ADD COLUMN (.*?);", sql, re.DOTALL):
        described = _describe(match.group(2))
        if described is not None:
            tables.setdefault(match.group(1), {})[described[0]] = described[1]

    # И снятые колонки — иначе тест видел бы их вечно: DROP COLUMN проходил
    # мимо разбора, колонка оставалась в схеме «по миграциям», и удаление поля
    # из модели падало бы как расхождение, хотя миграция написана верно.
    for match in re.finditer(r"ALTER TABLE (\w+) DROP COLUMN (\w+);", sql):
        tables.get(match.group(1), {}).pop(match.group(2).strip('"'), None)

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


def _upgrade_sql() -> str:
    """SQL всей цепочки миграций, отрендеренный офлайн."""
    from alembic.config import Config

    from alembic import command

    buffer = io.StringIO()
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.stdout = buffer
    with contextlib.redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


def test_audit_log_is_protected_from_updates() -> None:
    """Журнал аудита должен быть защищён на уровне БД, а не только кодом."""
    sql = _upgrade_sql()
    assert "CREATE TRIGGER audit_log_no_update_delete" in sql
    assert "BEFORE UPDATE OR DELETE ON audit_log" in sql


def test_audit_log_deletion_is_open_only_to_the_retention_flag() -> None:
    """Уборка по сроку хранения — единственная дверь в защите журнала.

    Без неё таблицу нельзя было подрезать вообще: DELETE поднимал исключение,
    и администратору, упёршемуся в диск, оставалось снять триггер целиком.
    Дверь именованная и открывается только на DELETE — UPDATE обязан
    оставаться запрещённым при любом значении флага.
    """
    from app.worker import AUDIT_RETENTION_UNLOCK

    body = _upgrade_sql()
    body = body[body.rindex("CREATE OR REPLACE FUNCTION audit_log_is_append_only") :]
    gate = body[: body.index("$$ LANGUAGE plpgsql")]

    assert "TG_OP = 'DELETE'" in gate
    assert "current_setting('rtspgw.audit_retention', true) = 'on'" in gate
    assert "RAISE EXCEPTION" in gate
    # Имя параметра продублировано в worker.py — оно обязано совпадать,
    # иначе уборка молча упирается в триггер и журнал растёт дальше.
    assert "rtspgw.audit_retention" in AUDIT_RETENTION_UNLOCK
    # SET LOCAL, а не SET: иначе флаг уедет в соседний запрос вместе
    # с соединением из пула и откроет дверь кому попало.
    assert AUDIT_RETENTION_UNLOCK.startswith("SET LOCAL ")
