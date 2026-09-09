"""История показаний: кольцевой буфер в Redis.

Почему Redis, а не PostgreSQL: это данные с точной датой смерти. Через час они
не нужны никому, писать их в базу — значит завести самую быстрорастущую
таблицу в схеме (двенадцать строк в минуту круглосуточно) ради графика,
который смотрят раз в неделю. Redis для этого уже есть, кольцевой буфер там
делается двумя командами, а перезапуск с потерей истории мониторинг переживает.

Пишет один worker, читает панель. Ни того, ни другого не смущает, если история
пуста: страница честно скажет, что показания ещё собираются.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import get_settings
from ..logging_setup import get_logger
from ..redis_client import get_redis
from .host import Reading

log = get_logger("metrics")

#: Список замеров, новые в голове.
SERIES_KEY = "metrics:series"
#: Положение дел на последний момент: диски, датчики, процессы.
STATE_KEY = "metrics:state"


def capacity() -> int:
    """Сколько замеров помещается в буфер при текущих настройках."""
    settings = get_settings()
    interval = max(settings.metrics_interval_seconds, 1)
    return max(settings.metrics_retention_minutes * 60 // interval, 2)


async def push(reading: Reading) -> None:
    """Кладёт замер в историю и обновляет снимок текущего состояния.

    Одним конвейером: три команды на пять секунд — это ерунда, но каждая из
    них ещё и ждёт ответ по сети, а worker в это время не делает ничего.
    """
    settings = get_settings()
    redis = get_redis()
    keep = capacity()

    pipe = redis.pipeline()
    pipe.lpush(SERIES_KEY, json.dumps(reading.series, separators=(",", ":")))
    pipe.ltrim(SERIES_KEY, 0, keep - 1)
    # Срок жизни вдвое длиннее самой истории: если worker остановится,
    # ключ уйдёт сам и не оставит после себя график многодневной давности,
    # выглядящий как свежий.
    pipe.expire(SERIES_KEY, settings.metrics_retention_minutes * 60 * 2)
    pipe.set(
        STATE_KEY,
        json.dumps(reading.state, separators=(",", ":")),
        # Состояние живёт немногим дольше периода опроса: устаревшее оно
        # хуже отсутствующего — «диск занят на 40 %» может быть вчерашним.
        ex=max(settings.metrics_interval_seconds * 6, 30),
    )
    await pipe.execute()


async def series(seconds: int, since: float = 0.0) -> list[dict[str, Any]]:
    """Замеры за последние `seconds` секунд, от старых к новым.

    `since` отсекает то, что вызывающий уже показал: страница обновляется раз
    в несколько секунд, и присылать ей заново весь час на каждое обновление
    незачем.
    """
    settings = get_settings()
    interval = max(settings.metrics_interval_seconds, 1)
    # Две лишние записи — на случай, если сбор чуть отставал от расписания.
    count = min(seconds // interval + 2, capacity())
    raw = await get_redis().lrange(SERIES_KEY, 0, count - 1)

    result: list[dict[str, Any]] = []
    for item in raw:
        try:
            entry = json.loads(item)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict) or float(entry.get("at", 0)) <= since:
            continue
        result.append(entry)
    result.reverse()
    return result


async def state() -> dict[str, Any] | None:
    raw = await get_redis().get(STATE_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


async def clear() -> None:
    """Забыть накопленное. Нужно ровно одному сценарию — тестам."""
    await get_redis().delete(SERIES_KEY, STATE_KEY)
