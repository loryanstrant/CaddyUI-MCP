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
   case-insensitively; `admin_url` and `sample_domains` disambiguate if a name is unclear.
3. If you omit `server_id`, tools target CaddyUI's **default server (1)** — which may be empty
   even when other servers have many hosts. So when a list looks empty, check
   `list_caddy_servers` before concluding there's nothing there.
4. Entries with `"orphaned": true` are leftover rows from a deleted Caddy server — don't treat
   them as live servers.

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

## Notes
- `id`s are integers assigned by CaddyUI. `domains` is a single string (space/comma separated
  for multiple hostnames).
- Mutations need a token with `full` scope (or `proxy_write` for proxy hosts only). A
  read-only token returns an error containing "token scope is read-only".
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
    server label, e.g. "SHOCKWAVE"), `admin_url`, `status`, `proxy_host_count`, `sample_domains`,
    and `orphaned` (true = leftover rows from a deleted server; ignore those). Server labels come
    from CaddyUI's Servers page; host counts come from probing server ids 1..probe_max.

    Args:
        probe_max: Highest server id to probe for lingering hosts (default 24).
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
    """List all TLS certificates managed by CaddyUI for a server.

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
        config: The certificate as a JSON object.
        server_id: Which managed Caddy server to create it on (default: server 1).
    """
    try:
        return _fmt(await get_client().create_certificate(config, server_id=server_id))
    except Exception as e:
        return f"Error creating certificate: {e}"


@mcp.tool(annotations=_WRITE)
async def update_certificate(
    cert_id: int, config: dict[str, Any], server_id: int | None = None
) -> str:
    """Update an existing certificate.

    Args:
        cert_id: The integer id of the certificate to update.
        config: The updated certificate as a JSON object.
        server_id: Which managed Caddy server the certificate is on (default: server 1).
    """
    try:
        return _fmt(await get_client().update_certificate(cert_id, config, server_id=server_id))
    except Exception as e:
        return f"Error updating certificate {cert_id}: {e}"


@mcp.tool(annotations=_DESTRUCTIVE)
async def delete_certificate(cert_id: int, server_id: int | None = None) -> str:
    """Permanently delete a certificate by id.

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
