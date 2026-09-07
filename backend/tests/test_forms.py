"""Разбор значений из формы.

Скрытое поле формы подделывается так же легко, как видимое, поэтому неизвестное
значение обязано превращаться в понятную ошибку, а не в пятисотку.
"""

from __future__ import annotations

from app.models import Role, StreamProfile
from app.web.forms import parse_cidrs, parse_enum


# ─── Перечисления ────────────────────────────────────────────────────────────
def test_known_value_is_parsed() -> None:
    assert parse_enum(StreamProfile, "transcode") is StreamProfile.transcode
    assert parse_enum(Role, "admin") is Role.admin


def test_unknown_value_does_not_raise() -> None:
    """Раньше здесь был ValueError, доходивший до страницы «Внутренняя ошибка»."""
    assert parse_enum(StreamProfile, "../../etc/passwd") is None
    assert parse_enum(Role, "superuser") is None
    assert parse_enum(Role, "") is None


def test_default_is_returned_for_unknown_value() -> None:
    assert parse_enum(StreamProfile, "мусор", StreamProfile.passthrough) is (
        StreamProfile.passthrough
    )


# ─── Подсети ─────────────────────────────────────────────────────────────────
def test_addresses_and_networks_are_accepted() -> None:
    good, bad = parse_cidrs("203.0.113.7, 203.0.113.0/24 , 2001:db8::/32")
    assert good == ["203.0.113.7", "203.0.113.0/24", "2001:db8::/32"]
    assert bad == []


def test_typo_is_reported_instead_of_being_swallowed() -> None:
    """ip_allowed молча пропускает непонятую подсеть.

    Из-за этого опечатка в единственной подсети превращала ссылку в
    нерабочую для всех, и оператор не получал об этом ни слова.
    """
    good, bad = parse_cidrs("203.0.113.0/24, 203.0.113.999, не-адрес")
    assert good == ["203.0.113.0/24"]
    assert bad == ["203.0.113.999", "не-адрес"]


def test_empty_input_means_no_restriction() -> None:
    assert parse_cidrs("") == ([], [])
    assert parse_cidrs("   ") == ([], [])


def test_separators_only_do_not_silently_drop_the_restriction() -> None:
    """Строка из одних запятых — это пустой список, то есть «пускать всех».

    Само по себе это допустимо (пустое поле означает ровно то же), важно лишь,
    что мусор при этом не оседает в базе.
    """
    assert parse_cidrs(",, ,") == ([], [])
