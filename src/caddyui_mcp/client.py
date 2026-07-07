"""Async HTTP client for the CaddyUI REST API (``/api/v1``).

CaddyUI (https://github.com/X4Applegate/caddyui) is a Go web app that manages a Caddy
reverse proxy. Its own SQLite database is the source of truth; it pushes the generated
config to Caddy's admin API. This client talks to CaddyUI's stable, versioned REST API
under ``/api/v1`` using an API token (Bearer auth). See ``DECISIONS.md`` for the surface.

Auth: a single header ``Authorization: Bearer <token>``. Tokens are minted in the CaddyUI
UI at ``/api-tokens`` with a scope of ``full`` / ``read_only`` / ``proxy_write``.

**Multi-server:** CaddyUI can centrally manage several Caddy instances. Every ``/api/v1``
list/CRUD endpoint is scoped to the "current server", chosen by the ``caddyui_server`` cookie
(the UI's server picker POSTs ``/servers/{id}/select``). A tokenized request with no cookie
defaults to **server 1**. This client sends ``caddyui_server=<server_id>`` when a ``server_id``
is supplied, so callers can target any managed Caddy server.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

SERVER_COOKIE = "caddyui_server"


class CaddyUISettings(BaseSettings):
    """Configuration loaded from environment variables (``CADDYUI_URL``, ``CADDYUI_TOKEN``)."""

    caddyui_url: str = "https://caddyui.strant.casa"
    caddyui_token: str = ""


class CaddyUIError(Exception):
    """An error returned by the CaddyUI API, or a transport failure.

    Carries the HTTP ``status_code`` and raw response ``body`` when available so tools
    can surface ``401`` (bad token), ``403`` (scope), ``404`` (missing id), ``422``
    (validation) clearly.
    """

    def __init__(
        self, message: str, status_code: int | None = None, body: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class CaddyUIClient:
    """Thin async wrapper over CaddyUI's ``/api/v1`` REST API.

    A fresh ``httpx.AsyncClient`` is created per request (avoids binding httpx's connection
    pool to one event loop — the pre-flight check and the MCP server run on different loops).
    """

    def __init__(self, settings: CaddyUISettings) -> None:
        self._base_url = settings.caddyui_url.rstrip("/")
        self._token = settings.caddyui_token
        if not self._token:
            logger.warning("CADDYUI_TOKEN is empty; API calls will fail with 401 Unauthorized.")
        logger.info(
            "CaddyUI client: base_url=%s token=%s",
            self._base_url,
            "set" if self._token else "MISSING",
        )

    # ------------------------------------------------------------------ transport

    def _http(self, cookies: dict[str, str] | None = None) -> httpx.AsyncClient:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return httpx.AsyncClient(
            base_url=self._base_url, headers=headers, timeout=30.0, cookies=cookies
        )

    async def close(self) -> None:
        """No-op: clients are created and closed per request. Kept for API symmetry/tests."""

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        server_id: int | None = None,
    ) -> Any:
        """Send a request and return parsed JSON (or ``None`` for empty/204), raising on error.

        When ``server_id`` is given, the ``caddyui_server`` cookie scopes the call to that
        managed Caddy server. The response body is fully read before the client context exits
        (non-streaming request), so ``resp`` remains usable afterwards.
        """
        cookies = {SERVER_COOKIE: str(server_id)} if server_id is not None else None
        logger.debug("CaddyUI %s %s (server_id=%s)", method, path, server_id)
        try:
            async with self._http(cookies) as client:
                resp = await client.request(method, path, json=json, params=params)
        except httpx.HTTPError as e:
            raise CaddyUIError(f"{method} {path} failed: {e}") from e

        if resp.status_code >= 400:
            body = resp.text
            raise CaddyUIError(
                f"{method} {path} -> HTTP {resp.status_code}: {body[:500]}",
                status_code=resp.status_code,
                body=body,
            )

        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ------------------------------------------------- server discovery (probe)

    async def discover_servers(self, probe_max: int = 24) -> list[dict[str, Any]]:
        """Discover managed Caddy servers by probing ``caddyui_server`` ids 1..probe_max.

        CaddyUI exposes no JSON endpoint listing its Caddy servers, so we scope
        ``GET /api/v1/proxy-hosts`` to each id and report those that hold proxy hosts, with a
        few sample domains (which make each server recognisable). Servers whose only content
        is redirects/raw-routes won't show here — target them directly by id if you know it.
        """

        async def probe(sid: int) -> dict[str, Any] | None:
            try:
                hosts = await self.list_proxy_hosts(server_id=sid)
            except CaddyUIError:
                return None
            if not isinstance(hosts, list) or not hosts:
                return None
            return {
                "server_id": sid,
                "proxy_host_count": len(hosts),
                "sample_domains": [h.get("domains") for h in hosts[:6] if isinstance(h, dict)],
            }

        results = await asyncio.gather(*(probe(i) for i in range(1, probe_max + 1)))
        return [r for r in results if r is not None]

    # --------------------------------------------------------------- proxy hosts

    async def list_proxy_hosts(self, server_id: int | None = None) -> Any:
        return await self._request("GET", "/api/v1/proxy-hosts", server_id=server_id)

    async def get_proxy_host(self, host_id: int, server_id: int | None = None) -> Any:
        return await self._request("GET", f"/api/v1/proxy-hosts/{host_id}", server_id=server_id)

    async def create_proxy_host(self, config: dict[str, Any], server_id: int | None = None) -> Any:
        return await self._request("POST", "/api/v1/proxy-hosts", json=config, server_id=server_id)

    async def update_proxy_host(
        self, host_id: int, config: dict[str, Any], server_id: int | None = None
    ) -> Any:
        return await self._request(
            "PUT", f"/api/v1/proxy-hosts/{host_id}", json=config, server_id=server_id
        )

    async def delete_proxy_host(self, host_id: int, server_id: int | None = None) -> Any:
        return await self._request("DELETE", f"/api/v1/proxy-hosts/{host_id}", server_id=server_id)

    async def toggle_proxy_host(self, host_id: int, server_id: int | None = None) -> Any:
        return await self._request(
            "POST", f"/api/v1/proxy-hosts/{host_id}/toggle", server_id=server_id
        )

    async def set_proxy_host_maintenance(self, host_id: int, server_id: int | None = None) -> Any:
        return await self._request(
            "POST", f"/api/v1/proxy-hosts/{host_id}/maintenance", server_id=server_id
        )

    # ---------------------------------------------------------- redirection hosts

    async def list_redirection_hosts(self, server_id: int | None = None) -> Any:
        return await self._request("GET", "/api/v1/redirection-hosts", server_id=server_id)

    async def get_redirection_host(self, host_id: int, server_id: int | None = None) -> Any:
        return await self._request(
            "GET", f"/api/v1/redirection-hosts/{host_id}", server_id=server_id
        )

    async def create_redirection_host(
        self, config: dict[str, Any], server_id: int | None = None
    ) -> Any:
        return await self._request(
            "POST", "/api/v1/redirection-hosts", json=config, server_id=server_id
        )

    async def update_redirection_host(
        self, host_id: int, config: dict[str, Any], server_id: int | None = None
    ) -> Any:
        return await self._request(
            "PUT", f"/api/v1/redirection-hosts/{host_id}", json=config, server_id=server_id
        )

    async def delete_redirection_host(self, host_id: int, server_id: int | None = None) -> Any:
        return await self._request(
            "DELETE", f"/api/v1/redirection-hosts/{host_id}", server_id=server_id
        )

    async def toggle_redirection_host(self, host_id: int, server_id: int | None = None) -> Any:
        return await self._request(
            "POST", f"/api/v1/redirection-hosts/{host_id}/toggle", server_id=server_id
        )

    # ------------------------------------------------------------------ raw routes

    async def list_raw_routes(self, server_id: int | None = None) -> Any:
        return await self._request("GET", "/api/v1/raw-routes", server_id=server_id)

    async def get_raw_route(self, route_id: int, server_id: int | None = None) -> Any:
        return await self._request("GET", f"/api/v1/raw-routes/{route_id}", server_id=server_id)

    async def create_raw_route(self, config: dict[str, Any], server_id: int | None = None) -> Any:
        return await self._request("POST", "/api/v1/raw-routes", json=config, server_id=server_id)

    async def update_raw_route(
        self, route_id: int, config: dict[str, Any], server_id: int | None = None
    ) -> Any:
        return await self._request(
            "PUT", f"/api/v1/raw-routes/{route_id}", json=config, server_id=server_id
        )

    async def delete_raw_route(self, route_id: int, server_id: int | None = None) -> Any:
        return await self._request("DELETE", f"/api/v1/raw-routes/{route_id}", server_id=server_id)

    async def toggle_raw_route(self, route_id: int, server_id: int | None = None) -> Any:
        return await self._request(
            "POST", f"/api/v1/raw-routes/{route_id}/toggle", server_id=server_id
        )

    # ---------------------------------------------------------------- certificates

    async def list_certificates(self, server_id: int | None = None) -> Any:
        return await self._request("GET", "/api/v1/certificates", server_id=server_id)

    async def get_certificate(self, cert_id: int, server_id: int | None = None) -> Any:
        return await self._request("GET", f"/api/v1/certificates/{cert_id}", server_id=server_id)

    async def create_certificate(self, config: dict[str, Any], server_id: int | None = None) -> Any:
        return await self._request("POST", "/api/v1/certificates", json=config, server_id=server_id)

    async def update_certificate(
        self, cert_id: int, config: dict[str, Any], server_id: int | None = None
    ) -> Any:
        return await self._request(
            "PUT", f"/api/v1/certificates/{cert_id}", json=config, server_id=server_id
        )

    async def delete_certificate(self, cert_id: int, server_id: int | None = None) -> Any:
        return await self._request("DELETE", f"/api/v1/certificates/{cert_id}", server_id=server_id)

    # ----------------------------------------------------- status (read-only /api/*)

    async def caddy_version(self, server_id: int | None = None) -> Any:
        """Caddy version as reported by CaddyUI for a server (cheap liveness/auth probe)."""
        return await self._request("GET", "/api/caddy-version", server_id=server_id)

    async def system_stats(self) -> Any:
        return await self._request("GET", "/api/system-stats")

    async def upstream_health(self, server_id: int | None = None) -> Any:
        return await self._request("GET", "/api/upstream-health", server_id=server_id)

    async def search(self, query: str) -> Any:
        return await self._request("GET", "/api/search", params={"q": query})


_client: CaddyUIClient | None = None
_settings_override: CaddyUISettings | None = None


def configure(settings: CaddyUISettings) -> None:
    """Set a custom settings override (e.g. for tests). Resets any existing client."""
    global _settings_override, _client
    _settings_override = settings
    _client = None
    logger.info("Client configured with override URL=%s", settings.caddyui_url)


def get_client() -> CaddyUIClient:
    """Return the shared client, creating it on first access."""
    global _client
    if _client is None:
        # Fields are populated from the environment by pydantic-settings.
        settings = _settings_override or CaddyUISettings()
        _client = CaddyUIClient(settings)
    return _client


def reset() -> None:
    """Clear the shared client (for test teardown)."""
    global _client, _settings_override
    _client = None
    _settings_override = None
    logger.info("Client reset")
