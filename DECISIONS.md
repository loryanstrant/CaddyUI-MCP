# Decisions & lessons

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
