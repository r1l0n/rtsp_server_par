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
INSTALL_SH = ROOT / "install.sh"
DOCKERFILE = ROOT / "backend" / "Dockerfile"


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
def test_only_expected_ports_are_published(compose: dict) -> None:
    """Наружу — только 80, 443 и 8189. Всё остальное внутри docker.

    8189 опубликован и по UDP, и по TCP: UDP — основной путь WebRTC-медиа,
    TCP — ICE-кандидат для сетей, где исходящий UDP закрыт. Без TCP там
    WebRTC не поднимается вообще, и зритель молча уезжает на HLS.
    """
    published: set[str] = set()
    for service in compose["services"].values():
        for mapping in service.get("ports", []):
            host_part = str(mapping).split(":")[0]
            proto = "/udp" if str(mapping).endswith("/udp") else "/tcp"
            published.add(host_part + proto)

    assert published == {"80/tcp", "443/tcp", "443/udp", "8189/udp", "8189/tcp"}


def test_redis_command_uses_space_separated_options(compose: dict) -> None:
    """redis-server не понимает форму `--опция=значение`.

    Всё, что идёт после `--`, он считает именем директивы, поэтому
    `--appendonly=yes` превращается в директиву «appendonly=yes», которой не
    существует, и сервер падает с «Bad directive». Контейнер уходит в цикл
    перезапусков, а compose сообщает лишь «container is unhealthy» — по этому
    сообщению причину не найти, поэтому проверяем форму записи здесь.

    Команда задана строкой, а не списком: в списке YAML `- yes` стало бы
    булевым true, и redis получил бы `--appendonly true`.
    """
    command = compose["services"]["redis"]["command"]

    assert isinstance(command, str), "команда redis должна быть строкой (иначе YAML съест yes)"
    assert command.startswith("redis-server ")
    assert "=" not in command, "redis-server не поддерживает --опция=значение"
    assert "--appendonly yes" in command
    assert "--maxmemory-policy noeviction" in command


def test_key_file_owner_matches_container_user() -> None:
    """UID владельца ключа обязан совпадать с UID процесса в контейнере.

    Docker монтирует файл секрета как есть, вместе с владельцем и правами
    хоста (uid/gid/mode в секции secrets работают только в swarm). Если
    установщик отдаст ключ не тому uid, приложение упадёт на старте с
    «Permission denied» — а по логу compose это выглядит просто как
    «container is unhealthy».
    """
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"useradd[^\n]*--uid\s+(\d+)", dockerfile)
    assert match, "в Dockerfile не найден useradd --uid"
    container_uid = match.group(1)

    install = INSTALL_SH.read_text(encoding="utf-8")
    declared = re.search(r"^APP_UID=(\d+)", install, re.MULTILINE)
    assert declared, "в install.sh не объявлен APP_UID"
    assert declared.group(1) == container_uid, (
        f"install.sh отдаёт ключ uid={declared.group(1)}, "
        f"а контейнер работает под uid={container_uid}"
    )

    assert 'chown "$APP_UID:$APP_UID" secrets/app_key' in install
    assert "chmod 400 secrets/app_key" in install


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


def test_webrtc_ports_match_published_ports(mediamtx: dict, compose: dict) -> None:
    """И UDP, и TCP-кандидат должны быть выставлены наружу на своих портах."""
    published = [str(p) for p in compose["services"]["mediamtx"]["ports"]]

    udp = mediamtx["webrtcLocalUDPAddress"].lstrip(":")
    assert f"{udp}:{udp}/udp" in published

    tcp = mediamtx["webrtcLocalTCPAddress"].lstrip(":")
    assert tcp, "ICE поверх TCP обязателен: без него WebRTC не работает там, где закрыт UDP"
    assert f"{tcp}:{tcp}/tcp" in published


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


def test_tls_issuer_is_configurable(caddyfile: str, compose: dict) -> None:
    """Один токен покрывает и Let's Encrypt, и собственный CA.

    `tls <почта>` — сертификат от Let's Encrypt (есть домен или имя sslip.io);
    `tls internal` — свой CA (когда наружу торчит только IP-адрес).
    Значение приходит из .env, поэтому режим меняется без правки конфигов.
    """
    assert "tls {$TLS_ISSUER}" in caddyfile
    # Глобальной директивы email быть не должно: в режиме internal почты нет,
    # и Caddy упал бы на пустом аргументе.
    assert "email {$" not in caddyfile

    env = compose["services"]["caddy"]["environment"]
    assert "TLS_ISSUER" in env
    assert "DOMAIN" in env


def test_default_sni_is_set(caddyfile: str) -> None:
    """Без default_sni режим «доступ по IP» не работает вообще.

    Браузер, открывая https://<IP>, не шлёт SNI — RFC 6066 запрещает
    IP-литералы в этом расширении. Без SNI Caddy ищет сертификат по локальному
    адресу соединения, а в контейнере это адрес docker-сети, а не публичный IP.
    Сертификат не находится, рукопожатие рвётся, браузер показывает
    ERR_SSL_PROTOCOL_ERROR — и в access-логе Caddy при этом пусто, так что
    причину по логам не найти.
    """
    assert "default_sni {$DOMAIN}" in caddyfile


def test_acme_ca_defaults_to_production(caddyfile: str, compose: dict) -> None:
    """Тестовый CA включается через .env, но по умолчанию — боевой.

    Значение по умолчанию задаётся в compose, а не в Caddyfile: так .env,
    написанные до появления этой переменной, продолжают работать, и Caddy
    никогда не получает пустой аргумент.
    """
    assert "acme_ca {$ACME_CA}" in caddyfile

    acme_ca = compose["services"]["caddy"]["environment"]["ACME_CA"]
    assert acme_ca.startswith("${ACME_CA:-")
    assert "acme-v02.api.letsencrypt.org" in acme_ca
    assert "staging" not in acme_ca


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
