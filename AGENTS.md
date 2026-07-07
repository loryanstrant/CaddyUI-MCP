# AGENTS.md — CaddyUI-MCP

> Cross-tool agent instructions (Claude Code, Copilot agent mode).
> **Gotcha:** Copilot *inline* suggestions do NOT read this file — any rule that must hold
> for inline completions is mirrored into `.github/copilot-instructions.md`. Keep them in sync.

## Project

An MCP server that wraps **CaddyUI**'s `/api/v1` REST API so an MCP client can manage a Caddy
reverse proxy (proxy/redirection hosts, raw routes, certificates). Python 3.13 + FastMCP v2.
Canonical repo on Gitea (`gitea.strant.casa/loryanstrant/CaddyUI-MCP`); GitHub is a one-way
push mirror — never review/merge PRs there. Modelled on the `ESPHome-MCP` homelab template.

## How to work here

- Use the `plan-before-coding` and `write-a-test-plan` skills (global, in `~/.copilot/skills/`
  — also available to Claude Code). Apply `dev-standards/standards/{core,generic}.md`.
- Smallest shippable slice; one thing at a time. No new tools/deps unless they replace
  something. Pin dependencies; prefer boring, proven libraries.
- At end of a task, capture any reusable lesson in `DECISIONS.md`.

## Layout

- `src/caddyui_mcp/client.py` — async httpx client for `/api/v1` (Bearer auth, typed errors).
- `src/caddyui_mcp/server.py` — FastMCP tools (one per operation) + `INSTRUCTIONS`.
- `src/caddyui_mcp/__main__.py` — `main()` (stdio) / `main_web()` (HTTP :8080) with a
  connectivity pre-flight.
- `healthcheck.py` — drives the full MCP handshake for the container healthcheck.

## Build / test / deploy

- `make check` = lint + format-check + typecheck + test (the gate).
- Live tests need a real instance: `CADDYUI_URL=… CADDYUI_TOKEN=… pytest -m live` (they create
  and delete a throwaway proxy host — safe, self-cleaning).
- Deploy as a container via Dockhand to **SHOCKWAVE**; front `/mcp` with Caddy at
  `caddyui-mcp.strant.casa`; register into MetaMCP. Verify the real artefact end-to-end
  (`initialize` + `tools/list` through the deployed endpoint), not just an HTTP 200.

## Secrets

`CADDYUI_TOKEN` lives in Vaultwarden (`CaddyUI API token (Claude Code)`) and in a gitignored
`.env` — **never** in the repo. Reference as `${CADDYUI_TOKEN}`. Mint tokens in CaddyUI at
`/api-tokens` (scope `full` for management).
