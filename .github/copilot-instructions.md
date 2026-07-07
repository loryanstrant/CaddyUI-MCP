# Copilot instructions — CaddyUI-MCP

MCP server (Python 3.13 + FastMCP v2) wrapping CaddyUI's `/api/v1` REST API. See `AGENTS.md`
for the full picture; this file mirrors the rules that must hold for inline completions.

- **Tools** live in `src/caddyui_mcp/server.py`, one `@mcp.tool` per operation; they call the
  async client in `client.py` and **return an error string on failure** (never raise). Reads
  use `annotations=_READ`, deletes `annotations=_DESTRUCTIVE`.
- **Auth** is `Authorization: Bearer <token>` from `CADDYUI_TOKEN`. Never hard-code a token or
  URL; both come from env via `CaddyUISettings`. Secrets live in Vaultwarden + gitignored
  `.env`, referenced as `${CADDYUI_TOKEN}`.
- **Create/update** take a pass-through `config: dict` — do not enumerate the 200+ proxy-host
  fields as parameters.
- Match the surrounding style: `from __future__ import annotations`, type hints, 100-col lines,
  ruff-clean. Pin deps. Canonical repo is Gitea; GitHub is a read-only mirror.
