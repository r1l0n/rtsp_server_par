"""HTTP к камере: один клиент на процесс, Digest и разбор ошибок.

Сделано по образцу `media/diagnose._probe_endpoint`, и по тем же причинам:

* `trust_env=False` — прокси из окружения не должен уводить запрос к камере;
* `follow_redirects=False` — редирект это способ увести запрос с проверенного
  адреса куда угодно, то есть обойти SSRF-проверку;
* ловим `Exception` целиком: `httpx.InvalidURL` наследуется от `Exception`,
  а не от `httpx.HTTPError`.

Экземпляры `DigestAuth` кэшируются по камере намеренно. Digest без кэша — это
401 и повторный запрос на КАЖДУЮ команду, а пока зритель держит стрелку,
команды идут пару раз в секунду: камера получала бы вдвое больше запросов
впустую. `httpx` хранит последний challenge внутри объекта и переиспользует
его, поэтому достаточно не создавать объект заново.
"""

from __future__ import annotations

import httpx

from ..config import get_settings
from ..logging_setup import get_logger
from ..media.ssrf import strip_credentials
from .base import PtzAuthError, PtzError, Target

log = get_logger("ptz")

_client: httpx.AsyncClient | None = None
_auth_cache: dict[tuple[str, int, str], httpx.DigestAuth] = {}


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(get_settings().ptz_http_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            # Сертификат IP-камеры почти всегда самоподписанный и выписан на
            # адрес, которого нет ни в одном списке доверия. Проверять его не
            # по чему; выбор здесь — между «без проверки цепочки» и «PTZ по
            # HTTPS не работает никогда». Пароль при этом открытым не идёт:
            # везде Digest или ONVIF-дайджест. См. docs/security.md.
            verify=False,  # noqa: S501
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
    _auth_cache.clear()


def _auth(target: Target) -> httpx.DigestAuth:
    key = (target.host, target.port, target.username)
    auth = _auth_cache.get(key)
    if auth is None:
        auth = httpx.DigestAuth(target.username, target.password)
        _auth_cache[key] = auth
    return auth


def forget_auth(target: Target) -> None:
    """Сбрасывает кэш Digest — после смены пароля камеры."""
    _auth_cache.pop((target.host, target.port, target.username), None)


async def request(
    target: Target,
    method: str,
    path: str,
    *,
    content: str | None = None,
    headers: dict[str, str] | None = None,
    authenticate: bool = True,
) -> httpx.Response:
    """Один запрос к камере. Любую беду переводит в PtzError."""
    url = target.url(path)
    try:
        response = await get_client().request(
            method,
            url,
            content=content.encode("utf-8") if content is not None else None,
            headers=headers,
            auth=_auth(target) if authenticate else httpx.USE_CLIENT_DEFAULT,
        )
    except Exception as exc:
        # Текст ошибки может содержать URL с учётными данными.
        detail = strip_credentials(f"{type(exc).__name__}: {exc}")[:200]
        log.info("ptz_request_failed", host=target.host, port=target.port, error=detail)
        raise PtzError(f"камера не отвечает: {detail}") from exc

    if response.status_code in (401, 403):
        raise PtzAuthError(
            "камера не приняла логин или пароль для управления. Проверьте "
            "отдельные учётные данные PTZ в настройках камеры"
        )
    return response


def ensure_digest(response: httpx.Response, target: Target) -> None:
    """Запрещает Basic поверх открытого HTTP.

    Basic передаёт пароль от камеры в сеть практически открытым текстом
    (base64 — это не шифрование). Мы этого не делаем никогда: цена ошибки —
    утёкший пароль от устройства, а не неудобство. Камерам, умеющим только
    Basic, остаётся включить у себя HTTPS.
    """
    if target.tls:
        return
    challenge = response.headers.get("www-authenticate", "").lower()
    if challenge.startswith("basic"):
        raise PtzAuthError(
            "камера принимает только базовую аутентификацию — при ней пароль "
            "уходит по сети почти открытым. Включите HTTPS в веб-интерфейсе "
            "камеры и отметьте «подключаться по HTTPS» в её настройках"
        )
