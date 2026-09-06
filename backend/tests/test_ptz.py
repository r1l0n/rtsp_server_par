"""Управление обзором: сборка команд, разбор ответов камеры, блокировка.

Обращений к настоящим камерам здесь нет — их нечем подменить (HTTP-моков в
проекте не заводили) и незачем: ошибаться в этом коде можно ровно в двух
местах, и оба проверяются без сети.

Первое — что именно уезжает в камеру. Знак оси, шкала скорости и номер
канала не видны ни в каком логе: неверный знак выглядит как «стрелка вверх
опускает камеру», а лишняя единица в канале — как «поворачивается соседняя
камера регистратора». Второе — разбор ответа: вендоры называют префиксы
неймспейсов как им вздумается и объявляют свой внутренний адрес вместо
доступного снаружи.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import uuid

import pytest

from app.models import Camera, StreamProfile
from app.ptz import lock, onvif, service
from app.ptz.base import PtzError, Target, Vector
from app.ptz.vendors import AxisDriver, DahuaDriver, HikvisionDriver


def _target(**kwargs: object) -> Target:
    defaults: dict[str, object] = {
        "host": "203.0.113.5",
        "port": 80,
        "tls": False,
        "username": "operator",
        "password": "s3cret",
        "channel": 1,
    }
    return Target(**{**defaults, **kwargs})  # type: ignore[arg-type]


def _camera(**kwargs: object) -> Camera:
    defaults: dict[str, object] = {
        "name": "Проходная",
        "host": "203.0.113.5",
        "port": 554,
        "mtx_path": "abcdefgh12345678abcdefgh",
        "profile": StreamProfile.passthrough,
        "on_demand": True,
        "audio_enabled": True,
        "is_enabled": True,
        "ptz_enabled": True,
        "ptz_driver": "auto",
        "ptz_channel": 1,
        "ptz_tls": False,
    }
    camera = Camera(**{**defaults, **kwargs})
    camera.id = uuid.uuid4()
    return camera


# ─── Направления ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("direction", "axis", "sign"),
    [
        ("up", "tilt", 1),
        ("down", "tilt", -1),
        ("left", "pan", -1),
        ("right", "pan", 1),
        ("zoom_in", "zoom", 1),
        ("zoom_out", "zoom", -1),
    ],
)
def test_direction_maps_to_the_expected_axis(direction: str, axis: str, sign: int) -> None:
    """Знак оси проверяем явно: перепутанный tilt не виден никак, кроме камеры,
    которая на «вверх» опускается."""
    vector = Vector.from_direction(direction)
    assert getattr(vector, axis) * sign > 0
    for other in ("pan", "tilt", "zoom"):
        if other != axis:
            assert getattr(vector, other) == 0.0


def test_unknown_direction_is_rejected() -> None:
    with pytest.raises(ValueError):
        Vector.from_direction("diagonal")


def test_speed_is_not_maximal() -> None:
    """На полной скорости прицелиться нельзя — одно нажатие уводит за край."""
    assert 0 < abs(Vector.from_direction("left").pan) < 1.0


# ─── ONVIF: сборка запроса ───────────────────────────────────────────────────
def test_password_digest_depends_on_every_part_and_their_order() -> None:
    """Порядок конкатенации в UsernameToken задан спецификацией.

    Известного «эталонного» вектора здесь нет намеренно: выдуманный эталон
    проверял бы сам себя. Проверяется то, что ломается на практике — что в
    подпись входят все три части и что их нельзя переставить местами.
    """
    nonce, created, password = b"0123456789abcdef", "2026-09-06T10:00:00Z", "s3cret"
    digest = onvif.password_digest(password, nonce, created)

    assert digest == onvif.password_digest(password, nonce, created)  # детерминизм
    assert len(base64.b64decode(digest)) == 20  # SHA-1
    assert digest != onvif.password_digest(password, b"f" * 16, created)
    assert digest != onvif.password_digest(password, nonce, "2026-09-06T10:00:01Z")
    assert digest != onvif.password_digest("other", nonce, created)
    # Перестановка nonce и created даёт другую подпись — порядок значим.
    assert digest != onvif.password_digest(password, created.encode(), nonce.decode())


def test_security_header_carries_nonce_and_created() -> None:
    header = onvif.security_header(
        "operator", "s3cret", b"0123456789abcdef", "2026-09-06T10:00:00Z"
    )
    assert "<wsse:Username>operator</wsse:Username>" in header
    assert base64.b64encode(b"0123456789abcdef").decode() in header
    assert "<wsu:Created>2026-09-06T10:00:00Z</wsu:Created>" in header
    assert "s3cret" not in header  # пароль уходит только хешем


def test_continuous_move_carries_timeout_so_the_camera_stops_itself() -> None:
    """Без Timeout камера крутится, пока её не остановят, — а браузер зрителя
    может умереть ровно в этот момент."""
    body = onvif.continuous_move_body("Profile_1", Vector.from_direction("right"), 2.0)
    assert "<tptz:Timeout>PT2S</tptz:Timeout>" in body
    assert 'x="0.60"' in body


@pytest.mark.parametrize(("seconds", "expected"), [(0.4, "PT1S"), (1.0, "PT1S"), (2.5, "PT3S")])
def test_duration_is_whole_seconds(seconds: float, expected: str) -> None:
    """Дробные xs:duration переваривают не все камеры."""
    assert onvif.duration(seconds) == expected


def test_profile_token_is_escaped() -> None:
    body = onvif.continuous_move_body("A&B<C", Vector(), 1.0)
    assert "A&amp;B&lt;C" in body


# ─── ONVIF: разбор ответа ────────────────────────────────────────────────────
_PROFILES = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
 <s:Body><trt:GetProfilesResponse xmlns:trt="http://www.onvif.org/ver10/media/wsdl">
  <trt:Profiles token="MainStream" fixed="true">
    <tt:Name xmlns:tt="http://www.onvif.org/ver10/schema">main</tt:Name>
  </trt:Profiles>
  <trt:Profiles token="PtzStream">
    <tt:PTZConfiguration xmlns:tt="http://www.onvif.org/ver10/schema" token="c1"/>
  </trt:Profiles>
 </trt:GetProfilesResponse></s:Body></s:Envelope>"""


def test_profiles_are_parsed_regardless_of_namespace_prefixes() -> None:
    assert onvif.parse_profiles(_PROFILES) == [("MainStream", False), ("PtzStream", True)]


def test_profile_with_ptz_wins_over_the_first_one() -> None:
    """Первый профиль камеры сплошь и рядом без PTZ — брать его нельзя."""
    assert onvif.pick_profile(onvif.parse_profiles(_PROFILES)) == "PtzStream"


def test_pick_profile_falls_back_to_the_only_one() -> None:
    assert onvif.pick_profile([("Solo", False)]) == "Solo"
    assert onvif.pick_profile([]) == ""


@pytest.mark.parametrize(
    ("xaddr", "expected"),
    [
        ("http://192.168.1.64/onvif/PTZ", "/onvif/PTZ"),
        ("http://192.168.1.64:8000/onvif/ptz_service", "/onvif/ptz_service"),
        ("https://10.0.0.2/onvif/PTZ", "/onvif/PTZ"),
        ("/onvif/PTZ", "/onvif/PTZ"),
        ("", "/onvif/ptz_service"),
    ],
)
def test_xaddr_keeps_only_the_path(xaddr: str, expected: str) -> None:
    """Камера за NAT объявляет свой внутренний адрес.

    Пойти по нему — это в лучшем случае таймаут, в худшем запрос в чужую
    сеть мимо всей SSRF-проверки. Берём только путь.
    """
    assert onvif.rewrite_xaddr(xaddr) == expected


def test_ptz_xaddr_is_found_in_capabilities() -> None:
    xml = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>
      <tds:GetCapabilitiesResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
       <tds:Capabilities><tt:PTZ xmlns:tt="http://www.onvif.org/ver10/schema">
        <tt:XAddr>http://192.168.1.64/onvif/PTZ</tt:XAddr>
       </tt:PTZ></tds:Capabilities></tds:GetCapabilitiesResponse></s:Body></s:Envelope>"""
    assert onvif.parse_ptz_xaddr(xml) == "http://192.168.1.64/onvif/PTZ"


def test_camera_without_ptz_service_reports_nothing() -> None:
    xml = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>
      <tds:GetCapabilitiesResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
       <tds:Capabilities/></tds:GetCapabilitiesResponse></s:Body></s:Envelope>"""
    assert onvif.parse_ptz_xaddr(xml) == ""


def test_camera_time_is_parsed_for_the_clock_skew() -> None:
    """Ушедшие часы камеры — самая частая причина «пароль верный, а ONVIF нет»."""
    xml = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>
     <tds:GetSystemDateAndTimeResponse xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
      <tt:UTCDateTime xmlns:tt="http://www.onvif.org/ver10/schema">
       <tt:Time><tt:Hour>10</tt:Hour><tt:Minute>30</tt:Minute><tt:Second>15</tt:Second></tt:Time>
       <tt:Date><tt:Year>2026</tt:Year><tt:Month>9</tt:Month><tt:Day>6</tt:Day></tt:Date>
      </tt:UTCDateTime></tds:GetSystemDateAndTimeResponse></s:Body></s:Envelope>"""
    assert onvif.parse_camera_time(xml) == dt.datetime(2026, 9, 6, 10, 30, 15, tzinfo=dt.UTC)


def test_garbage_time_does_not_crash_the_driver() -> None:
    xml = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>
     <tt:UTCDateTime xmlns:tt="http://www.onvif.org/ver10/schema">
      <tt:Date><tt:Year>две тысячи</tt:Year></tt:Date></tt:UTCDateTime></s:Body></s:Envelope>"""
    assert onvif.parse_camera_time(xml) is None


def test_soap_fault_text_is_extracted() -> None:
    xml = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>
      <s:Fault><s:Reason><s:Text>Sender not authorized</s:Text></s:Reason></s:Fault>
      </s:Body></s:Envelope>"""
    assert onvif.fault_text(xml) == "Sender not authorized"


def test_non_xml_answer_is_not_mistaken_for_a_fault() -> None:
    assert onvif.fault_text("<html><body>404</body></html>") == ""
    assert onvif.fault_text("not xml at all") == ""


# ─── Родные протоколы ────────────────────────────────────────────────────────
def test_hikvision_body_uses_its_own_scale_and_duration() -> None:
    body = HikvisionDriver().body(Vector.from_direction("left"), 2.0)
    assert "<pan>-60</pan>" in body
    assert "<tilt>0</tilt>" in body
    assert "<duration>2000</duration>" in body


def test_hikvision_path_follows_the_channel() -> None:
    driver = HikvisionDriver()
    assert driver.path(_target(channel=3), momentary=True).startswith(
        "/ISAPI/PTZCtrl/channels/3/"
    )


def test_dahua_channel_is_zero_based() -> None:
    """У Dahua каналы с нуля, у остальных с единицы.

    Оператор везде вводит номер как в RTSP-пути, поэтому единицу вычитает
    драйвер. Ошибка здесь выглядит как «поворачивается соседняя камера».
    """
    query = DahuaDriver().query(_target(channel=1), Vector.from_direction("left"), "start")
    assert "channel=0" in query
    assert "code=Left" in query
    assert "action=start" in query


@pytest.mark.parametrize(
    ("direction", "code"),
    [
        ("left", "Left"), ("right", "Right"), ("up", "Up"), ("down", "Down"),
        ("zoom_in", "ZoomTele"), ("zoom_out", "ZoomWide"),
    ],
)
def test_dahua_codes(direction: str, code: str) -> None:
    assert DahuaDriver().code(Vector.from_direction(direction)) == code


def test_axis_sends_zero_speeds_to_stop() -> None:
    query = AxisDriver().query(_target(), Vector())
    assert "continuouspantiltmove=0%2C0" in query
    assert "continuouszoommove=0" in query


def test_axis_move_carries_the_direction() -> None:
    query = AxisDriver().query(_target(channel=2), Vector.from_direction("up"))
    assert "camera=2" in query
    assert "continuouspantiltmove=0%2C60" in query


# ─── Блокировка ──────────────────────────────────────────────────────────────
async def test_first_press_takes_control_and_the_second_viewer_waits() -> None:
    camera_id = uuid.uuid4()
    assert await lock.acquire(camera_id, "v:first", 15) == (True, 0)

    taken, retry_after = await lock.acquire(camera_id, "v:second", 15)
    assert taken is False
    assert retry_after > 0


async def test_holder_keeps_extending_while_pressing() -> None:
    camera_id = uuid.uuid4()
    await lock.acquire(camera_id, "v:first", 15)
    assert await lock.acquire(camera_id, "v:first", 15) == (True, 0)
    assert await lock.owner(camera_id) == "v:first"


async def test_release_by_a_stranger_does_not_free_the_camera() -> None:
    camera_id = uuid.uuid4()
    await lock.acquire(camera_id, "v:first", 15)
    await lock.release(camera_id, "v:second")
    assert await lock.owner(camera_id) == "v:first"

    await lock.release(camera_id, "v:first")
    assert await lock.owner(camera_id) is None


async def test_control_passes_on_when_the_hold_expires() -> None:
    camera_id = uuid.uuid4()
    await lock.acquire(camera_id, "v:first", 1)
    await asyncio.sleep(1.1)
    assert await lock.acquire(camera_id, "v:second", 15) == (True, 0)


def test_viewer_holder_does_not_leak_the_cookie() -> None:
    """Идентификатора зрителя достаточно, чтобы смотреть камеру, — в значении
    ключа Redis ему делать нечего."""
    viewer_id = "s3cret-viewer-cookie-value"
    holder = lock.viewer_holder(viewer_id)
    assert viewer_id not in holder
    assert holder.startswith("v:")
    assert holder == lock.viewer_holder(viewer_id)


# ─── Журнал ──────────────────────────────────────────────────────────────────
async def test_audit_is_written_once_per_control_session() -> None:
    """Одна наводка камеры — десятки команд. В журнал идёт сеанс, не нажатие."""
    camera_id = uuid.uuid4()
    assert await service.should_audit(camera_id, "v:first") is True
    assert await service.should_audit(camera_id, "v:first") is False
    # Другой человек на той же камере — отдельная запись.
    assert await service.should_audit(camera_id, "v:second") is True


# ─── Сторож ──────────────────────────────────────────────────────────────────
class _FakeDriver:
    """Драйвер без своей остановки — как Dahua и Axis."""

    name = "fake"

    def __init__(self, stops_itself: bool = False) -> None:
        self.moves: list[tuple[Vector, float]] = []
        self.stops = 0
        self._stops_itself = stops_itself

    @property
    def stops_itself(self) -> bool:
        return self._stops_itself

    async def identify(self, target: Target) -> object:
        raise NotImplementedError

    async def move(self, target: Target, velocity: Vector, seconds: float) -> None:
        self.moves.append((velocity, seconds))

    async def stop(self, target: Target) -> None:
        self.stops += 1


@pytest.fixture
async def fast_ptz(monkeypatch: pytest.MonkeyPatch):
    """Подменяет драйвер и укорачивает команду, чтобы сторож сработал быстро.

    Сторожа снимаем после теста: они живут в глобальном словаре модуля и
    иначе досрабатывали бы посреди следующего теста.
    """
    driver = _FakeDriver()
    settings = service.get_settings()
    monkeypatch.setattr(settings, "ptz_move_seconds", 0.05)
    monkeypatch.setattr(service, "_resolve", lambda camera: _resolved(driver))
    yield driver
    await service.shutdown()


async def _resolved(driver: _FakeDriver) -> tuple[object, Target]:
    return driver, _target()


async def test_press_sends_exactly_one_move(fast_ptz: _FakeDriver) -> None:
    camera = _camera()
    result = await service.press(camera, "left", "u:1")
    assert result.ok
    assert len(fast_ptz.moves) == 1
    assert fast_ptz.moves[0][0].pan < 0


async def test_camera_stops_itself_when_the_browser_goes_quiet(
    fast_ptz: _FakeDriver,
) -> None:
    """Главное свойство пульта: закрытая вкладка не оставляет камеру крутиться."""
    camera = _camera()
    await service.press(camera, "right", "u:1")
    assert fast_ptz.stops == 0

    await asyncio.sleep(0.2)
    assert fast_ptz.stops == 1


async def test_holding_the_key_postpones_the_watchdog(fast_ptz: _FakeDriver) -> None:
    camera = _camera()
    await service.press(camera, "right", "u:1")
    await asyncio.sleep(0.02)
    await service.press(camera, "right", "u:1")
    await asyncio.sleep(0.02)
    # Пока команды идут, останавливать нечего.
    assert fast_ptz.stops == 0


async def test_second_viewer_is_refused_without_touching_the_camera(
    fast_ptz: _FakeDriver,
) -> None:
    camera = _camera()
    await service.press(camera, "left", "v:first")
    before = len(fast_ptz.moves)

    result = await service.press(camera, "right", "v:second")
    assert result.ok is False
    assert result.reason == "busy"
    assert "другой" in result.message
    # Чужое нажатие не должно порождать трафик к камере.
    assert len(fast_ptz.moves) == before


async def test_broken_camera_does_not_keep_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Иначе неисправная камера блокирует пульт для всех на пятнадцать секунд."""
    camera = _camera()

    async def failing(_camera: Camera) -> tuple[object, Target]:
        raise PtzError("камера не отвечает")

    monkeypatch.setattr(service, "_resolve", failing)
    result = await service.press(camera, "left", "u:1")

    assert result.ok is False
    assert result.reason == "error"
    assert await lock.owner(camera.id) is None


# ─── Учётные данные ──────────────────────────────────────────────────────────
def test_credentials_are_taken_from_the_rtsp_link() -> None:
    """Пароли камер сплошь со спецсимволами и хранятся перекодированными."""
    user, password = service.credentials_from_rtsp(
        "rtsp://operator:p%40ss%2Fword@203.0.113.5:554/stream"
    )
    assert (user, password) == ("operator", "p@ss/word")


def test_credentials_survive_encryption() -> None:
    blob = service.pack_credentials("ptz-user", "p@ss:word")
    assert service.unpack_credentials(blob) == ("ptz-user", "p@ss:word")


@pytest.mark.parametrize(
    ("port", "tls", "expected"), [(None, False, 80), (None, True, 443), (8000, False, 8000)]
)
def test_default_port_follows_the_scheme(port: int | None, tls: bool, expected: int) -> None:
    assert service.default_port(_camera(ptz_port=port, ptz_tls=tls)) == expected


# ─── Маршруты ────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_panel_ptz_route_exists_and_demands_a_session(client) -> None:
    """Смоук: маршрут зарегистрирован (не 404) и без входа не работает."""
    response = client.post(
        f"/cameras/{uuid.uuid4()}/ptz",
        data={"action": "move", "direction": "left"},
        follow_redirects=False,
    )
    assert response.status_code != 404
    assert response.status_code in (302, 303, 401, 403)


def test_public_ptz_refuses_a_request_without_the_viewer_cookie(client) -> None:
    response = client.post(
        "/v/nosuchlink/ptz",
        json={"action": "move", "direction": "left"},
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 403


def test_public_ptz_refuses_a_cross_site_style_request(client) -> None:
    """Без X-Requested-With чужая страница не смогла бы дотянуться до пульта:
    поставить этот заголовок кросс-доменно можно только через preflight, а
    CORS у нас не разрешён."""
    response = client.post("/v/nosuchlink/ptz", json={"action": "move", "direction": "left"})
    assert response.status_code == 403
