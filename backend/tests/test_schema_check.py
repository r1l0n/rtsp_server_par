"""Сверка схемы с миграциями — та, что кричит в журнал при старте.

Она страхует от единственной, зато повторяющейся ошибки обновления: образы
пересобрали, `alembic upgrade head` забыли. Ошибка в самой страховке была бы
хуже её отсутствия — молчание там означает «всё в порядке».
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app import schema_check

BACKEND_DIR = Path(__file__).resolve().parents[1]


class _FakeSession:
    """Отдаёт заранее заготовленные ответы на scalar() по порядку."""

    def __init__(self, *answers: Any) -> None:
        self._answers = list(answers)

    async def scalar(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._answers.pop(0)


def test_head_matches_what_alembic_would_upgrade_to() -> None:
    """Проверка обязана знать ту же голову, что и `alembic upgrade head`."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()

    assert len(heads) == 1, f"миграции разветвились на несколько голов: {heads}"
    assert schema_check.head_revision() == heads[0]


async def test_missing_alembic_version_table_is_reported() -> None:
    """to_regclass вернул NULL — базу не мигрировали ни разу."""
    fresh, state = await schema_check.status(_FakeSession(None))  # type: ignore[arg-type]

    assert not fresh
    assert "не накатывались" in state


async def test_revision_behind_head_is_reported() -> None:
    """Ровно тот случай, из-за которого «Забыли пароль?» отвечало пятисоткой."""
    session = _FakeSession("alembic_version", "0003_mail_settings")
    fresh, state = await schema_check.status(session)  # type: ignore[arg-type]

    assert not fresh
    assert "0003_mail_settings" in state
    assert schema_check.head_revision() in state


async def test_current_revision_is_quiet() -> None:
    session = _FakeSession("alembic_version", schema_check.head_revision())
    fresh, _ = await schema_check.status(session)  # type: ignore[arg-type]

    assert fresh
