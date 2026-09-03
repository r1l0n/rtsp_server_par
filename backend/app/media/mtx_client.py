"""Клиент Control API MediaMTX (v3).

Эндпоинты сверены с api/openapi.yaml тега v1.20.1.
Доступ к API открыт только из docker-сети `core` (см. mediamtx/mediamtx.yml).
"""

from __future__ import annotations

from typing import Any

import httpx

from ..logging_setup import get_logger

log = get_logger("mtx")

DEFAULT_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
ITEMS_PER_PAGE = 500


class MediaMTXError(RuntimeError):
    """Ошибка Control API."""


class PathNotFound(MediaMTXError):
    pass


class PathAlreadyExists(MediaMTXError):
    pass


class MediaMTXClient:
    def __init__(self, base_url: str, timeout: httpx.Timeout = DEFAULT_TIMEOUT) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- низкий уровень ------------------------------------------------------
    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise MediaMTXError(f"{method} {url}: {exc}") from exc

        if response.status_code == 404:
            raise PathNotFound(url)
        if response.status_code >= 400:
            detail = _error_detail(response)
            if "already exists" in detail.lower():
                raise PathAlreadyExists(detail)
            raise MediaMTXError(f"{method} {url} -> {response.status_code}: {detail}")

        if not response.content:
            return None
        return response.json()

    async def _list_all(self, url: str) -> list[dict[str, Any]]:
        """Собирает все страницы списочного эндпоинта."""
        items: list[dict[str, Any]] = []
        page = 0
        while True:
            data = await self._request(
                "GET", url, params={"page": page, "itemsPerPage": ITEMS_PER_PAGE}
            )
            items.extend(data.get("items") or [])
            page += 1
            if page >= int(data.get("pageCount") or 1):
                return items

    # --- конфигурация путей --------------------------------------------------
    async def list_config_paths(self) -> list[dict[str, Any]]:
        return await self._list_all("/v3/config/paths/list")

    async def get_config_path(self, name: str) -> dict[str, Any]:
        return await self._request("GET", f"/v3/config/paths/get/{name}")

    async def add_path(self, name: str, conf: dict[str, Any]) -> None:
        await self._request("POST", f"/v3/config/paths/add/{name}", json=conf)

    async def patch_path(self, name: str, conf: dict[str, Any]) -> None:
        await self._request("PATCH", f"/v3/config/paths/patch/{name}", json=conf)

    async def replace_path(self, name: str, conf: dict[str, Any]) -> None:
        await self._request("POST", f"/v3/config/paths/replace/{name}", json=conf)

    async def delete_path(self, name: str) -> None:
        await self._request("DELETE", f"/v3/config/paths/delete/{name}")

    async def upsert_path(self, name: str, conf: dict[str, Any]) -> None:
        """Создать путь или полностью заменить его конфигурацию.

        replace вместо patch — чтобы поля, убранные из conf, действительно
        сбрасывались в значения по умолчанию, а не оставались от прошлой версии.
        """
        try:
            await self.replace_path(name, conf)
        except PathNotFound:
            await self.add_path(name, conf)

    # --- состояние путей -----------------------------------------------------
    async def list_active_paths(self) -> list[dict[str, Any]]:
        return await self._list_all("/v3/paths/list")

    async def get_active_path(self, name: str) -> dict[str, Any]:
        return await self._request("GET", f"/v3/paths/get/{name}")

    # --- сессии WebRTC -------------------------------------------------------
    async def list_webrtc_sessions(self) -> list[dict[str, Any]]:
        return await self._list_all("/v3/webrtcsessions/list")

    async def kick_webrtc_session(self, session_id: str) -> None:
        await self._request("POST", f"/v3/webrtcsessions/kick/{session_id}")


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        return str(payload.get("error") or payload)[:200]
    return str(payload)[:200]


# --- синглтон для процесса ---------------------------------------------------
_client: MediaMTXClient | None = None


def get_mtx() -> MediaMTXClient:
    global _client
    if _client is None:
        from ..config import get_settings

        _client = MediaMTXClient(get_settings().mtx_api_url)
    return _client


async def close_mtx() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
