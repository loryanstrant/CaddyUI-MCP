"""FastMCP server exposing CaddyUI's REST API as MCP tools.

Four managed resources — proxy hosts, redirection hosts, raw routes, TLS certificates —
each with full CRUD (+ toggle / maintenance where CaddyUI offers it), plus read-only status
tools and multi-server discovery. All tools catch exceptions and return an error string
rather than raising, so the MCP client always gets a usable response.

Multi-server: CaddyUI can manage several Caddy instances. Every resource is scoped to one
server; tools take an optional ``server_id`` (see ``list_caddy_servers``). Omitting it targets
CaddyUI's default server (1).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastmcp import FastMCP

from caddyui_mcp.client import get_client

logger = logging.getLogger(__name__)

#: Valid certificate `source` values. CaddyUI accepts anything and stores it verbatim, so the
#: wrapper is the only place a typo is catchable.
_CERT_SOURCES = frozenset({"pem", "path", "managed"})

INSTRUCTIONS = """\
This server manages a Caddy reverse proxy through the CaddyUI REST API. It exposes four
resources — **proxy hosts**, **redirection hosts**, **raw routes**, and **TLS
certificates** — plus read-only status tools.

## Multi-server (important)
CaddyUI can centrally manage **several Caddy instances**, and every resource belongs to one
server. Almost every tool takes an optional **`server_id`**:
1. Call **`list_caddy_servers`** first. It returns each server's **`name`** — CaddyUI's own
   server label (e.g. "SHOCKWAVE", "SOUNDWAVE") — plus `admin_url`, `status`, and host count.
2. **Resolve the server the user named** (e.g. "SHOCKWAVE", "the soundwave box") to its
   `server_id` from that list, then pass **`server_id`** to the other tools. Match names
   case-insensitively; `admin_url`, `tags` and `sample_domains` disambiguate if a name is
   unclear.
3. If you omit `server_id`, tools target CaddyUI's **default server (1)** — which may be empty
   even when other servers have many hosts. So when a list looks empty, check
   `list_caddy_servers` before concluding there's nothing there.
4. Entries with `"orphaned": true` are leftover rows from a deleted Caddy server — don't treat
   them as live servers. Only `true` means that: `"orphaned": null` means the server list
   couldn't be read, so those entries may well be real servers.
5. **`type` tells you whether a write will actually reach Caddy.** `"managed"` = CaddyUI
   validates and **pushes** config to that Caddy instance. `"external"` = CaddyUI **only
   monitors** it — a create/update there is stored in CaddyUI's database and returns success,
   but is **never pushed**, so nothing changes on the wire. Warn the user before writing to an
   `external` server. Entries also carry `caddy_version`, `tags`, and `last_contact_at`
   (RFC3339 UTC, or null).

## Workflow
1. `list_caddy_servers` → pick a `server_id`.
2. `list_*`/`get_*` (with `server_id`) to inspect.
3. To **create or update**, FIRST `get_*` an existing object of the same type to learn the
   field shape — the proxy-host model has 200+ optional fields, so copy-and-modify beats
   guessing. Pass a JSON object as `config` (and the same `server_id`).
   - Minimal proxy host: `{"domains": "app.example.com", "forward_scheme": "http",
     "forward_host": "10.0.0.5", "forward_port": 3000}`.
   - Minimal redirection host: `{"domains": "old.example.com", "forward_scheme": "https",
     "forward_domain": "new.example.com", "forward_http_code": 301}`.
4. `toggle_*` enables/disables; `set_proxy_host_maintenance` flips maintenance mode.
5. `delete_*` is **permanent** — confirm the id (and server) with `get_*` first.

## Certificates
`source` is one of three values, and CaddyUI does **not** validate it on create:
- **`pem`** — inline material in `cert_pem` + `key_pem`. This is the **silent default** if you
  omit `source`, so always set it explicitly.
- **`path`** — `cert_path` + `key_path`, as seen by the Caddy host's filesystem.
- **`managed`** — ACME **DNS-01**, including standalone wildcards (CaddyUI 2.17+). Needs
  `dns_provider` (and usually `dns_profile_id`); those credentials must already be saved in
  CaddyUI's Settings. Caddy obtains and renews these automatically.

Behaviours worth knowing before you act:
- `list_certificates` returns **`managed` entries mixed in** with hand-supplied ones. CaddyUI's
  own web dropdowns filter them out; the API does not. Filter on `source` yourself.
- `update_certificate` is a **partial merge that ignores empty strings** — you cannot blank a
  field through it. Sending `{"cert_pem": ""}` is a no-op, not a clear. (Unlike the proxy-host
  tools, where you re-send the whole object.)
- `delete_certificate` returns **409** if any host or route still references it. Use
  `find_unused_certificates` to see what's safe to remove.
- Deleting a **`managed`** certificate **stops ACME renewal** for its domains — that's more than
  dropping a stored file, so confirm with the user first.

## Before you write
- `test_upstream` checks a backend is reachable *before* you create a proxy host pointing at it.
- `validate_raw_route` checks a raw route parses *before* you save it — otherwise errors don't
  surface until the next sync.
- `check_deploy_status` after creating tells you whether DNS propagated and the cert was issued.

## Notes
- `id`s are integers assigned by CaddyUI. `domains` is a single string (space/comma separated
  for multiple hostnames).
- Mutations need a token with `full` scope (or `proxy_write` for proxy hosts only). A
  read-only token returns an error containing "token scope is read-only". `test_upstream` and
  `validate_raw_route` change nothing, but are POSTs, so a read-only token is refused for them
  too.
"""

mcp = FastMCP(name="CaddyUI", instructions=INSTRUCTIONS)

_READ = {"readOnlyHint": True, "destructiveHint": False}
_WRITE = {"readOnlyHint": False, "destructiveHint": False}
_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True}


def _fmt(obj: Any) -> str:
    """Format an API result as pretty JSON (or a confirmation for empty responses)."""
    if obj is None:
        return "OK — success (no content returned)."
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


# =============================================================================== servers


@mcp.tool(annotations=_READ)
async def list_caddy_servers(probe_max: int = 24) -> str:
    """List the Caddy servers CaddyUI manages, with their **names**, admin URLs and host counts.

    Use this to resolve a server the user names (e.g. "SHOCKWAVE") to its `server_id`, then pass
    that `server_id` to the other tools. Each entry has: `server_id`, `name` (CaddyUI's own
    server label, e.g. "SHOCKWAVE"), `admin_url`, `status`, `type` (`managed` = CaddyUI pushes
    config there; `external` = monitor-only, writes never reach Caddy), `caddy_version`, `tags`,
    `last_contact_at`, `proxy_host_count`, `sample_domains`, and `orphaned` (true = leftover
    rows from a deleted server, ignore those; null = the server list couldn't be read).

    Server details come from CaddyUI's `GET /api/v1/servers` (CaddyUI 2.20.2+). Older instances
    fall back to its HTML Servers page, which is admin-gated and provides no `tags` or
    `last_contact_at`. Host counts always come from probing.

    Args:
        probe_max: How deep to scan for **orphaned** leftovers from deleted servers (default
            24). Registered servers are listed exactly, whatever this is set to.
    """
    try:
        servers = await get_client().discover_servers(probe_max=probe_max)
    except Exception as e:
        return f"Error discovering servers: {e}"
    if not servers:
        return (
            f"No servers with proxy hosts found in ids 1..{probe_max}. The default server (1) "
            "may be empty; pass a higher probe_max or a known server_id directly."
        )
    return _fmt(servers)


# =========================================================================== proxy hosts


@mcp.tool(annotations=_READ)
async def list_proxy_hosts(server_id: int | None = None) -> str:
    """List all proxy hosts for a server. Run `list_caddy_servers` first to choose `server_id`.

    Args:
        server_id: Which managed Caddy server to target (default: CaddyUI's server 1).
    """
    try:
        return _fmt(await get_client().list_proxy_hosts(server_id=server_id))
    except Exception as e:
        return f"Error listing proxy hosts: {e}"


@mcp.tool(annotations=_READ)
async def get_proxy_host(host_id: int, server_id: int | None = None) -> str:
    """Get a single proxy host by id (the full field set — use this to learn the model shape).

    Args:
        host_id: The integer id of the proxy host.
        server_id: Which managed Caddy server to target (default: server 1).
    """
    try:
        return _fmt(await get_client().get_proxy_host(host_id, server_id=server_id))
    except Exception as e:
        return f"Error getting proxy host {host_id}: {e}"


@mcp.tool(annotations=_WRITE)
async def create_proxy_host(config: dict[str, Any], server_id: int | None = None) -> str:
    """Create a new proxy host on a server.

    Args:
        config: The proxy host as a JSON object. Minimum: `domains`, `forward_scheme`
            ("http"/"https"), `forward_host`, `forward_port`. `get_proxy_host` on an existing
            host to see all available fields (ssl_enabled, websocket_support, …).
        server_id: Which managed Caddy server to create it on (default: server 1).
    """
    try:
        return _fmt(await get_client().create_proxy_host(config, server_id=server_id))
    except Exception as e:
        return f"Error creating proxy host: {e}"


@mcp.tool(annotations=_WRITE)
async def update_proxy_host(
    host_id: int, config: dict[str, Any], server_id: int | None = None
) -> str:
    """Update an existing proxy host (send the full object, as returned by `get_proxy_host`).

    Args:
        host_id: The integer id of the proxy host to update.
        config: The updated proxy host as a JSON object.
        server_id: Which managed Caddy server the host is on (default: server 1).
    """
    try:
        return _fmt(await get_client().update_proxy_host(host_id, config, server_id=server_id))
    except Exception as e:
        return f"Error updating proxy host {host_id}: {e}"


@mcp.tool(annotations=_DESTRUCTIVE)
async def delete_proxy_host(host_id: int, server_id: int | None = None) -> str:
    """Permanently delete a proxy host by id.

    Args:
        host_id: The integer id of the proxy host to delete.
        server_id: Which managed Caddy server the host is on (default: server 1).
    """
    try:
        await get_client().delete_proxy_host(host_id, server_id=server_id)
        return f"Deleted proxy host {host_id}."
    except Exception as e:
        return f"Error deleting proxy host {host_id}: {e}"


@mcp.tool(annotations=_WRITE)
async def toggle_proxy_host(host_id: int, server_id: int | None = None) -> str:
    """Enable or disable a proxy host (flips its `enabled` state).

    Args:
        host_id: The integer id of the proxy host to toggle.
        server_id: Which managed Caddy server the host is on (default: server 1).
    """
    try:
        return _fmt(await get_client().toggle_proxy_host(host_id, server_id=server_id))
    except Exception as e:
        return f"Error toggling proxy host {host_id}: {e}"


@mcp.tool(annotations=_WRITE)
async def set_proxy_host_maintenance(host_id: int, server_id: int | None = None) -> str:
    """Toggle maintenance mode for a proxy host (serves a maintenance page while on).

    Args:
        host_id: The integer id of the proxy host.
        server_id: Which managed Caddy server the host is on (default: server 1).
    """
    try:
        return _fmt(await get_client().set_proxy_host_maintenance(host_id, server_id=server_id))
    except Exception as e:
        return f"Error setting maintenance for proxy host {host_id}: {e}"


# ====================================================================== redirection hosts


@mcp.tool(annotations=_READ)
async def list_redirection_hosts(server_id: int | None = None) -> str:
    """List all redirection hosts (domain-to-domain HTTP redirects) for a server.

    Args:
        server_id: Which managed Caddy server to target (default: server 1).
    """
    try:
        return _fmt(await get_client().list_redirection_hosts(server_id=server_id))
    except Exception as e:
        return f"Error listing redirection hosts: {e}"


@mcp.tool(annotations=_READ)
async def get_redirection_host(host_id: int, server_id: int | None = None) -> str:
    """Get a single redirection host by id.

    Args:
        host_id: The integer id of the redirection host.
        server_id: Which managed Caddy server to target (default: server 1).
    """
    try:
        return _fmt(await get_client().get_redirection_host(host_id, server_id=server_id))
    except Exception as e:
        return f"Error getting redirection host {host_id}: {e}"


@mcp.tool(annotations=_WRITE)
async def create_redirection_host(config: dict[str, Any], server_id: int | None = None) -> str:
    """Create a new redirection host on a server.

    Args:
        config: JSON object. Minimum: `domains`, `forward_scheme`, `forward_domain`,
            `forward_http_code` (e.g. 301/302). `get_redirection_host` to see all fields.
        server_id: Which managed Caddy server to create it on (default: server 1).
    """
    try:
        return _fmt(await get_client().create_redirection_host(config, server_id=server_id))
    except Exception as e:
        return f"Error creating redirection host: {e}"


@mcp.tool(annotations=_WRITE)
async def update_redirection_host(
    host_id: int, config: dict[str, Any], server_id: int | None = None
) -> str:
    """Update an existing redirection host.

    Args:
        host_id: The integer id of the redirection host to update.
        config: The updated redirection host as a JSON object.
        server_id: Which managed Caddy server the host is on (default: server 1).
    """
    try:
        return _fmt(
            await get_client().update_redirection_host(host_id, config, server_id=server_id)
        )
    except Exception as e:
        return f"Error updating redirection host {host_id}: {e}"


@mcp.tool(annotations=_DESTRUCTIVE)
async def delete_redirection_host(host_id: int, server_id: int | None = None) -> str:
    """Permanently delete a redirection host by id.

    Args:
        host_id: The integer id of the redirection host to delete.
        server_id: Which managed Caddy server the host is on (default: server 1).
    """
    try:
        await get_client().delete_redirection_host(host_id, server_id=server_id)
        return f"Deleted redirection host {host_id}."
    except Exception as e:
        return f"Error deleting redirection host {host_id}: {e}"


@mcp.tool(annotations=_WRITE)
async def toggle_redirection_host(host_id: int, server_id: int | None = None) -> str:
    """Enable or disable a redirection host.

    Args:
        host_id: The integer id of the redirection host to toggle.
        server_id: Which managed Caddy server the host is on (default: server 1).
    """
    try:
        return _fmt(await get_client().toggle_redirection_host(host_id, server_id=server_id))
    except Exception as e:
        return f"Error toggling redirection host {host_id}: {e}"


# ============================================================================= raw routes


@mcp.tool(annotations=_READ)
async def list_raw_routes(server_id: int | None = None) -> str:
    """List all raw routes (advanced hand-written Caddy routes) for a server.

    Args:
        server_id: Which managed Caddy server to target (default: server 1).
    """
    try:
        return _fmt(await get_client().list_raw_routes(server_id=server_id))
    except Exception as e:
        return f"Error listing raw routes: {e}"


@mcp.tool(annotations=_READ)
async def get_raw_route(route_id: int, server_id: int | None = None) -> str:
    """Get a single raw route by id.

    Args:
        route_id: The integer id of the raw route.
        server_id: Which managed Caddy server to target (default: server 1).
    """
    try:
        return _fmt(await get_client().get_raw_route(route_id, server_id=server_id))
    except Exception as e:
        return f"Error getting raw route {route_id}: {e}"


@mcp.tool(annotations=_WRITE)
async def create_raw_route(config: dict[str, Any], server_id: int | None = None) -> str:
    """Create a new raw route. `get_raw_route` on an existing route to learn the shape first.

    Args:
        config: The raw route as a JSON object.
        server_id: Which managed Caddy server to create it on (default: server 1).
    """
    try:
        return _fmt(await get_client().create_raw_route(config, server_id=server_id))
    except Exception as e:
        return f"Error creating raw route: {e}"


@mcp.tool(annotations=_WRITE)
async def update_raw_route(
    route_id: int, config: dict[str, Any], server_id: int | None = None
) -> str:
    """Update an existing raw route.

    Args:
        route_id: The integer id of the raw route to update.
        config: The updated raw route as a JSON object.
        server_id: Which managed Caddy server the route is on (default: server 1).
    """
    try:
        return _fmt(await get_client().update_raw_route(route_id, config, server_id=server_id))
    except Exception as e:
        return f"Error updating raw route {route_id}: {e}"


@mcp.tool(annotations=_DESTRUCTIVE)
async def delete_raw_route(route_id: int, server_id: int | None = None) -> str:
    """Permanently delete a raw route by id.

    Args:
        route_id: The integer id of the raw route to delete.
        server_id: Which managed Caddy server the route is on (default: server 1).
    """
    try:
        await get_client().delete_raw_route(route_id, server_id=server_id)
        return f"Deleted raw route {route_id}."
    except Exception as e:
        return f"Error deleting raw route {route_id}: {e}"


@mcp.tool(annotations=_WRITE)
async def toggle_raw_route(route_id: int, server_id: int | None = None) -> str:
    """Enable or disable a raw route.

    Args:
        route_id: The integer id of the raw route to toggle.
        server_id: Which managed Caddy server the route is on (default: server 1).
    """
    try:
        return _fmt(await get_client().toggle_raw_route(route_id, server_id=server_id))
    except Exception as e:
        return f"Error toggling raw route {route_id}: {e}"


# =========================================================================== certificates


@mcp.tool(annotations=_READ)
async def list_certificates(server_id: int | None = None) -> str:
    """List all TLS certificates CaddyUI holds for a server.

    Includes `source: "managed"` (ACME/DNS-01) entries mixed in with hand-supplied `pem`/`path`
    ones — CaddyUI's web dropdowns hide those, but the API does not. Filter on `source`.

    Args:
        server_id: Which managed Caddy server to target (default: server 1).
    """
    try:
        return _fmt(await get_client().list_certificates(server_id=server_id))
    except Exception as e:
        return f"Error listing certificates: {e}"


@mcp.tool(annotations=_READ)
async def get_certificate(cert_id: int, server_id: int | None = None) -> str:
    """Get a single certificate by id.

    Args:
        cert_id: The integer id of the certificate.
        server_id: Which managed Caddy server to target (default: server 1).
    """
    try:
        return _fmt(await get_client().get_certificate(cert_id, server_id=server_id))
    except Exception as e:
        return f"Error getting certificate {cert_id}: {e}"


@mcp.tool(annotations=_WRITE)
async def create_certificate(config: dict[str, Any], server_id: int | None = None) -> str:
    """Create/register a new certificate. `get_certificate` on an existing one to learn the shape.

    Args:
        config: The certificate as a JSON object. `name` is required. Set `source` explicitly —
            CaddyUI silently defaults it to `"pem"`. Then supply the matching fields:
            `pem` → `cert_pem` + `key_pem`; `path` → `cert_path` + `key_path`;
            `managed` → `dns_provider` (+ usually `dns_profile_id`) for ACME DNS-01.
        server_id: Which managed Caddy server to create it on (default: server 1).
    """
    # CaddyUI validates neither of these and writes the row regardless, so a typo here lands an
    # unusable certificate in its database. Guard only where the fallback is unusable — the
    # omitted-`source` default of "pem" is documented and legitimate, so it is left alone.
    source = config.get("source")
    if source is not None and source not in _CERT_SOURCES:
        return (
            f"Error: source {source!r} is not valid. Use one of: "
            f"{', '.join(sorted(_CERT_SOURCES))}. CaddyUI does not validate this field and "
            "would store the certificate in an unusable state."
        )
    if source == "managed" and not str(config.get("dns_provider") or "").strip():
        return (
            "Error: a 'managed' certificate is issued via ACME DNS-01 and needs 'dns_provider' "
            "(and usually 'dns_profile_id'). Those credentials must already be saved in "
            "CaddyUI's Settings. Run get_certificate on an existing managed certificate to copy "
            "the values."
        )
    try:
        return _fmt(await get_client().create_certificate(config, server_id=server_id))
    except Exception as e:
        return f"Error creating certificate: {e}"


@mcp.tool(annotations=_WRITE)
async def update_certificate(
    cert_id: int, config: dict[str, Any], server_id: int | None = None
) -> str:
    """Update an existing certificate (a partial merge — send only the fields you're changing).

    CaddyUI **ignores empty strings**, so you cannot blank a field this way: `{"cert_pem": ""}`
    is a no-op, not a clear. Delete and recreate the certificate if you need a field emptied.

    Args:
        cert_id: The integer id of the certificate to update.
        config: The fields to change, as a JSON object.
        server_id: Which managed Caddy server the certificate is on (default: server 1).
    """
    try:
        return _fmt(await get_client().update_certificate(cert_id, config, server_id=server_id))
    except Exception as e:
        return f"Error updating certificate {cert_id}: {e}"


@mcp.tool(annotations=_DESTRUCTIVE)
async def delete_certificate(cert_id: int, server_id: int | None = None) -> str:
    """Permanently delete a certificate by id.

    Returns a 409 error if any proxy host, redirection host or raw route still references it —
    clear the reference first (`find_unused_certificates` shows what's safe to remove). Deleting
    a `source: "managed"` certificate also **stops ACME renewal** for its domains, so confirm
    with the user before doing that.

    Args:
        cert_id: The integer id of the certificate to delete.
        server_id: Which managed Caddy server the certificate is on (default: server 1).
    """
    try:
        await get_client().delete_certificate(cert_id, server_id=server_id)
        return f"Deleted certificate {cert_id}."
    except Exception as e:
        return f"Error deleting certificate {cert_id}: {e}"


# ================================================================================= status


@mcp.tool(annotations=_READ)
async def caddy_version(server_id: int | None = None) -> str:
    """Report the Caddy version CaddyUI is talking to for a server (a quick connectivity check).

    Args:
        server_id: Which managed Caddy server to query (default: server 1).
    """
    try:
        return _fmt(await get_client().caddy_version(server_id=server_id))
    except Exception as e:
        return f"Error getting Caddy version: {e}"


@mcp.tool(annotations=_READ)
async def system_stats() -> str:
    """Get CaddyUI system statistics (host/runtime metrics)."""
    try:
        return _fmt(await get_client().system_stats())
    except Exception as e:
        return f"Error getting system stats: {e}"


@mcp.tool(annotations=_READ)
async def upstream_health(server_id: int | None = None) -> str:
    """Get health status of configured proxy upstreams for a server.

    Args:
        server_id: Which managed Caddy server to query (default: server 1).
    """
    try:
        return _fmt(await get_client().upstream_health(server_id=server_id))
    except Exception as e:
        return f"Error getting upstream health: {e}"


@mcp.tool(annotations=_READ)
async def search(query: str) -> str:
    """Search across CaddyUI objects (hosts, routes, certificates).

    Args:
        query: The search string.
    """
    try:
        return _fmt(await get_client().search(query))
    except Exception as e:
        return f"Error searching for {query!r}: {e}"


@mcp.tool(annotations=_READ)
async def caddyui_version() -> str:
    """Report CaddyUI's own version: `current`, `latest` available, and `has_update`.

    Distinct from `caddy_version`, which reports the version of the *Caddy* instance CaddyUI
    manages. `current` is always the field to trust — it tells you which API generation you're
    talking to.

    `latest` is reliable on **CaddyUI 2.20.2+**. Before that its Docker Hub tag query only read
    the first page, so it could report a tag *older* than `current` (upstream issue #17). If
    `latest` sorts below `current`, the instance predates the fix — ignore the field.
    """
    try:
        return _fmt(await get_client().caddyui_version())
    except Exception as e:
        return f"Error getting CaddyUI version: {e}"


# ================================================================= pre-flight / diagnostics


@mcp.tool(annotations=_READ)
async def test_upstream(
    host: str, port: int, scheme: str = "http", server_id: int | None = None
) -> str:
    """Check whether a backend is reachable, **before** creating a proxy host pointing at it.

    Changes nothing, but it is a POST, so a `read_only`-scoped token is refused.

    Args:
        host: Backend hostname or IP (e.g. "10.0.0.5").
        port: Backend port (e.g. 3000).
        scheme: "http" (default) or "https".
        server_id: Which managed Caddy server to test from (default: server 1).
    """
    try:
        return _fmt(
            await get_client().test_upstream(host, port, scheme=scheme, server_id=server_id)
        )
    except Exception as e:
        return f"Error testing upstream {host}:{port}: {e}"


@mcp.tool(annotations=_READ)
async def validate_raw_route(
    caddyfile_src: str | None = None,
    json_data: str | None = None,
    server_id: int | None = None,
) -> str:
    """Validate a raw route **before** saving it. Returns `{"ok": bool, "error": str}`.

    Worth doing every time: a malformed raw route otherwise fails silently at save time and only
    surfaces at the next Caddy sync. Changes nothing, but it is a POST, so a `read_only` token
    is refused.

    Args:
        caddyfile_src: Caddyfile snippet to check (parsed through Caddy's config adapter).
        json_data: Route JSON to check instead — must contain a non-empty `handle` array.
        server_id: Which managed Caddy server to validate against (default: server 1).
    """
    if not caddyfile_src and not json_data:
        return "Error: pass either caddyfile_src or json_data."
    try:
        return _fmt(
            await get_client().validate_raw_route(
                caddyfile_src=caddyfile_src, json_data=json_data, server_id=server_id
            )
        )
    except Exception as e:
        return f"Error validating raw route: {e}"


@mcp.tool(annotations=_READ)
async def check_deploy_status(resource: str, resource_id: int, server_id: int | None = None) -> str:
    """Check post-save deployment of a proxy host or raw route (DNS propagated? cert issued?).

    Use this after `create_proxy_host` / `create_raw_route` to confirm the change actually
    landed, rather than trusting the create response alone.

    Args:
        resource: Either "proxy_host" or "raw_route".
        resource_id: The integer id of that proxy host or raw route.
        server_id: Which managed Caddy server it lives on (default: server 1).
    """
    client = get_client()
    lookup = {
        "proxy_host": client.proxy_host_deploy_status,
        "raw_route": client.raw_route_deploy_status,
    }
    fetch = lookup.get(resource)
    if fetch is None:
        return f"Error: resource must be 'proxy_host' or 'raw_route', not {resource!r}."
    try:
        return _fmt(await fetch(resource_id, server_id=server_id))
    except Exception as e:
        return f"Error getting deploy status for {resource} {resource_id}: {e}"


@mcp.tool(annotations=_READ)
async def managed_certificate_status(cert_id: int, server_id: int | None = None) -> str:
    """Live ACME status of a `managed` certificate on **every** Caddy server CaddyUI knows.

    Per server: whether the definition is deployed there, the certificate actually being served,
    its issuer, expiry, days remaining, and any renewal problem. Only valid for certificates
    whose `source` is `"managed"` — others return an error. Needs CaddyUI 2.17.2+.

    Args:
        cert_id: The integer id of the managed certificate.
        server_id: Which managed Caddy server to ask (default: server 1).
    """
    try:
        return _fmt(await get_client().managed_certificate_status(cert_id, server_id=server_id))
    except Exception as e:
        return f"Error getting managed certificate status for {cert_id}: {e}"


@mcp.tool(annotations=_READ)
async def find_unused_certificates(server_id: int | None = None) -> str:
    """List certificates on a server that no proxy host, redirection host or raw route uses.

    These are candidates for cleanup — `delete_certificate` refuses (409) while a certificate is
    still referenced, so everything listed here is safe to remove. `managed` (ACME) certificates
    are excluded: Caddy attaches those by domain coverage rather than by id, so they are never
    "unused" in this sense.

    Args:
        server_id: Which managed Caddy server to check (default: server 1).
    """
    try:
        return _fmt(await get_client().unused_certificates(server_id=server_id))
    except Exception as e:
        return f"Error finding unused certificates: {e}"
