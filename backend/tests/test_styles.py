"""Проверки таблицы стилей.

Тема задаётся набором токенов, и почти все ошибки в ней — не «некрасиво»,
а «в одной из тем элемент пропал». Проверяем то, что глазами ловится только
переключением темы туда-сюда на каждой странице.
"""

from __future__ import annotations

import re

from app.web.templating import STATIC_DIR

CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

TOKEN = re.compile(r"^\s*(--[a-z0-9-]+)\s*:\s*([^;]+);", re.MULTILINE)


def _block(selector: str) -> str:
    """Тело первого правила с этим селектором."""
    start = CSS.index(selector)
    open_brace = CSS.index("{", start)
    depth, i = 1, open_brace + 1
    while depth:
        if CSS[i] == "{":
            depth += 1
        elif CSS[i] == "}":
            depth -= 1
        i += 1
    return CSS[open_brace + 1 : i - 1]


def _tokens(selector: str) -> dict[str, str]:
    return {name: value.strip() for name, value in TOKEN.findall(_block(selector))}


def test_light_theme_is_declared_identically_in_both_places() -> None:
    """Выбор «светлая» и «как в системе» обязаны давать один и тот же результат.

    Блока два: один по атрибуту, второй под медиазапросом — сервер не знает
    настройку ОС. Их легко поправить по одному и получить две слегка разные
    светлые темы, которые никто не сравнит вживую.
    """
    explicit = _tokens(':root[data-theme="light"]')
    from_system = _tokens(':root[data-theme="auto"]')
    assert explicit == from_system


def test_every_colour_token_exists_in_both_themes() -> None:
    """Токен, забытый в светлой теме, достаётся ей от тёмной.

    Выглядит это как чёрный текст на чёрном фоне в одном углу страницы —
    и находится случайно, месяцы спустя.
    """
    dark = _tokens(":root {")
    light = _tokens(':root[data-theme="light"]')

    # Метрики, шрифты и производные от --text цвета темой не различаются.
    shared = {"--font", "--mono", "--muted", "--faint"}
    colour_tokens = {
        name for name in dark
        if not name.startswith(("--space", "--radius")) and name not in shared
    }
    assert not colour_tokens - set(light), "не заданы в светлой теме"


def test_overlay_is_its_own_token() -> None:
    """Затемнение под модальным окном нельзя брать из шкалы neutral.

    Шкалы в светлой теме перевёрнуты, и `neutral-900` там почти белый —
    затемнение получалось бы белым, а окно тонуло в засветке.
    """
    assert "--overlay" in _tokens(":root {")
    assert "--overlay" in _tokens(':root[data-theme="light"]')
    assert "var(--overlay)" in CSS


def test_container_stretches_inside_the_grid_shell() -> None:
    """`margin: 0 auto` у грид-элемента отменяет растягивание.

    Без явного width:100% содержимое сжимается по контенту и центрируется,
    оставляя пустые поля в пол-экрана. Ровно это уже случалось.
    """
    container = _block(".container {")
    assert "width: 100%" in container
    assert "max-width" in container


def test_brand_colour_is_not_used_for_every_link() -> None:
    """Фирменный красный — бренд и главное действие, а не каждая ссылка.

    Иначе интерфейс выглядит так, будто всё на экране требует вмешательства.
    """
    assert "--link" in _tokens(":root {")
    assert "a { color: var(--link);" in CSS
