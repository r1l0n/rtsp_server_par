"""Сцепки между docker-compose, Caddyfile, mediamtx.yml и кодом.

Эти файлы связаны неявно: подсети из compose прописаны в правах MediaMTX,
регулярка в Caddyfile должна совпадать с алфавитом имён путей, порт публикации
transcode-профиля — с rtspAddress. Любое расхождение ломает сервис молча:
конфиг валиден, а видео не идёт. Здесь эти связи зафиксированы.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
CADDYFILE = ROOT / "Caddyfile"
MEDIAMTX = ROOT / "mediamtx" / "mediamtx.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def mediamtx() -> dict:
    return yaml.safe_load(MEDIAMTX.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def caddyfile() -> str:
    return CADDYFILE.read_text(encoding="utf-8")


# ─── Периметр ────────────────────────────────────────────────────────────────
def test_only_three_ports_are_published(compose: dict) -> None:
    """Наружу — только 80, 443 и 8189/udp. Всё остальное внутри docker."""
    published: set[str] = set()
    for service in compose["services"].values():
        for mapping in service.get("ports", []):
            host_part = str(mapping).split(":")[0]
            proto = "/udp" if str(mapping).endswith("/udp") else ""
            published.add(host_part + proto)

    assert published == {"80", "443", "443/udp", "8189/udp"}


def test_storage_services_have_no_published_ports(compose: dict) -> None:
    for name in ("postgres", "redis", "api", "worker"):
        assert not compose["services"][name].get("ports"), f"{name} не должен смотреть наружу"


def test_data_network_has_no_internet_access(compose: dict) -> None:
    """Postgres и Redis не должны иметь исходящего доступа в интернет."""
    assert compose["networks"]["data"]["internal"] is True


def test_caddy_is_not_on_core_network(compose: dict) -> None:
    """Скомпрометированный Caddy не должен доставать до Control API MediaMTX."""
    assert "core" not in compose["services"]["caddy"]["networks"]


def test_mediamtx_control_api_is_not_published(compose: dict) -> None:
    ports = [str(p) for p in compose["services"]["mediamtx"].get("ports", [])]
    assert not any("9997" in p or "9998" in p or "8888" in p or "8889" in p for p in ports)


# ─── Подсети: compose ↔ права MediaMTX ───────────────────────────────────────
def _subnet(compose: dict, network: str) -> str:
    return compose["networks"][network]["ipam"]["config"][0]["subnet"]


def test_mediamtx_permissions_match_compose_subnets(compose: dict, mediamtx: dict) -> None:
    core, edge = _subnet(compose, "core"), _subnet(compose, "edge")
    users = mediamtx["authInternalUsers"]

    privileged = next(u for u in users if any(p["action"] == "api" for p in u["permissions"]))
    read_only = next(u for u in users if all(p["action"] != "api" for p in u["permissions"]))

    assert core in privileged["ips"], "Control API должен быть доступен из сети core"
    assert edge not in privileged["ips"], "сеть edge не должна иметь доступ к Control API"
    assert edge in read_only["ips"], "Caddy из сети edge должен иметь право читать потоки"


def test_only_edge_and_core_can_reach_mediamtx(compose: dict, mediamtx: dict) -> None:
    allowed = {ip for user in mediamtx["authInternalUsers"] for ip in user["ips"]}
    data_subnet = _subnet(compose, "data")
    assert data_subnet not in allowed


# ─── Входящие протоколы ──────────────────────────────────────────────────────
def test_incoming_protocols_are_disabled(mediamtx: dict) -> None:
    """Сервис только тянет потоки и никогда ничего не принимает снаружи."""
    assert mediamtx["rtmp"] is False
    assert mediamtx["srt"] is False


def test_rtsp_listens_on_loopback_only(mediamtx: dict) -> None:
    """RTSP нужен только локальному ffmpeg профиля transcode."""
    assert mediamtx["rtspAddress"].startswith("127.0.0.1:")
    assert mediamtx["rtspTransports"] == ["tcp"]


def test_transcode_publishes_to_the_address_mediamtx_listens_on(mediamtx: dict) -> None:
    from app.media.paths import build_path_conf
    from app.models import Camera, StreamProfile

    camera = Camera(
        name="Тест",
        mtx_path="abcdefgh12345678abcdefgh",
        profile=StreamProfile.transcode,
        on_demand=True,
        audio_enabled=True,
    )
    command = build_path_conf(camera, "rtsp://203.0.113.5/s")["runOnDemand"]
    assert command.endswith(f"rtsp://{mediamtx['rtspAddress']}/{camera.mtx_path}")


def test_webrtc_advertises_public_host_not_container_ip(mediamtx: dict, compose: dict) -> None:
    """В контейнере адреса интерфейсов бесполезны для браузера."""
    assert mediamtx["webrtcIPsFromInterfaces"] is False
    env = compose["services"]["mediamtx"]["environment"]
    assert "MTX_WEBRTCADDITIONALHOSTS" in env


def test_webrtc_udp_port_matches_published_port(mediamtx: dict, compose: dict) -> None:
    port = mediamtx["webrtcLocalUDPAddress"].lstrip(":")
    published = [str(p) for p in compose["services"]["mediamtx"]["ports"]]
    assert any(p.startswith(f"{port}:{port}") for p in published)


# ─── Caddyfile ↔ имена путей ─────────────────────────────────────────────────
def test_caddy_media_matchers_accept_generated_path_names(caddyfile: str) -> None:
    from app.media.paths import new_mtx_path

    patterns = re.findall(r"path_regexp \^(/(?:whep|hls)/[^\s]+)\$", caddyfile)
    assert patterns, "в Caddyfile не найдены матчеры медиа-путей"

    name = new_mtx_path()
    samples = {
        f"/whep/{name}/whep",
        f"/whep/{name}/whep/session-abc",
        f"/hls/{name}/index.m3u8",
    }
    for sample in samples:
        assert any(re.fullmatch(p, sample) for p in patterns), f"не проходит матчер: {sample}"


def test_caddy_blocks_service_endpoints(caddyfile: str) -> None:
    """Метрики и readyz не должны быть доступны из интернета."""
    assert re.search(r"handle /metrics \{\s*respond 404", caddyfile)
    assert re.search(r"handle /readyz \{\s*respond 404", caddyfile)


def test_caddy_asks_authz_for_every_media_prefix(caddyfile: str) -> None:
    assert caddyfile.count("forward_auth api:8000") == 3
    assert caddyfile.count("uri /internal/authz") == 3


def test_caddy_falls_through_to_404_on_other_media_paths(caddyfile: str) -> None:
    """Служебные страницы MediaMTX не должны быть доступны наружу."""
    assert re.search(r"handle /whep/\* \{\s*respond 404", caddyfile)
    assert re.search(r"handle /hls/\* \{\s*respond 404", caddyfile)
