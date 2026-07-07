# Decisions & lessons

## 2026-07-07 — Multi-server: `/api/v1` is scoped to the `caddyui_server` cookie

**Context.** CaddyUI can centrally manage several Caddy instances. Every `/api/v1` list/CRUD
endpoint is scoped to "the current server" (confirmed in the in-app `/api/docs` and in
`server.go`: `apiV1ListProxyHosts` → `models.ListProxyHosts(DB, currentServerID(r), …)`).
`currentServerID` reads the **`caddyui_server` cookie** and **defaults to server 1** when
absent. A tokenized request (no cookie) therefore only ever sees server 1 — which is commonly
empty — so the MCP looked like it saw "0 hosts" even on a deployment with dozens of hosts
spread across servers 2, 4, 7, 8, 9, 10, …

**Decision.** Thread an optional `server_id` through every tool/client method, sent as the
`caddyui_server` cookie (set on the httpx client instance, not per-request — per-request
`cookies=` is deprecated in httpx). Add a `list_caddy_servers` discovery tool: CaddyUI exposes
**no JSON endpoint** listing its Caddy servers (only HTML `/servers` pages + a session-only
`POST /servers/{id}/select`), so discovery **probes** `caddyui_server` ids 1..N and reports
those holding proxy hosts, with sample domains to identify each. The server `instructions`
tell the LLM to call `list_caddy_servers` first and not conclude "empty" from server 1 alone.

**Lesson.** When a wrapped API returns suspiciously empty results, check for **implicit
session/tenant scoping** (cookie/header/selected-context) before assuming the backend is
broken — here the CaddyUI↔Caddy link was perfectly healthy; the API was just scoped to an
empty default server.

## 2026-07-07 — Wrap CaddyUI's `/api/v1` REST API, not the UI or Caddy itself

**Context.** CaddyUI (`X4Applegate/caddyui`, Go + chi + SQLite) exposes three kinds of HTTP
routes: HTML page routes for the browser UI, unversioned `/api/*` AJAX helpers, and a
**stable versioned JSON REST API under `/api/v1`** (added v2.13, documented in-app at
`/api/docs`). CaddyUI's SQLite DB is the source of truth; it pushes generated config to
Caddy's admin API. So there are three possible targets for an MCP: Caddy's admin API, the
CaddyUI UI routes, or the CaddyUI REST API.

**Decision.** Target **`/api/v1`**. It's the only stable, documented, machine-oriented
surface, and it keeps CaddyUI as the source of truth (edits show up in the UI and get pushed
to Caddy correctly). Resources wrapped: proxy hosts, redirection hosts, raw routes,
certificates — full CRUD (+ toggle / maintenance). A few read-only `/api/*` status endpoints
(`caddy-version`, `system-stats`, `upstream-health`, `search`) are surfaced too, flagged as
less stable.

**Auth.** Single header `Authorization: Bearer <token>`. Tokens are minted in the UI at
`/api-tokens`, shown once, stored as a SHA-256 hash. Scopes: `full`, `read_only` (GET/HEAD
only — mutations return `403 "token scope is read-only"`), `proxy_write` (proxy hosts only).
Missing/bad token → `401`. **The real token prefix is `cadu_`** (the in-app docs sample says
`caddyui_tok_…`, but issued tokens are `cadu_…`).

**Lessons (reusable).**
- The `ProxyHost` model has **200+ fields**; the documented create example is heavily
  trimmed. Don't enumerate fields in tool signatures — accept a pass-through `config` dict and
  tell the LLM (via server `instructions`) to `GET` an existing object first to learn the
  shape. Minimal proxy host: `domains`, `forward_scheme`, `forward_host`, `forward_port`.
- Verify against the real instance: `GET /api/v1/proxy-hosts` returns a JSON array (`[]` on an
  empty instance) — a 200 with `[]` is healthy, so the healthcheck/connectivity probe must
  treat an empty array as success, not "no data".
- Built on the canonical homelab MCP shape (`ESPHome-MCP`): Python 3.13 + FastMCP v2, dual
  stdio (`caddyui-mcp`) / HTTP (`caddyui-mcp-web`) entrypoints, digest-pinned `python:3.13-slim`
  image, healthcheck that drives the full MCP handshake.
- The MCP Streamable HTTP endpoint is `/mcp` (no trailing slash); `/mcp/` returns 307. Front
  it with Caddy over HTTP/1.1.
