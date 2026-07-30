# Decisions & lessons

## 2026-07-30 — Scrape `/servers` semantically, not by ordinal (CaddyUI v2.20 "Caddy Fleet")

**Context.** CaddyUI shipped v2.16.10 → v2.20.1 in a single day. Diffing the route tables
between the tags showed the **`/api/v1` surface is unchanged** — only three routes were added
anywhere in the app, none of them versioned. But v2.20.0 rewrote `web/templates/servers.html`,
the one page this MCP has to scrape because CaddyUI exposes no JSON list of its Caddy servers
(re-verified: `models.ListCaddyServers` is consumed only by HTML handlers). The name cell went
from `<div>NAME</div>` to `<div class="fleet-name"><strong>NAME</strong><span>Current</span></div>
<div class="fleet-tags"><span class="version-pill">…</span>…</div>`, health badges went
lowercase → capitalised, 7 columns became 6, and the selected row lost its `Select` form.

Two bugs followed. `_HTML_TAG_RE.sub(" ", cell)` **substituted** tags with spaces, destroying the
element boundaries, so badges/version/tags glued onto `name`:
`"SHOCKWAVE \n Current \n v2.10.0 edge"`. And `_STATUS_RE` had no `re.I`, so `Online` fell
through unmatched. Notably the *old* template already emitted a version pill in the same cell —
the bug was **latent on v2.16.9** and only invisible because no server had a version recorded.

**Decision.** A pure, I/O-free `parse_servers_html()` with ordinal-free rules: name prefers the
`<strong>` element, then the first text run that isn't a known badge word, then the admin host;
status matches case-insensitively and normalises to lowercase; `policy` is a whole-segment
`managed|external` token match in any cell **other than** the name cell (so a server literally
named "Managed" can't poison it); `last_contact` is the cell **immediately before** the cell
holding the `/servers/{id}/…` links — one rule covering index 5 on classic and 4 on fleet. Both
generations are pinned as fixtures captured verbatim from the live instance. `managed_servers()`
now also logs the detected markup generation and warns specifically on a 3xx (the page is
admin-gated, and httpx neither follows redirects nor raises on them, so a non-admin token
previously produced a silent empty map).

**Lessons (reusable).**
- **When you must scrape, anchor on what survives a redesign:** element role (`<strong>`), a
  stable class (`version-pill`), whole-segment token equality, and position *relative to a
  stable anchor* — never a raw column index.
- **Split on tags, don't substitute them.** `re.split` preserves the element boundaries that
  carry the meaning; `re.sub(" ")` destroys exactly the information you need.
- **Never assume casing** in scraped text — `re.I` plus normalisation on the way out.
- Keep the parser a pure `str → dict` function. The next redesign then costs one failing
  fixture test instead of a silent data-quality bug in production.

## 2026-07-30 — No HTML-parser dependency: regex + two real fixtures beats a DOM library

**Context.** The obvious reaction to the above is "use BeautifulSoup/selectolax".

**Decision.** Stay on `re` + stdlib `html.unescape`. A DOM parser replaces **tokenisation**, but
the breakage here was **semantic** — "which element holds the name". `soup.select_one("td:nth-child(1)")`
is exactly as wrong as `cells[0]` after a column reorder, so the dependency would have bought
nothing while adding a compiled extension to a `python:3.13-slim` image and to the `pip-audit` /
`bandit` surface. The surface is one ~45-line pure function against one known template, pinned by
fixtures for both generations. **Escape hatch:** if a future generation genuinely needs nested
traversal or attribute selectors, use stdlib `html.parser.HTMLParser` — still zero deps.

**Lesson.** A dependency is only justified if it removes the part that actually breaks. Name the
failure mode first, then ask whether the library addresses *that*.

## 2026-07-30 — Certificates gained a `managed` (ACME DNS-01) source in v2.17

**Context.** The only `/api/v1` payload change across v2.16.9 → v2.20.1: `certificateToAPIMap`
gained `dns_provider` + `dns_profile_id`, and `source` gained a third value `"managed"` —
Caddy-managed ACME certificates including standalone wildcards.

Behaviours found by reading the handlers rather than the docs:
- `apiV1CreateCertificate` defaults an omitted `source` to `"pem"` and **validates nothing**.
- `apiV1UpdateCertificate` is a **partial merge that ignores empty strings** — a field cannot be
  blanked through the API.
- `apiV1DeleteCertificate` returns **409** while the certificate is referenced.
- `ListCertificates` (the API path) does **not** filter `managed`, though the HTML dropdowns'
  `ListCertificateOptionsForUser` does — so managed certs are **mixed into** `list_certificates`
  output even though they never appear in the web UI's picker.

**Decision.** Document all of it in `INSTRUCTIONS` + docstrings, and add exactly **two** guards to
`create_certificate`: reject an unknown `source`, and reject `source: "managed"` without
`dns_provider`. Deliberately **no** guard on an omitted `source` — "defaults to pem" is
documented, legitimate behaviour, and guarding it would break valid calls.

**Lessons.**
- **Diff the route table between tags before diffing behaviour.** It bounded a four-minor-version
  bump to "3 new routes, zero breaking changes" in minutes and stopped it being treated as a rewrite.
- **When upstream validates nothing, the wrapper is the only place a typo is catchable** — but
  guard only where the silent fallback yields an *unusable* row, not where it yields a sane default.

## 2026-07-30 — CaddyUI's unversioned `/api/*` helpers are form-encoded, not JSON

**Context.** Wrapping `/api/proxy-hosts/test-upstream` and `/api/raw-routes/validate` needed a
transport change: both read `r.FormValue`, so a JSON body arrives as an empty form and the
handler sees nothing at all — a silent wrong answer, not an error. `_request` grew a `data=`
path alongside `json=` (mutually exclusive).

Two upstream comments in the same file are stale relative to the code: `/api/raw-routes/validate`
no longer uses Caddy's `/load?validate_only=true` (v2.9.233 removed it — Caddy ignores the flag,
so the "validation" was actually *applying* a ghost server to the running config); it now uses
`/adapt` for Caddyfile input and a structural `handle`-array check for JSON. Separately,
`/api/version-check` reports a `latest` derived from Docker Hub tags that can be **older** than
`current` (observed: `latest: v2.9.233` on a v2.20.1 install), so only `current` is trustworthy.

**Lesson.** **Read the handler before writing the client method.** `r.FormValue` vs
`json.NewDecoder` changes the signature, and an endpoint's name, its doc comment, and what it now
actually does can all disagree.

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
