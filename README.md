# CaddyUI MCP

An [MCP](https://modelcontextprotocol.io) server for **[CaddyUI](https://github.com/X4Applegate/caddyui)**
— the self-hosted web UI for the [Caddy](https://caddyserver.com) reverse proxy. It lets an
MCP client (Claude, etc.) inspect and manage your Caddy configuration in natural language:
proxy hosts, redirection hosts, raw routes, and TLS certificates.

It wraps CaddyUI's stable, versioned **REST API under `/api/v1`** (added in CaddyUI v2.13),
authenticating with an API token (`Authorization: Bearer <token>`). CaddyUI's own SQLite DB
is the source of truth; it pushes the generated config to Caddy's admin API — so this server
talks to CaddyUI, not to Caddy directly.

## Tools

| Resource | Tools |
| --- | --- |
| **Servers** | `list_caddy_servers` |
| **Proxy hosts** | `list_proxy_hosts`, `get_proxy_host`, `create_proxy_host`, `update_proxy_host`, `delete_proxy_host`, `toggle_proxy_host`, `set_proxy_host_maintenance` |
| **Redirection hosts** | `list_redirection_hosts`, `get_redirection_host`, `create_redirection_host`, `update_redirection_host`, `delete_redirection_host`, `toggle_redirection_host` |
| **Raw routes** | `list_raw_routes`, `get_raw_route`, `create_raw_route`, `update_raw_route`, `delete_raw_route`, `toggle_raw_route` |
| **Certificates** | `list_certificates`, `get_certificate`, `create_certificate`, `update_certificate`, `delete_certificate`, `find_unused_certificates`, `managed_certificate_status` |
| **Status** (read-only) | `caddy_version`, `caddyui_version`, `system_stats`, `upstream_health`, `search` |
| **Pre-flight / diagnostics** (read-only) | `test_upstream`, `validate_raw_route`, `check_deploy_status` |

Create/update tools take a JSON `config` object. The proxy-host model has 200+ optional
fields, so the intended workflow is **`get_*` an existing object, then modify and re-send** —
the server's instructions tell the LLM to do exactly that.

**Check before you write.** `test_upstream` confirms a backend is reachable before you point a
proxy host at it; `validate_raw_route` confirms a raw route parses before you save it (otherwise
errors only surface at the next Caddy sync); `check_deploy_status` reports afterwards whether DNS
propagated and the certificate was issued. All three are read-only.

### Certificates

`source` is one of `pem` (inline `cert_pem`/`key_pem`), `path` (`cert_path`/`key_path` on the
Caddy host), or `managed` — ACME **DNS-01**, including standalone wildcards, added in CaddyUI
2.17. A `managed` certificate needs `dns_provider` (and usually `dns_profile_id`), whose
credentials must already be saved in CaddyUI's Settings.

Worth knowing: CaddyUI does not validate `source` and silently defaults it to `pem`;
`list_certificates` returns `managed` entries **mixed in** with the rest (the web UI's dropdowns
hide them, the API does not); `update_certificate` is a partial merge that ignores empty strings,
so a field cannot be blanked; and `delete_certificate` returns 409 while anything still
references the certificate. `find_unused_certificates` shows what is safe to remove.

### Multi-server

CaddyUI can centrally manage **several Caddy instances**, and every resource is scoped to one
server. Almost every tool takes an optional **`server_id`**; omitting it targets CaddyUI's
**default server (1)**, which may be empty even when other servers are full. Call
**`list_caddy_servers`** first — it probes the server ids and reports which hold proxy hosts
(with sample domains so you can tell them apart) — then pass the chosen `server_id` to the
other tools. (Server selection uses CaddyUI's `caddyui_server` cookie; there is no documented
API parameter for it.)

Each entry also reports **`policy`**, **`caddy_version`** and **`last_contact`**. `policy` is a
safety signal worth reading: `managed` means CaddyUI validates and **pushes** config to that
Caddy instance, while `external` means it only *monitors* it — a write there is stored in
CaddyUI's database and returns success, but never reaches Caddy.

> CaddyUI has no JSON endpoint listing its Caddy servers, so `list_caddy_servers` parses the
> HTML `/servers` page. That page was redesigned in CaddyUI v2.20 ("Caddy Fleet"), so the parser
> is deliberately structure-based rather than column-index-based and is pinned by fixtures for
> both markup generations (`tests/fixtures/`). See `DECISIONS.md` before changing it. The page is
> admin-gated: a non-admin token still lists resources fine but yields no server names.

## Configuration

Config is via environment variables (12-factor). Copy [`.env.example`](.env.example) to `.env`:

| Variable | Required | Description |
| --- | --- | --- |
| `CADDYUI_URL` | yes | CaddyUI base URL, e.g. `https://caddyui.example.com` (default `https://caddyui.strant.casa`). |
| `CADDYUI_TOKEN` | yes | API token minted in CaddyUI at `/api-tokens`. Scope `full` (all CRUD), `proxy_write` (proxy hosts only), or `read_only`. |
| `MCP_HTTP_PORT` | no | Port for the web entrypoint (default `8080`). |
| `LOG_LEVEL` | no | `DEBUG`/`INFO`/`WARNING`/`ERROR` (default `INFO`). |

### Getting a token

In CaddyUI, open **API Tokens** (`/api-tokens`), create a token with the scope you want, and
copy it (shown once). For full read+write management, use `full`.

## Run with Docker

```bash
cp .env.example .env       # then set CADDYUI_URL and CADDYUI_TOKEN
docker compose up -d --build
docker compose ps          # STATUS should become "healthy"
```

The server listens on `:8080` and serves MCP over **Streamable HTTP** at
`http://<host>:8080/mcp`. The container `HEALTHCHECK` performs a full MCP handshake and calls
`list_proxy_hosts`, so it only reports healthy when CaddyUI is actually reachable and the
token works (an empty CaddyUI still counts as healthy).

Once the registry image is published, pin it in `compose.yaml`:

```yaml
image: ghcr.io/loryanstrant/caddyui-mcp:latest
```

## Connect an MCP client

Point your client at the Streamable HTTP endpoint (note: `/mcp`, no trailing slash):

```json
{
  "mcpServers": {
    "caddyui": { "type": "http", "url": "http://<host>:8080/mcp" }
  }
}
```

For a stdio client, run `caddyui-mcp` (instead of the web entrypoint) with the same env.

## Develop

```bash
make install-dev   # venv + deps
make check         # lint + format-check + typecheck + test

# live tests against a real CaddyUI (creates and deletes a throwaway proxy host):
CADDYUI_URL=https://caddyui.example.com CADDYUI_TOKEN=cadu_... .venv/bin/pytest -m live
```

## License

[MIT](LICENSE).
