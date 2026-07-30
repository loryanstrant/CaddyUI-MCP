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

The one exception is ``GET /api/v1/servers`` (CaddyUI 2.20.2+, added at this project's request —
upstream issue #18), which is unscoped and lists the whole fleet. Older instances have no such
endpoint, so :mod:`caddyui_mcp._servers_html` scrapes the HTML page as a deprecated fallback.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from pydantic_settings import BaseSettings

from caddyui_mcp._servers_html import _detect_markup, parse_servers_html

logger = logging.getLogger(__name__)

SERVER_COOKIE = "caddyui_server"

# ------------------------------------------------------------------ server listing
#
# CaddyUI 2.20.2+ exposes ``GET /api/v1/servers``; older instances only render the server list
# as HTML, which :mod:`caddyui_mcp._servers_html` scrapes. Both sources are normalised into one
# shape so ``discover_servers`` never has to know which one it got.

#: The keys every server entry carries, whichever source produced it. Asserted by tests against
#: both paths — if you add a key here, both sources must emit it.
SERVER_FIELDS = (
    "name",
    "admin_url",
    "status",
    "type",
    "caddy_version",
    "tags",
    "last_contact_at",
)


def normalise_api_servers(payload: Any) -> dict[int, dict[str, Any]]:
    """Normalise ``GET /api/v1/servers`` into ``{id: {…SERVER_FIELDS}}``.

    Pure and I/O-free. Tolerates a non-list payload, non-dict entries and a missing or non-int
    ``id`` — a malformed response degrades to fewer entries, never an exception.
    """
    out: dict[int, dict[str, Any]] = {}
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
            continue
        status = entry.get("status")
        tags = entry.get("tags")
        out[entry["id"]] = {
            "name": entry.get("name") or None,
            "admin_url": entry.get("admin_url") or None,
            # Already lowercase upstream; normalising anyway makes "status is always lowercase"
            # true by construction rather than by upstream's continued goodwill.
            "status": status.lower() if isinstance(status, str) and status else None,
            "type": entry.get("type") or None,
            # Upstream sends "" for unknown; None reads as "unknown" to an LLM, "" reads as
            # "the version is the empty string", and None is what the HTML path emits.
            "caddy_version": entry.get("version") or None,
            "tags": tags if isinstance(tags, list) else [],
            "last_contact_at": entry.get("last_contact_at") or None,
        }
    return out


# ----------------------------------------------------------- certificate usage
#
# CaddyUI computes this internally (v2.20.1 `internal/server/certificate_usage.go`) but exposes
# no endpoint for it. Since `certificate_id` is present on proxy hosts, redirection hosts and
# raw routes alike, the same answer is derivable from the existing /api/v1 lists.

#: Certificate sources a user supplies by hand. ``managed`` certs are ACME-driven — Caddy
#: attaches them by domain coverage rather than by ``certificate_id``, so they are never
#: "unused" in this sense. CaddyUI's own check excludes them for the same reason.
_CUSTOM_CERT_SOURCES = frozenset({"pem", "path"})


def select_unused_certificates(
    certificates: Any,
    proxy_hosts: Any,
    redirection_hosts: Any,
    raw_routes: Any,
) -> dict[str, Any]:
    """Return the certificates that no proxy host, redirection host or raw route references.

    Pure, so it can be tested with plain dicts. Non-list inputs are tolerated (an error string
    or ``None`` from a failed call degrades to "nothing referenced" rather than raising).
    """

    def _referenced(resources: Any) -> set[int]:
        if not isinstance(resources, list):
            return set()
        # certificate_id 0 is CaddyUI's "no certificate", not a reference to cert id 0.
        return {
            r["certificate_id"]
            for r in resources
            if isinstance(r, dict) and isinstance(r.get("certificate_id"), int)
            if r["certificate_id"]
        }

    referenced = _referenced(proxy_hosts) | _referenced(redirection_hosts) | _referenced(raw_routes)
    certs = certificates if isinstance(certificates, list) else []

    unused = [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "domains": c.get("domains"),
            "source": c.get("source"),
        }
        for c in certs
        if isinstance(c, dict)
        and c.get("id")
        and c["id"] not in referenced
        and c.get("source") in _CUSTOM_CERT_SOURCES
    ]
    return {
        "unused": unused,
        "certificates_checked": len(certs),
        "referenced_ids": sorted(referenced),
    }


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
        data: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        server_id: int | None = None,
    ) -> Any:
        """Send a request and return parsed JSON (or ``None`` for empty/204), raising on error.

        When ``server_id`` is given, the ``caddyui_server`` cookie scopes the call to that
        managed Caddy server. The response body is fully read before the client context exits
        (non-streaming request), so ``resp`` remains usable afterwards.

        ``json`` targets the versioned ``/api/v1`` REST API. ``data`` sends a form-encoded body
        instead, which the unversioned ``/api/*`` AJAX helpers require — they read
        ``r.FormValue`` and see nothing at all in a JSON body. The two are mutually exclusive.
        """
        if json is not None and data is not None:
            raise ValueError("pass either json= or data=, not both")
        cookies = {SERVER_COOKIE: str(server_id)} if server_id is not None else None
        logger.debug("CaddyUI %s %s (server_id=%s)", method, path, server_id)
        try:
            async with self._http(cookies) as client:
                resp = await client.request(method, path, json=json, data=data, params=params)
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

    # ------------------------------------------------- server discovery

    async def servers(self) -> Any:
        """``GET /api/v1/servers`` — every Caddy server CaddyUI manages (CaddyUI 2.20.2+).

        Unlike every other ``/api/v1`` endpoint this one is **not** scoped by the
        ``caddyui_server`` cookie; it returns the whole fleet. It therefore takes no
        ``server_id`` deliberately — passing one would be a silent no-op.
        """
        return await self._request("GET", "/api/v1/servers")

    async def registered_servers(self) -> dict[int, dict[str, Any]] | None:
        """Registered Caddy servers keyed by id, in the common :data:`SERVER_FIELDS` shape.

        Prefers ``GET /api/v1/servers`` (CaddyUI 2.20.2+, added at this project's request —
        upstream issue #18). Falls back to scraping the HTML ``/servers`` page **only** on a
        404, which is what an older CaddyUI returns for a route it doesn't have.

        Returns ``None`` for *could not be determined*, which is deliberately distinct from
        ``{}`` for *determined, and empty*: the caller uses that difference to avoid claiming
        every server is orphaned when the listing itself failed.
        """
        try:
            return normalise_api_servers(await self.servers())
        except CaddyUIError as e:
            if e.status_code != 404:
                # A 401/403/5xx is an auth, scope or server problem — not an old CaddyUI.
                # Scraping instead would mask it (and would fail too, differently).
                logger.warning("GET /api/v1/servers failed: %s", e)
                return None
            logger.warning(
                "GET /api/v1/servers -> 404; this CaddyUI predates v2.20.2. Falling back to "
                "scraping the HTML /servers page — deprecated, scheduled for removal (see "
                "DECISIONS.md 2026-07-31). Upgrade CaddyUI to v2.20.2+."
            )
        return await self._scrape_servers()

    async def _scrape_servers(self) -> dict[int, dict[str, Any]] | None:
        """**Deprecated** fallback: parse the HTML ``/servers`` page (CaddyUI < 2.20.2).

        Transport and diagnostics only; parsing lives in :mod:`caddyui_mcp._servers_html`.
        """
        try:
            async with self._http() as client:
                resp = await client.get("/servers")
        except Exception as e:  # discovery must never crash a tool
            logger.warning("Could not fetch /servers page for names: %s", e)
            return None

        # httpx does not follow redirects by default and raise_for_status() ignores 3xx, so a
        # redirect to /login would otherwise sail through as "no servers found".
        if resp.status_code >= 300:
            logger.warning(
                "GET /servers -> HTTP %s. That page is admin-gated, so the token's user must "
                "have the admin role. Upgrading CaddyUI to v2.20.2+ removes the problem — its "
                "GET /api/v1/servers works with any authenticated token.",
                resp.status_code,
            )
            return None

        html = resp.text
        markup = _detect_markup(html)
        servers = parse_servers_html(html)
        if not servers:
            logger.warning(
                "Parsed 0 servers from /servers (markup=%s, %d bytes). Upgrade CaddyUI to "
                "v2.20.2+ and this page stops being used at all.",
                markup,
                len(html),
            )
            return None
        logger.debug("Parsed %d servers from /servers (markup=%s)", len(servers), markup)
        return servers

    async def discover_servers(self, probe_max: int = 24) -> list[dict[str, Any]]:
        """Discover the Caddy servers CaddyUI manages, with names and proxy-host counts.

        Combines :meth:`registered_servers` (id → name/type/status/…) with a probe of
        ``GET /api/v1/proxy-hosts`` per id, which is the only way to get per-server host counts
        and sample domains since that endpoint is scoped by the ``caddyui_server`` cookie.

        Registered servers are always listed. ``probe_max`` bounds the hunt for **orphans** —
        ids that still hold proxy hosts but are no longer registered servers, i.e. leftovers
        from a deleted Caddy server (``orphaned: true``). ``orphaned: None`` means the server
        listing couldn't be determined, so orphan status is unknown.
        """
        # None (couldn't determine) is deliberately distinct from {} (determined, empty):
        # without that distinction a failed listing marks every server orphaned, and the tool
        # instructions tell the model to ignore orphaned entries — confidently wrong output.
        registered = await self.registered_servers()
        known = registered or {}

        async def count(sid: int) -> tuple[int, list[Any]]:
            try:
                hosts = await self.list_proxy_hosts(server_id=sid)
            except CaddyUIError:
                return 0, []
            if not isinstance(hosts, list):
                return 0, []
            return len(hosts), [h.get("domains") for h in hosts[:6] if isinstance(h, dict)]

        # Registered ids are always included, even beyond probe_max.
        ids = sorted(set(known) | set(range(1, probe_max + 1)))
        counts = dict(zip(ids, await asyncio.gather(*(count(i) for i in ids)), strict=True))

        servers: list[dict[str, Any]] = []
        for sid in ids:
            n, domains = counts[sid]
            is_registered = sid in known
            if not is_registered and n == 0:
                continue  # neither a registered server nor holding any hosts
            info = known.get(sid, {})
            # .get() for every key: a partial parse must degrade to nulls, never KeyError.
            entry: dict[str, Any] = {"server_id": sid}
            entry.update({field: info.get(field) for field in SERVER_FIELDS})
            entry["tags"] = info.get("tags") or []
            entry.update(
                {
                    "orphaned": None if registered is None else not is_registered,
                    "proxy_host_count": n,
                    "sample_domains": domains,
                }
            )
            servers.append(entry)
        return servers

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

    async def unused_certificates(self, server_id: int | None = None) -> dict[str, Any]:
        """Find certificates on a server that no host or route references.

        Reproduces CaddyUI's own ``internal/server/certificate_usage.go`` client-side — it has
        no API endpoint, but ``certificate_id`` is exposed on all three resource types, so the
        four lists are enough. Fetched concurrently since they're independent.
        """
        certs, proxies, redirects, routes = await asyncio.gather(
            self.list_certificates(server_id=server_id),
            self.list_proxy_hosts(server_id=server_id),
            self.list_redirection_hosts(server_id=server_id),
            self.list_raw_routes(server_id=server_id),
        )
        return select_unused_certificates(certs, proxies, redirects, routes)

    # ----------------------------------------------------- status (read-only /api/*)

    async def caddy_version(self, server_id: int | None = None) -> Any:
        """Caddy version as reported by CaddyUI for a server (cheap liveness/auth probe)."""
        return await self._request("GET", "/api/caddy-version", server_id=server_id)

    async def caddyui_version(self) -> Any:
        """CaddyUI's own version: ``{current, latest, has_update}``."""
        return await self._request("GET", "/api/version-check")

    async def system_stats(self) -> Any:
        return await self._request("GET", "/api/system-stats")

    async def upstream_health(self, server_id: int | None = None) -> Any:
        return await self._request("GET", "/api/upstream-health", server_id=server_id)

    async def search(self, query: str) -> Any:
        return await self._request("GET", "/api/search", params={"q": query})

    async def managed_certificate_status(self, cert_id: int, server_id: int | None = None) -> Any:
        """Live per-Caddy-server status of a ``managed`` (ACME) certificate.

        Note the path is ``/certificates/…``, not ``/api/certificates/…``. Returns HTTP 400 if
        the certificate's ``source`` is not ``managed``, and 404 on CaddyUI older than v2.17.2.
        """
        return await self._request(
            "GET", f"/certificates/{cert_id}/managed-status", server_id=server_id
        )

    async def proxy_host_deploy_status(self, host_id: int, server_id: int | None = None) -> Any:
        return await self._request(
            "GET", f"/api/proxy-hosts/{host_id}/deploy-status", server_id=server_id
        )

    async def raw_route_deploy_status(self, route_id: int, server_id: int | None = None) -> Any:
        return await self._request(
            "GET", f"/api/raw-routes/{route_id}/deploy-status", server_id=server_id
        )

    # ------------------------------------------- pre-flight checks (form-encoded /api/*)

    async def test_upstream(
        self, host: str, port: int | str, scheme: str = "http", server_id: int | None = None
    ) -> Any:
        """Probe a backend for reachability. Form-encoded: the handler reads ``r.FormValue``."""
        return await self._request(
            "POST",
            "/api/proxy-hosts/test-upstream",
            data={
                "host": str(host).strip(),
                "port": str(port).strip(),
                "scheme": "https" if scheme == "https" else "http",
            },
            server_id=server_id,
        )

    async def validate_raw_route(
        self,
        caddyfile_src: str | None = None,
        json_data: str | None = None,
        server_id: int | None = None,
    ) -> Any:
        """Validate a raw route before saving it. Form-encoded (``r.FormValue``).

        ``caddyfile_src`` is checked through Caddy's ``/adapt``; ``json_data`` gets a structural
        check (it must carry a non-empty ``handle`` array).
        """
        data: dict[str, str] = {}
        if caddyfile_src:
            data["caddyfile_src"] = caddyfile_src
        if json_data:
            data["json_data"] = json_data
        return await self._request(
            "POST", "/api/raw-routes/validate", data=data, server_id=server_id
        )


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
