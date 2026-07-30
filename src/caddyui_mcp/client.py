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
import html as html_mod
import logging
import re
from typing import Any

import httpx
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

SERVER_COOKIE = "caddyui_server"

# --------------------------------------------------------------- /servers scraping
#
# CaddyUI exposes no JSON endpoint listing the Caddy servers it manages (verified against
# upstream v2.20.1: ``models.ListCaddyServers`` is consumed only by HTML handlers), so the
# ``/servers`` page has to be scraped. That page has been rewritten once already — v2.20.0
# replaced the "Caddy Servers" table with the "Caddy Fleet" inventory — so these rules are
# deliberately **ordinal-free**: they anchor on element role (``<strong>``), stable class
# names (``version-pill``), whole-segment token equality, and position *relative to a stable
# anchor* rather than raw column indices. Both generations are pinned by fixtures in
# ``tests/fixtures/``. See DECISIONS.md (2026-07-30) before changing any of this.

# Non-content elements whose text must never be mistaken for a field value.
_NOISE_RE = re.compile(r"<(script|style|svg)\b.*?</\1>", re.S | re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
# Every row links to at least one of these, in both generations. The fleet markup drops the
# ``Select`` form on the currently-selected server, so ``select`` alone is not enough.
_SERVER_ID_RE = re.compile(r"/servers/(\d+)/(?:select|edit|config|delete)")
# A Caddy admin endpoint: ``http://host:2019`` or a Unix socket (which the v2.20 page
# actively recommends, and which has no scheme or port).
_ADMIN_URL_RE = re.compile(r"(?:https?://[^\s\"'<>]+:\d+|unix/[^\s\"'<>]+)")
# v2.16 renders these lowercase, v2.20 capitalises them — hence re.I, and callers lower().
_STATUS_RE = re.compile(r"\b(online|offline|unknown|error)\b", re.I)
_POLICY_RE = re.compile(r"\A(managed|external)\Z", re.I)
_STRONG_RE = re.compile(r"<strong[^>]*>(.*?)</strong>", re.S | re.I)
_VERSION_PILL_RE = re.compile(
    r"<span[^>]*class=\"[^\"]*version-pill[^\"]*\"[^>]*>(.*?)</span>", re.S | re.I
)
# Text that sits alongside the name but is not part of it. The escapes are em/en dashes,
# which CaddyUI uses as the "no value" placeholder in several cells.
_BADGE_TEXT_RE = re.compile(r"\A(current|no tags|never|managed|external|[-\u2014\u2013])\Z", re.I)


def _text_segments(fragment: str) -> list[str]:
    """Split an HTML fragment into its visible text runs, one per element.

    Uses ``_HTML_TAG_RE.split`` rather than ``.sub`` on purpose: substituting tags with a
    space **destroys the element boundaries that carry the meaning**, which is exactly how
    the v2.20 "Caddy Fleet" rewrite turned a name cell into
    ``"SHOCKWAVE \\n Current \\n v2.10.0 edge"``. Splitting keeps each element's text
    separate so the caller can pick the one it wants.
    """
    fragment = _NOISE_RE.sub(" ", fragment)
    return [t for t in (html_mod.unescape(p).strip() for p in _HTML_TAG_RE.split(fragment)) if t]


def _detect_markup(html: str) -> str:
    """Identify which ``/servers`` template generation produced ``html`` (for logging only).

    Deliberately not surfaced in tool output — it is noise for an LLM. It exists so a future
    third generation shows up as ``markup=unknown`` in the container logs instead of as a
    silent data-quality regression.
    """
    if "fleet-table" in html or "fleet-name" in html:
        return "fleet"  # v2.20.0+
    if "md:table" in html or "version-pill" in html:
        return "classic"  # v2.16.x and earlier
    return "unknown"


def _parse_server_row(row: str) -> tuple[int, dict[str, Any]] | None:
    """Parse one ``<tr>`` into ``(server_id, {...})``, or ``None`` if it isn't a server row."""
    id_m = _SERVER_ID_RE.search(row)
    if id_m is None:
        return None  # header row, or the colspan empty-state row
    server_id = int(id_m.group(1))

    cells = _CELL_RE.findall(row)
    name_cell = cells[0] if cells else ""

    # --- admin_url: searched over the whole row, so column order is irrelevant.
    admin_m = _ADMIN_URL_RE.search(row)
    admin_url = admin_m.group(0) if admin_m else None

    # --- caddy_version: the ``version-pill`` class is identical in both generations.
    version_m = _VERSION_PILL_RE.search(name_cell)
    caddy_version = None
    if version_m is not None:
        segs = _text_segments(version_m.group(1))
        caddy_version = segs[0] if segs else None

    # --- name: prefer the semantic element, then the first non-badge text run, then the host.
    name = None
    strong_m = _STRONG_RE.search(name_cell)
    if strong_m is not None:  # fleet markup wraps the name in <strong>
        segs = _text_segments(strong_m.group(1))
        name = segs[0] if segs else None
    if not name:
        # Classic markup has no <strong>. Drop the version pill (it is a sibling of the
        # name, not part of it) and take the first text run that isn't a status badge.
        stripped = _VERSION_PILL_RE.sub(" ", name_cell)
        candidates = [s for s in _text_segments(stripped) if not _BADGE_TEXT_RE.match(s)]
        name = candidates[0] if candidates else None
    if not name and admin_url:  # last resort: derive a label from the admin host
        name = admin_url.split("//", 1)[-1].split(":", 1)[0].split(".", 1)[0]

    # --- status: cell 1 in both generations, but fall back to the row so a reorder degrades
    # to "still finds it" rather than "returns None".
    status_source = cells[1] if len(cells) > 1 else row
    status_m = _STATUS_RE.search(" ".join(_text_segments(status_source))) or _STATUS_RE.search(row)
    status = status_m.group(1).lower() if status_m is not None else None

    # --- policy: whole-segment token match in any cell other than the name cell and the
    # actions cell. Token matching (rather than "cell 3") survives a future column reorder;
    # excluding the name cell stops a server literally *named* "Managed" from poisoning it.
    policy = None
    for idx, cell in enumerate(cells):
        if idx == 0 or _SERVER_ID_RE.search(cell):
            continue
        match = next((s for s in _text_segments(cell) if _POLICY_RE.match(s)), None)
        if match is not None:
            policy = match.lower()
            break

    # --- last_contact: the cell immediately before the actions cell. Position relative to a
    # stable anchor, so one rule covers index 5 (classic, 7 columns) and 4 (fleet, 6).
    last_contact = None
    actions_idx = next((i for i, c in enumerate(cells) if _SERVER_ID_RE.search(c)), None)
    if actions_idx is not None and actions_idx > 0:
        segs = _text_segments(cells[actions_idx - 1])
        last_contact = segs[0] if segs else None

    return server_id, {
        "name": name,
        "admin_url": admin_url,
        "status": status,
        "policy": policy,
        "caddy_version": caddy_version,
        "last_contact": last_contact,
    }


def parse_servers_html(html: str) -> dict[int, dict[str, Any]]:
    """Parse CaddyUI's ``/servers`` page into ``{id: {name, admin_url, status, ...}}``.

    Pure and I/O-free so both markup generations can be pinned by fixtures. Rows that don't
    link to ``/servers/{id}/…`` (the header, the empty-state row) are skipped, as are the
    responsive mobile cards — they use ``<article>``/``<div>``, not ``<tr>``, so they never
    produce duplicate ids.
    """
    out: dict[int, dict[str, Any]] = {}
    for row in _ROW_RE.findall(html):
        parsed = _parse_server_row(row)
        if parsed is not None:
            out[parsed[0]] = parsed[1]
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

    async def managed_servers(self) -> dict[int, dict[str, Any]]:
        """Fetch and parse CaddyUI's ``/servers`` page into ``{id: {name, admin_url, …}}``.

        CaddyUI has no JSON endpoint listing its Caddy servers, so this scrapes the HTML admin
        page (a Bearer token is accepted there). Parsing lives in :func:`parse_servers_html`;
        this method is only transport plus diagnostics. Returns ``{}`` if the page can't be
        fetched or parsed — discovery must never crash a tool.
        """
        try:
            async with self._http() as client:
                resp = await client.get("/servers")
        except Exception as e:  # discovery must never crash a tool
            logger.warning("Could not fetch /servers page for names: %s", e)
            return {}

        # httpx does not follow redirects by default and raise_for_status() ignores 3xx, so a
        # redirect to /login would otherwise sail through as "no servers found".
        if resp.status_code >= 300:
            logger.warning(
                "GET /servers -> HTTP %s. That page is admin-gated, so the API token's user "
                "must have the admin role; server names/policy will be unavailable.",
                resp.status_code,
            )
            return {}

        html = resp.text
        markup = _detect_markup(html)
        servers = parse_servers_html(html)
        if not servers:
            logger.warning(
                "Parsed 0 servers from /servers (markup=%s, %d bytes). If markup=unknown, "
                "CaddyUI's template changed again — see DECISIONS.md.",
                markup,
                len(html),
            )
        else:
            logger.debug("Parsed %d servers from /servers (markup=%s)", len(servers), markup)
        return servers

    async def discover_servers(self, probe_max: int = 24) -> list[dict[str, Any]]:
        """Discover the Caddy servers CaddyUI manages, with names and proxy-host counts.

        Combines the ``/servers`` page (for id → name/admin_url/status) with a probe of
        ``GET /api/v1/proxy-hosts`` per id (for host counts + sample domains, since the API is
        scoped by the ``caddyui_server`` cookie). Reports current servers plus any id that
        still holds proxy hosts but is no longer a registered server (``orphaned: true`` —
        leftover rows from a deleted Caddy server).
        """
        managed = await self.managed_servers()

        async def count(sid: int) -> tuple[int, list[Any]]:
            try:
                hosts = await self.list_proxy_hosts(server_id=sid)
            except CaddyUIError:
                return 0, []
            if not isinstance(hosts, list):
                return 0, []
            return len(hosts), [h.get("domains") for h in hosts[:6] if isinstance(h, dict)]

        ids = sorted(set(managed) | set(range(1, probe_max + 1)))
        counts = dict(zip(ids, await asyncio.gather(*(count(i) for i in ids)), strict=True))

        servers: list[dict[str, Any]] = []
        for sid in ids:
            n, domains = counts[sid]
            is_managed = sid in managed
            if not is_managed and n == 0:
                continue  # neither a registered server nor holding any hosts
            info = managed.get(sid, {})
            # .get() for every key: a partial parse must degrade to nulls, never KeyError.
            servers.append(
                {
                    "server_id": sid,
                    "name": info.get("name"),
                    "admin_url": info.get("admin_url"),
                    "status": info.get("status"),
                    "policy": info.get("policy"),
                    "caddy_version": info.get("caddy_version"),
                    "last_contact": info.get("last_contact"),
                    "orphaned": not is_managed,
                    "proxy_host_count": n,
                    "sample_domains": domains,
                }
            )
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
