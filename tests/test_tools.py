from __future__ import annotations

import pytest

from caddyui_mcp.client import CaddyUIClient, CaddyUIError, CaddyUISettings
from caddyui_mcp.server import _fmt

# --------------------------------------------------------------------------- unit


def test_settings_defaults():
    s = CaddyUISettings(caddyui_token="cadu_x")
    assert s.caddyui_url == "https://caddyui.strant.casa"
    assert s.caddyui_token == "cadu_x"


def test_base_url_strips_trailing_slash():
    c = CaddyUIClient(
        CaddyUISettings(caddyui_url="https://caddyui.example.com/", caddyui_token="t")
    )
    assert c._base_url == "https://caddyui.example.com"


def test_auth_header_set_when_token_present():
    c = CaddyUIClient(CaddyUISettings(caddyui_url="https://x", caddyui_token="cadu_abc"))
    http = c._http()
    assert http.headers["Authorization"] == "Bearer cadu_abc"


def test_no_auth_header_when_token_missing():
    c = CaddyUIClient(CaddyUISettings(caddyui_url="https://x", caddyui_token=""))
    http = c._http()
    assert "Authorization" not in http.headers


def test_fmt_none_is_confirmation():
    assert "success" in _fmt(None).lower()


def test_fmt_object_is_json():
    out = _fmt({"id": 1, "domains": "a.example.com"})
    assert '"domains": "a.example.com"' in out


def test_error_carries_status_and_body():
    err = CaddyUIError("boom", status_code=403, body="token scope is read-only")
    assert err.status_code == 403
    assert "read-only" in (err.body or "")


# ---------------------------------------------------------------------------- live


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_proxy_host_roundtrip(live_settings: tuple[str, str]):
    """Against a real CaddyUI: list, then create -> get -> toggle -> delete a throwaway host."""
    url, token = live_settings
    client = CaddyUIClient(CaddyUISettings(caddyui_url=url, caddyui_token=token))
    created_id: int | None = None
    try:
        hosts = await client.list_proxy_hosts()
        assert isinstance(hosts, list)

        created = await client.create_proxy_host(
            {
                "domains": "mcp-selftest.strant.casa",
                "forward_scheme": "http",
                "forward_host": "127.0.0.1",
                "forward_port": 9,
            }
        )
        assert isinstance(created, dict)
        created_id = created["id"]

        fetched = await client.get_proxy_host(created_id)
        assert fetched["domains"] == "mcp-selftest.strant.casa"

        await client.toggle_proxy_host(created_id)
    finally:
        if created_id is not None:
            await client.delete_proxy_host(created_id)
        await client.close()
