"""HTTP к камере: что транспорт пропускает, а что обрывает.

В остальных тестах PTZ драйверы подменяются целиком, и сам транспорт не
исполняется ни разу. Здесь он проверяется через httpx.MockTransport —
настоящих камер по-прежнему нет, но код запроса и разбора ответа настоящий.
"""

from __future__ import annotations

import httpx
import pytest

from app.ptz import transport
from app.ptz.base import PtzAuthError, PtzError, Target


def _target(**kwargs: object) -> Target:
    defaults: dict[str, object] = {
        "host": "203.0.113.5",
        "port": 80,
        "tls": False,
        "username": "operator",
        "password": "secret",
    }
    defaults.update(kwargs)
    return Target(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def camera_answers(monkeypatch: pytest.MonkeyPatch):
    """Подменяет клиент на MockTransport с заданным ответом."""

    def install(handler) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(transport, "_client", client)

    yield install
    monkeypatch.setattr(transport, "_client", None)
    transport._auth_cache.clear()


# ─── Обычный ответ ───────────────────────────────────────────────────────────
async def test_ordinary_answer_reaches_the_driver_intact(camera_answers) -> None:
    body = "<DeviceInfo><model>DS-2DE4A425</model></DeviceInfo>"
    camera_answers(
        lambda request: httpx.Response(
            200, text=body, headers={"Content-Type": "application/xml"}
        )
    )

    response = await transport.request(_target(), "GET", "/ISAPI/System/deviceInfo")

    assert response.status_code == 200
    assert response.text == body


async def test_headers_survive_the_repacking(camera_answers) -> None:
    """ensure_digest читает www-authenticate — заголовок обязан дойти."""
    camera_answers(
        lambda request: httpx.Response(
            200, text="ok", headers={"WWW-Authenticate": 'Basic realm="camera"'}
        )
    )

    response = await transport.request(_target(), "GET", "/whatever")

    with pytest.raises(PtzAuthError, match="базовую аутентификацию"):
        transport.ensure_digest(response, _target())


async def test_a_body_right_under_the_limit_still_passes(camera_answers) -> None:
    payload = "a" * transport.MAX_RESPONSE_BYTES
    camera_answers(lambda request: httpx.Response(200, text=payload))

    response = await transport.request(_target(), "GET", "/big-but-legal")

    assert len(response.text) == transport.MAX_RESPONSE_BYTES


# ─── Ограничение размера ─────────────────────────────────────────────────────
async def test_an_oversized_body_is_cut_off_and_explained(camera_answers) -> None:
    """Ограничение в драйверах было мнимым: срез шёл после полного чтения.

    Адрес управления — это host камеры с произвольным портом, так что
    направить опознание на файловый сервер оператор может и без злого
    умысла, а тело ответа попадало прямо в память процесса панели.
    """
    payload = b"a" * (transport.MAX_RESPONSE_BYTES + 1024)
    camera_answers(lambda request: httpx.Response(200, content=payload))

    with pytest.raises(PtzError, match="отвечает не камера"):
        await transport.request(_target(), "GET", "/not-a-camera")


async def test_the_size_error_is_not_disguised_as_a_dead_camera(
    camera_answers,
) -> None:
    """«Камера не отвечает» увело бы оператора искать сеть вместо порта."""
    payload = b"a" * (transport.MAX_RESPONSE_BYTES + 1)
    camera_answers(lambda request: httpx.Response(200, content=payload))

    with pytest.raises(PtzError) as failure:
        await transport.request(_target(), "GET", "/not-a-camera")

    assert "не отвечает" not in str(failure.value)


# ─── Отказы ──────────────────────────────────────────────────────────────────
async def test_unreachable_camera_becomes_a_readable_error(camera_answers) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    camera_answers(refuse)

    with pytest.raises(PtzError, match="камера не отвечает"):
        await transport.request(_target(), "GET", "/ISAPI/System/deviceInfo")


async def test_rejected_credentials_become_an_auth_error(camera_answers) -> None:
    camera_answers(lambda request: httpx.Response(401, text="denied"))

    with pytest.raises(PtzAuthError, match="логин или пароль"):
        await transport.request(_target(), "GET", "/ISAPI/System/deviceInfo")
