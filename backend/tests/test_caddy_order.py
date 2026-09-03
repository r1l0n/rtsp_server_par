"""Порядок директив в медиа-обработчиках Caddy.

Отдельный файл, потому что проверка нетипичная: она про поведение самого
Caddy, а не про согласованность значений между конфигами.

Внутри `handle` Caddy исполняет директивы не в том порядке, в каком они
написаны, а по своему встроенному списку — и в нём `uri` стоит РАНЬШЕ
`forward_auth`. Из-за этого `uri strip_prefix /whep` срабатывал до проверки
прав, и /internal/authz получал URI вида «/<path>/whep» вместо
«/whep/<path>/whep». Он его не узнавал и отвечал 403 на каждый медиа-запрос:
сервис не показывал ни одной камеры, а в логах не было ничего, кроме голого
403 без причины. Лечится обёрткой `route`, которая сохраняет порядок как
написано, — и вот это здесь и закреплено.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CADDYFILE = Path(__file__).resolve().parents[2] / "Caddyfile"

MEDIA_HANDLERS = ("@whep_create", "@whep_session", "@hls")


def _block(text: str, opener: str) -> str:
    """Тело блока `handle <opener> { ... }` по балансу скобок."""
    start = text.index(f"handle {opener} {{")
    cursor = text.index("{", start)
    depth = 0
    for position in range(cursor, len(text)):
        if text[position] == "{":
            depth += 1
        elif text[position] == "}":
            depth -= 1
            if depth == 0:
                return text[cursor + 1 : position]
    raise AssertionError(f"незакрытый блок handle {opener}")


@pytest.fixture(scope="module")
def caddyfile() -> str:
    return CADDYFILE.read_text(encoding="utf-8")


@pytest.mark.parametrize("handler", MEDIA_HANDLERS)
def test_media_handler_pins_order_with_route(caddyfile: str, handler: str) -> None:
    body = _block(caddyfile, handler)
    assert "route {" in body, (
        f"handle {handler}: без route Caddy переставит uri перед forward_auth"
    )


@pytest.mark.parametrize("handler", MEDIA_HANDLERS)
def test_authorization_runs_before_the_prefix_is_stripped(
    caddyfile: str, handler: str
) -> None:
    body = _block(caddyfile, handler)
    assert body.index("forward_auth") < body.index("uri strip_prefix"), (
        f"handle {handler}: проверка прав должна идти до среза префикса"
    )


@pytest.mark.parametrize("handler", MEDIA_HANDLERS)
def test_forward_auth_target_is_the_authz_endpoint(caddyfile: str, handler: str) -> None:
    body = _block(caddyfile, handler)
    assert "forward_auth api:8000" in body
    assert "uri /internal/authz" in body
