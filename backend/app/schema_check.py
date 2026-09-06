"""Сверка схемы базы с миграциями.

Миграции накатываются отдельной командой, а не при старте контейнера, и это
осознанно: `alembic upgrade` внутри запуска означал бы, что неудачная миграция
роняет панель при каждом рестарте, а api и worker накатывали бы её наперегонки.

Цена отдельной команды — её можно забыть при обновлении. Тогда код уже знает
про таблицу, которой в базе ещё нет, и сервис отвечает пятисоткой на одной
форме, а причина видна только в трассировке (так после появления
`password_resets` перестало работать «Забыли пароль?»). Поэтому расхождение
называется вслух: строкой в журнале при старте и полем `schema` в /readyz.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Каталог миграций: /srv/alembic рядом с /srv/app в образе, backend/alembic
#: в рабочей копии.
ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"

#: Команда, которой чинится расхождение. Та же, что в docs/install.md.
UPGRADE_HINT = "docker compose run --rm api alembic upgrade head"


@lru_cache(maxsize=1)
def head_revision() -> str:
    """Ревизия, которую ожидает код. Пустая строка — каталог миграций не найден."""
    if not ALEMBIC_DIR.is_dir():
        return ""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    # Config без файла: alembic.ini здесь не нужен, а его script_location
    # задан относительно рабочего каталога и из процесса uvicorn не сработал бы.
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    return ",".join(sorted(ScriptDirectory.from_config(config).get_heads()))


async def db_revision(session: AsyncSession) -> str | None:
    """Ревизия, на которой стоит база. None — миграции не накатывались ни разу."""
    if await session.scalar(text("SELECT to_regclass('public.alembic_version')")) is None:
        return None
    return await session.scalar(text("SELECT version_num FROM alembic_version"))


async def status(session: AsyncSession) -> tuple[bool, str]:
    """(схема соответствует коду, короткое описание состояния)."""
    head = head_revision()
    if not head:
        # Каталога миграций рядом нет — сказать нечего, и молчание здесь
        # честнее ложной тревоги.
        return True, "неизвестно"

    current = await db_revision(session)
    if current is None:
        return False, f"миграции не накатывались, код ожидает {head}"
    if current != head:
        return False, f"база на {current}, код ожидает {head}"
    return True, current
