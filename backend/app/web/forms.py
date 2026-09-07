"""Разбор значений, пришедших из формы.

Поле формы — это строка от кого угодно: скрытое подделывается ровно так же
легко, как видимое. `StreamProfile(value)` и `Role(value)` на неизвестной
строке поднимают ValueError, который доходил до глобального обработчика:
оператор видел «Внутренняя ошибка сервиса», а в журнале появлялась пятисотка,
за которой ничего не стояло. Часть обработчиков это уже ловила, часть нет —
поэтому разбор собран здесь, а не повторяется по месту.
"""

from __future__ import annotations

import enum
import ipaddress


def parse_enum[E: enum.Enum](enum_type: type[E], raw: str, default: E | None = None) -> E | None:
    """Значение перечисления из формы. Неизвестное — `default` (по умолчанию None)."""
    try:
        return enum_type(raw)
    except ValueError:
        return default


def parse_cidrs(raw: str) -> tuple[list[str], list[str]]:
    """Строка «сеть, сеть, …» -> (корректные, непонятые).

    Непонятые возвращаются отдельно, а не отбрасываются: `ip_allowed` их молча
    пропускает, и опечатка в единственной подсети превращала ссылку в
    нерабочую для всех — без единого слова оператору о том, что он ошибся.
    Обратный случай не лучше: строка из одних запятых давала пустой список,
    то есть ограничение исчезало целиком.
    """
    good: list[str] = []
    bad: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError:
            bad.append(item)
        else:
            good.append(item)
    return good, bad
