"""Ограничение частоты запросов — фиксированное окно на счётчиках Redis.

Именно фиксированное, а не скользящее (раньше здесь было написано обратное).
Счётчик заводится со сроком и по его истечении обнуляется разом, поэтому на
стыке двух окон проходит до двойного лимита: двадцать попыток входа в
последнюю секунду одного окна и ещё двадцать в первую секунду следующего.

Для выбранных значений это приемлемо — они рассчитаны на перебор, а не на
точный учёт, — но знать об этом надо: скользящее окно такой всплеск не
пропускает, и читатель, поверивший прежнему описанию, считал защиту строже,
чем она есть.
"""

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
#: И отдельно по учётной записи. Лимит по сессии считается по её
#: идентификатору, а сессий со статусом «пароль принят, жду код» можно завести
#: сколько угодно, зная один валидный пароль, — по одной на каждые шесть
#: попыток. Проверка кода восстановления стоит дорого (argon2 на каждый живой
#: код), поэтому счёт нужен и по человеку, а не только по вкладке.
TOTP_BY_ACCOUNT = Limit(limit=30, window=900)
#: «Забыл пароль»: форма открыта всем, поэтому лимит и по адресу отправителя
#: запроса, и по названному ящику — иначе ею завалят чужую почту.
RESET_BY_IP = Limit(limit=5, window=900)
RESET_BY_ACCOUNT = Limit(limit=3, window=900)
#: Открытие публичной ссылки: защита от перебора slug/токена.
PUBLIC_VIEW_BY_IP = Limit(limit=120, window=60)
#: Пульт шлёт команду примерно раз в 700 мс, пока кнопку держат, поэтому
#: лимит высокий: он ловит скрипт, а не живого человека со стрелками.
PTZ_BY_HOLDER = Limit(limit=240, window=60)
#: Ввод пароля к защищённой ссылке.
LINK_PASSWORD_BY_IP = Limit(limit=10, window=600)
#: Открытие ссылки-приглашения: защита от перебора токенов.
INVITE_BY_IP = Limit(limit=30, window=600)
#: Отправка приглашений одним администратором — чтобы панель нельзя было
#: превратить в рассыльщик спама с нашего домена (и сжечь репутацию SMTP).
INVITE_SEND_BY_ACTOR = Limit(limit=30, window=3600)
#: Проверочные письма: кнопка «Отправить проверочное письмо» не должна
#: превращаться в способ долбить чужой SMTP с нашего адреса.
MAIL_TEST_BY_ACTOR = Limit(limit=10, window=600)


async def hit(bucket: str, key: str, limit: Limit) -> Decision:
    """Учитывает попытку и говорит, можно ли её выполнять.

    Окно заводится тем же атомарным шагом, что и счётчик. Раньше EXPIRE шёл
    отдельной командой после разбора ответа, и это давало два неприятных
    исхода: два одновременных первых запроса сдвигали окно вперёд, а обрыв
    связи с Redis ровно между командами оставлял ключ без TTL навсегда —
    при `maxmemory-policy noeviction` такой счётчик уже никогда не истечёт,
    и адрес или учётная запись оказывались заблокированы до ручной уборки.

    `SET NX EX` создаёт ключ с готовым сроком, только если его ещё нет, и не
    трогает уже идущее окно; INCR и TTL идут следом в той же транзакции.
    """
    from ..redis_client import get_redis

    redis_key = f"rl:{bucket}:{key}"

    pipe = get_redis().pipeline(transaction=True)
    pipe.set(redis_key, 0, ex=limit.window, nx=True)
    pipe.incr(redis_key)
    pipe.ttl(redis_key)
    _, count, ttl = await pipe.execute()

    count = int(count)
    # TTL -1 (ключ без срока) в норме недостижим, но если он всё же случился —
    # чиним, иначе счётчик останется навсегда.
    if ttl is None or int(ttl) < 0:
        await get_redis().expire(redis_key, limit.window)
        ttl = limit.window

    if count > limit.limit:
        return Decision(allowed=False, remaining=0, retry_after=int(ttl))
    return Decision(allowed=True, remaining=limit.limit - count, retry_after=0)


async def reset(bucket: str, key: str) -> None:
    """Сбрасывает счётчик — вызывается после успешного входа."""
    from ..redis_client import get_redis

    await get_redis().delete(f"rl:{bucket}:{key}")
