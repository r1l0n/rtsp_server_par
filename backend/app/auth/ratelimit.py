"""Ограничение частоты запросов (скользящее окно на счётчиках Redis)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after: int


@dataclass(frozen=True, slots=True)
class Limit:
    #: Сколько попыток разрешено в окне.
    limit: int
    #: Длина окна в секундах.
    window: int


#: Вход по паролю: по IP и отдельно по логину, чтобы ни распределённый перебор
#: одного аккаунта, ни перебор логинов с одного адреса не проходили.
LOGIN_BY_IP = Limit(limit=20, window=300)
LOGIN_BY_ACCOUNT = Limit(limit=10, window=900)
#: Второй фактор перебирается быстрее — 6 цифр, поэтому окно жёстче.
TOTP_BY_SESSION = Limit(limit=6, window=300)
#: Открытие публичной ссылки: защита от перебора slug/токена.
PUBLIC_VIEW_BY_IP = Limit(limit=120, window=60)
#: Ввод пароля к защищённой ссылке.
LINK_PASSWORD_BY_IP = Limit(limit=10, window=600)


async def hit(bucket: str, key: str, limit: Limit) -> Decision:
    """Учитывает попытку и говорит, можно ли её выполнять."""
    from ..redis_client import get_redis

    redis_key = f"rl:{bucket}:{key}"
    redis = get_redis()

    pipe = redis.pipeline()
    pipe.incr(redis_key)
    pipe.ttl(redis_key)
    count, ttl = await pipe.execute()

    if ttl is None or ttl < 0:
        # Ключ только что создан (или потерял TTL) — задаём окно.
        await redis.expire(redis_key, limit.window)
        ttl = limit.window

    if count > limit.limit:
        return Decision(allowed=False, remaining=0, retry_after=int(ttl))
    return Decision(allowed=True, remaining=limit.limit - int(count), retry_after=0)


async def reset(bucket: str, key: str) -> None:
    """Сбрасывает счётчик — вызывается после успешного входа."""
    from ..redis_client import get_redis

    await get_redis().delete(f"rl:{bucket}:{key}")
