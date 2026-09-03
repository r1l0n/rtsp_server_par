"""Эндпоинт forward_auth: кто и на каком основании пускается к медиа."""

from __future__ import annotations

import uuid

import pytest

from app.internal import authz
from app.internal.authz import (
    VIEW_COOKIE,
    count_viewers,
    grant,
    grant_operator,
    ip_allowed,
    new_viewer_id,
)

MTX_PATH = "abcdefgh12345678abcdefgh"


@pytest.fixture
def allow_all_links(monkeypatch: pytest.MonkeyPatch):
    """Проверку ссылки в БД подменяем — здесь тестируется маршрутизация доступа."""

    async def always_valid(link_id: uuid.UUID) -> bool:
        return True

    monkeypatch.setattr(authz, "_link_is_valid", always_valid)


@pytest.fixture
def deny_all_links(monkeypatch: pytest.MonkeyPatch):
    async def never_valid(link_id: uuid.UUID) -> bool:
        return False

    monkeypatch.setattr(authz, "_link_is_valid", never_valid)


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _authz(client, uri: str, cookies: dict[str, str] | None = None):
    return client.get(
        "/internal/authz",
        headers={"X-Forwarded-Uri": uri},
        cookies=cookies or {},
    )


# ─── Отказы ──────────────────────────────────────────────────────────────────
def test_denied_without_cookie() -> None:
    with _client() as client:
        assert _authz(client, f"/hls/{MTX_PATH}/index.m3u8").status_code == 403


def test_denied_with_unknown_viewer() -> None:
    with _client() as client:
        response = _authz(
            client, f"/hls/{MTX_PATH}/index.m3u8", {VIEW_COOKIE: "unknown-viewer-id"}
        )
        assert response.status_code == 403


@pytest.mark.parametrize(
    "uri",
    [
        "/",
        "/cameras",
        f"/whep2/{MTX_PATH}/whep",
        "/hls/UPPERCASE12345678/index.m3u8",
        "/hls/short/index.m3u8",
        "/hls//index.m3u8",
        f"/../hls/{MTX_PATH}/index.m3u8",
    ],
)
def test_non_media_uris_denied(uri: str) -> None:
    with _client() as client:
        assert _authz(client, uri).status_code == 403


async def test_denied_when_link_no_longer_valid(deny_all_links) -> None:
    viewer = new_viewer_id()
    link_id = uuid.uuid4()
    await grant(viewer, MTX_PATH, link_id, ttl_seconds=60)

    with _client() as client:
        response = _authz(client, f"/hls/{MTX_PATH}/index.m3u8", {VIEW_COOKIE: viewer})
        assert response.status_code == 403


async def test_viewer_cannot_reach_other_camera(allow_all_links) -> None:
    """Доступ выдан на один путь — соседний перебором не открывается."""
    viewer = new_viewer_id()
    await grant(viewer, MTX_PATH, uuid.uuid4(), ttl_seconds=60)

    with _client() as client:
        other = "zzzzzzzz87654321zzzzzzzz"
        assert _authz(client, f"/hls/{other}/index.m3u8", {VIEW_COOKIE: viewer}).status_code == 403


# ─── Разрешения ──────────────────────────────────────────────────────────────
async def test_granted_viewer_allowed_for_hls_and_whep(allow_all_links) -> None:
    viewer = new_viewer_id()
    link_id = uuid.uuid4()
    await grant(viewer, MTX_PATH, link_id, ttl_seconds=60)

    with _client() as client:
        for uri in (
            f"/hls/{MTX_PATH}/index.m3u8",
            f"/hls/{MTX_PATH}/segment7.mp4",
            f"/whep/{MTX_PATH}/whep",
            f"/whep/{MTX_PATH}/whep/session-id",
        ):
            response = _authz(client, uri, {VIEW_COOKIE: viewer})
            assert response.status_code == 200, uri
            assert response.headers["X-Mtx-Path"] == MTX_PATH
            assert response.headers["X-Link-Id"] == str(link_id)


async def test_viewer_can_hold_several_cameras(allow_all_links) -> None:
    """Открытая вторая ссылка не должна отбирать доступ у первой."""
    viewer = new_viewer_id()
    second = "bbbbbbbb22222222bbbbbbbb"
    await grant(viewer, MTX_PATH, uuid.uuid4(), ttl_seconds=60)
    await grant(viewer, second, uuid.uuid4(), ttl_seconds=60)

    with _client() as client:
        cookies = {VIEW_COOKIE: viewer}
        assert _authz(client, f"/hls/{MTX_PATH}/index.m3u8", cookies).status_code == 200
        assert _authz(client, f"/hls/{second}/index.m3u8", cookies).status_code == 200


async def test_query_string_is_ignored_when_matching_path(allow_all_links) -> None:
    viewer = new_viewer_id()
    await grant(viewer, MTX_PATH, uuid.uuid4(), ttl_seconds=60)
    with _client() as client:
        response = _authz(
            client, f"/hls/{MTX_PATH}/index.m3u8?_HLS_msn=5", {VIEW_COOKIE: viewer}
        )
        assert response.status_code == 200


# ─── Учёт зрителей и фильтр по IP ────────────────────────────────────────────
async def test_viewer_counter_is_per_link() -> None:
    link_id = uuid.uuid4()
    assert await count_viewers(link_id) == 0
    await grant(new_viewer_id(), MTX_PATH, link_id, ttl_seconds=60)
    await grant(new_viewer_id(), MTX_PATH, link_id, ttl_seconds=60)
    assert await count_viewers(link_id) == 2


def test_empty_cidr_list_allows_any_address() -> None:
    assert ip_allowed("203.0.113.7", [])


def test_ip_filter_matches_network() -> None:
    assert ip_allowed("203.0.113.7", ["203.0.113.0/24"])
    assert not ip_allowed("198.51.100.7", ["203.0.113.0/24"])


def test_ip_filter_rejects_unparsable_address() -> None:
    assert not ip_allowed("не-адрес", ["203.0.113.0/24"])


def test_ip_filter_skips_broken_cidr_but_honours_valid_one() -> None:
    assert ip_allowed("203.0.113.7", ["мусор", "203.0.113.0/24"])


# ─── Просмотр оператором из панели ───────────────────────────────────────────
async def test_operator_grant_opens_the_path_without_a_link(deny_all_links) -> None:
    """Публичной ссылки нет, проверять нечего — доступ выдаётся сам по себе.

    deny_all_links здесь важен: он доказывает, что операторский доступ идёт
    мимо проверки ссылок, а не опирается на неё.
    """
    viewer_id = new_viewer_id()
    await grant_operator(viewer_id, MTX_PATH, 900)

    with _client() as client:
        response = _authz(client, f"/whep/{MTX_PATH}/whep", {VIEW_COOKIE: viewer_id})

    assert response.status_code == 200
    assert response.headers["X-Mtx-Path"] == MTX_PATH


async def test_operator_grant_covers_only_its_own_path() -> None:
    """Ключ выдан на конкретный путь и на соседний доступ не распространяется."""
    viewer_id = new_viewer_id()
    await grant_operator(viewer_id, MTX_PATH, 900)

    with _client() as client:
        response = _authz(client, "/hls/zzzzzzzz11112222zzzzzzzz/index.m3u8",
                          {VIEW_COOKIE: viewer_id})

    assert response.status_code == 403


async def test_watching_extends_the_grant(allow_all_links, fake_redis) -> None:
    """Доступ не должен протухать посреди просмотра.

    Ставим ключу почти истёкший TTL и убеждаемся, что успешная проверка
    вернула его к полному сроку: раньше зритель получал 403 ровно через час
    после открытия ссылки, прямо во время трансляции.
    """
    from app.config import get_settings

    viewer_id = new_viewer_id()
    await grant(viewer_id, MTX_PATH, uuid.uuid4(), 30)

    with _client() as client:
        assert _authz(client, f"/hls/{MTX_PATH}/index.m3u8",
                      {VIEW_COOKIE: viewer_id}).status_code == 200

    ttl = await fake_redis.ttl(f"viewer:{viewer_id}")
    assert ttl > get_settings().view_cookie_ttl_minutes * 60 - 10
