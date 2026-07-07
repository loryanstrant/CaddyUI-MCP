"""Async HTTP client for the CaddyUI REST API (``/api/v1``).

CaddyUI (https://github.com/X4Applegate/caddyui) is a Go web app that manages a Caddy
reverse proxy. Its own SQLite database is the source of truth; it pushes the generated
config to Caddy's admin API. This client talks to CaddyUI's stable, versioned REST API
under ``/api/v1`` using an API token (Bearer auth). See ``DECISIONS.md`` for the surface.

Auth: a single header ``Authorization: Bearer <token>``. Tokens are minted in the CaddyUI
UI at ``/api-tokens`` with a scope of ``full`` / ``read_only`` / ``proxy_write``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


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

    A single ``httpx.AsyncClient`` (with the Bearer header baked in) is shared across all
    tool calls and created lazily on first use.
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

    def _http(self) -> httpx.AsyncClient:
        """Build a fresh async client per request.

        A new client each call avoids binding httpx's connection pool to one event loop:
        the pre-flight check (``asyncio.run``) and the MCP server run on *different* loops,
        and a cached client would fail the second call with "Event loop is closed".
        """
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return httpx.AsyncClient(base_url=self._base_url, headers=headers, timeout=30.0)

    async def close(self) -> None:
        """No-op: clients are created and closed per request. Kept for API symmetry/tests."""

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Send a request and return parsed JSON (or ``None`` for empty/204), raising on error.

        The response body is fully read before the client context exits (non-streaming
        request), so ``resp`` remains usable afterwards.
        """
        logger.debug("CaddyUI %s %s", method, path)
        try:
            async with self._http() as client:
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

    # --------------------------------------------------------------- proxy hosts

    async def list_proxy_hosts(self) -> Any:
        return await self._request("GET", "/api/v1/proxy-hosts")

    async def get_proxy_host(self, host_id: int) -> Any:
        return await self._request("GET", f"/api/v1/proxy-hosts/{host_id}")

    async def create_proxy_host(self, config: dict[str, Any]) -> Any:
        return await self._request("POST", "/api/v1/proxy-hosts", json=config)

    async def update_proxy_host(self, host_id: int, config: dict[str, Any]) -> Any:
        return await self._request("PUT", f"/api/v1/proxy-hosts/{host_id}", json=config)

    async def delete_proxy_host(self, host_id: int) -> Any:
        return await self._request("DELETE", f"/api/v1/proxy-hosts/{host_id}")

    async def toggle_proxy_host(self, host_id: int) -> Any:
        return await self._request("POST", f"/api/v1/proxy-hosts/{host_id}/toggle")

    async def set_proxy_host_maintenance(self, host_id: int) -> Any:
        return await self._request("POST", f"/api/v1/proxy-hosts/{host_id}/maintenance")

    # ---------------------------------------------------------- redirection hosts

    async def list_redirection_hosts(self) -> Any:
        return await self._request("GET", "/api/v1/redirection-hosts")

    async def get_redirection_host(self, host_id: int) -> Any:
        return await self._request("GET", f"/api/v1/redirection-hosts/{host_id}")

    async def create_redirection_host(self, config: dict[str, Any]) -> Any:
        return await self._request("POST", "/api/v1/redirection-hosts", json=config)

    async def update_redirection_host(self, host_id: int, config: dict[str, Any]) -> Any:
        return await self._request("PUT", f"/api/v1/redirection-hosts/{host_id}", json=config)

    async def delete_redirection_host(self, host_id: int) -> Any:
        return await self._request("DELETE", f"/api/v1/redirection-hosts/{host_id}")

    async def toggle_redirection_host(self, host_id: int) -> Any:
        return await self._request("POST", f"/api/v1/redirection-hosts/{host_id}/toggle")

    # ------------------------------------------------------------------ raw routes

    async def list_raw_routes(self) -> Any:
        return await self._request("GET", "/api/v1/raw-routes")

    async def get_raw_route(self, route_id: int) -> Any:
        return await self._request("GET", f"/api/v1/raw-routes/{route_id}")

    async def create_raw_route(self, config: dict[str, Any]) -> Any:
        return await self._request("POST", "/api/v1/raw-routes", json=config)

    async def update_raw_route(self, route_id: int, config: dict[str, Any]) -> Any:
        return await self._request("PUT", f"/api/v1/raw-routes/{route_id}", json=config)

    async def delete_raw_route(self, route_id: int) -> Any:
        return await self._request("DELETE", f"/api/v1/raw-routes/{route_id}")

    async def toggle_raw_route(self, route_id: int) -> Any:
        return await self._request("POST", f"/api/v1/raw-routes/{route_id}/toggle")

    # ---------------------------------------------------------------- certificates

    async def list_certificates(self) -> Any:
        return await self._request("GET", "/api/v1/certificates")

    async def get_certificate(self, cert_id: int) -> Any:
        return await self._request("GET", f"/api/v1/certificates/{cert_id}")

    async def create_certificate(self, config: dict[str, Any]) -> Any:
        return await self._request("POST", "/api/v1/certificates", json=config)

    async def update_certificate(self, cert_id: int, config: dict[str, Any]) -> Any:
        return await self._request("PUT", f"/api/v1/certificates/{cert_id}", json=config)

    async def delete_certificate(self, cert_id: int) -> Any:
        return await self._request("DELETE", f"/api/v1/certificates/{cert_id}")

    # ----------------------------------------------------- status (read-only /api/*)

    async def caddy_version(self) -> Any:
        """Caddy version as reported by CaddyUI. Doubles as a cheap liveness/auth probe."""
        return await self._request("GET", "/api/caddy-version")

    async def system_stats(self) -> Any:
        return await self._request("GET", "/api/system-stats")

    async def upstream_health(self) -> Any:
        return await self._request("GET", "/api/upstream-health")

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
