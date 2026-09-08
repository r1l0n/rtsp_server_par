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


def test_rail_labels_keep_their_names_while_hidden() -> None:
    """Названия разделов прячутся прозрачностью, а не display/visibility.

    Полоса меню — один ряд иконок, и всё имя ссылки живёт в спрятанном ярлыке.
    display:none и visibility:hidden убрали бы его из дерева доступности, и
    скринридер прочитал бы шесть раз «ссылка». Глазами это не ловится вовсе.

    pointer-events там по той же причине наоборот обязателен: спрятанный ярлык
    лежит поверх содержимого и без него перехватывал бы чужие клики.
    """
    label = _block(".rail-label {")
    assert "opacity: 0" in label
    assert "pointer-events: none" in label
    assert "display: none" not in label
    assert "visibility: hidden" not in label


def test_rail_opens_for_the_keyboard_too() -> None:
    """Ярлыки раскрываются и по фокусу, а не только под курсором.

    С клавиатуры полоса иначе остаётся рядом безымянных квадратов: видно,
    что фокус куда-то переехал, но не видно куда.
    """
    assert ".sidebar:focus-within .rail-label" in CSS


def test_the_logo_is_not_painted_like_the_stroked_icons() -> None:
    """Фирменный знак рисуется заливкой, а иконки — обводкой.

    В `.i` стоит `fill: none`, и CSS перебивает атрибуты fill внутри <symbol>.
    Достаточно кому-нибудь свести знак к общему классу иконок — и в углу
    каждой страницы останется пустое место. Ошибка тихая: разметка на месте,
    размеры на месте, не видно ничего.
    """
    logo = _block(".logo {")
    assert "fill" not in logo
    assert "stroke" not in logo


def test_flyout_band_is_reserved_beside_the_rail() -> None:
    """Ярлыки всплывают в отведённую дорожку, а не поверх содержимого.

    Стоит вернуть в каркас голый --rail, и подписи разделов лягут на плитки
    камер. Видно это только под курсором и только на широком экране, поэтому
    поймать глазами почти нельзя.
    """
    assert "var(--rail-flyout)" in _block(".shell {")
    # Потолок ширины ярлыка тоже считается от дорожки: иначе длинное название
    # раздела вылезет за неё и накроет содержимое.
    assert "var(--rail-flyout)" in _block(".rail-label {")


def _translate_x(rule: str) -> int:
    """Горизонтальный сдвиг из `transform: translate(...)` в пикселях."""
    value = re.search(r"transform: translate\(([^,]+),", rule).group(1).strip()
    token = re.fullmatch(r"var\((--space-\d)\)", value)
    if token:
        value = _tokens(":root {")[token.group(1)]
    return int(value.removesuffix("px"))


def test_the_label_moves_toward_the_icon_being_pointed_at() -> None:
    """Ярлык подъезжает к своей иконке, а не отъезжает от неё.

    Движение к точке, на которую смотрят, читается как ответ на наведение;
    движение прочь — будто ярлык убегает от курсора. Заодно это причина, по
    которой дорожка считается по положению в покое, а не по ближнему: поодаль
    держатся все ярлыки, кроме одного.

    Привязка именно к пункту, а не к полосе целиком: раскрытие всего столбика
    показывает, что полоса ожила, но не отвечает на вопрос «где я сейчас».
    """
    rest = _translate_x(_block(".rail-label {"))
    near = _translate_x(_block(".rail-item:hover .rail-label,"))
    assert near < rest


def test_narrow_screens_name_the_sections_without_hover() -> None:
    """На телефоне наведения нет — там подписи стоят на месте, а не всплывают.

    Полоса из одних иконок на сенсорном экране превращается в ребус: узнать,
    что за иконка, можно только ткнув в неё и посмотрев, куда унесло.
    """
    narrow = _block("@media (max-width: 860px)")
    assert ".rail-label" in narrow
    assert "opacity: 1" in narrow
