from __future__ import annotations

import asyncio

import pytest

from caddyui_mcp.client import (
    SERVER_COOKIE,
    SERVER_FIELDS,
    CaddyUIClient,
    CaddyUIError,
    CaddyUISettings,
    normalise_api_servers,
    select_unused_certificates,
)
from caddyui_mcp.server import _fmt, create_certificate, validate_raw_route

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


def test_server_cookie_name():
    # The multi-server scoping cookie must match CaddyUI's constant.
    assert SERVER_COOKIE == "caddyui_server"


def test_json_and_data_are_mutually_exclusive():
    """/api/v1 takes JSON; the unversioned /api/* AJAX helpers take form values. Never both."""
    c = CaddyUIClient(CaddyUISettings(caddyui_url="https://x", caddyui_token="t"))
    with pytest.raises(ValueError, match="not both"):
        asyncio.run(c._request("POST", "/x", json={"a": 1}, data={"b": "2"}))


# ------------------------------------------------- unused certificates (pure logic)
#
# Mirrors CaddyUI's own internal/server/certificate_usage.go, which has no API endpoint.

_CERTS = [
    {"id": 1, "name": "wildcard-pem", "source": "pem"},
    {"id": 2, "name": "on-disk", "source": "path"},
    {"id": 3, "name": "acme-wildcard", "source": "managed"},
    {"id": 4, "name": "spare", "source": "pem"},
]


def _unused_names(**kwargs) -> list[str]:
    result = select_unused_certificates(
        _CERTS,
        kwargs.get("proxy_hosts", []),
        kwargs.get("redirection_hosts", []),
        kwargs.get("raw_routes", []),
    )
    return [c["name"] for c in result["unused"]]


def test_unreferenced_custom_certificates_are_unused():
    assert _unused_names() == ["wildcard-pem", "on-disk", "spare"]


def test_managed_certificates_are_never_unused():
    """Caddy attaches ACME certs by domain coverage, not by certificate_id."""
    assert "acme-wildcard" not in _unused_names()


def test_reference_from_any_resource_type_counts():
    assert "wildcard-pem" not in _unused_names(proxy_hosts=[{"certificate_id": 1}])
    assert "on-disk" not in _unused_names(redirection_hosts=[{"certificate_id": 2}])
    assert "spare" not in _unused_names(raw_routes=[{"certificate_id": 4}])


def test_certificate_id_zero_means_no_certificate():
    """0 is CaddyUI's 'none', not a reference to certificate id 0."""
    result = select_unused_certificates(_CERTS, [{"certificate_id": 0}], [], [])
    assert result["referenced_ids"] == []
    assert len(result["unused"]) == 3


def test_non_list_input_degrades_instead_of_raising():
    """A failed upstream call must not turn into a traceback."""
    result = select_unused_certificates(_CERTS, None, "error string", [])
    assert result["certificates_checked"] == 4
    assert result["referenced_ids"] == []


# ---------------------------------------------------- tool-layer guards (no network)


@pytest.fixture
def no_client(monkeypatch: pytest.MonkeyPatch):
    """Make any attempt to reach CaddyUI an error, so guards must reject before calling."""

    def boom():
        raise AssertionError("guard should have rejected before touching the client")

    monkeypatch.setattr("caddyui_mcp.server.get_client", boom)


@pytest.mark.asyncio
async def test_create_certificate_rejects_unknown_source(no_client):
    out = await create_certificate({"name": "x", "source": "pfx"})
    assert "not valid" in out
    assert "managed" in out and "pem" in out and "path" in out


@pytest.mark.asyncio
async def test_create_certificate_rejects_managed_without_dns_provider(no_client):
    out = await create_certificate({"name": "x", "source": "managed"})
    assert "dns_provider" in out


@pytest.mark.asyncio
async def test_create_certificate_allows_managed_with_dns_provider(monkeypatch):
    """The guard must not block a valid managed cert."""
    seen: dict = {}

    class _Stub:
        async def create_certificate(self, config, server_id=None):
            seen.update(config)
            return {"id": 7, **config}

    monkeypatch.setattr("caddyui_mcp.server.get_client", lambda: _Stub())
    out = await create_certificate(
        {"name": "wild", "source": "managed", "dns_provider": "cloudflare"}
    )
    assert seen["dns_provider"] == "cloudflare"
    assert '"id": 7' in out


@pytest.mark.asyncio
async def test_validate_raw_route_requires_an_argument(no_client):
    assert "either" in await validate_raw_route()


# ---------------------------------------------------------------------------- live


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_discover_servers(live_settings: tuple[str, str]):
    """Against a real multi-server CaddyUI: discovery finds at least one server with hosts."""
    url, token = live_settings
    client = CaddyUIClient(CaddyUISettings(caddyui_url=url, caddyui_token=token))
    try:
        servers = await client.discover_servers(probe_max=24)
        assert isinstance(servers, list)
        assert servers, "expected at least one server with proxy hosts"
        for s in servers:
            assert isinstance(s["server_id"], int)
            assert s["proxy_host_count"] >= 1
            assert set(s) == {
                "server_id",
                *SERVER_FIELDS,
                "orphaned",
                "proxy_host_count",
                "sample_domains",
            }
        # Every *registered* server must have resolved a name — the contract the whole tool
        # exists for, and the assertion that would have caught the old admin-gating failure.
        assert all(s["name"] for s in servers if s["orphaned"] is False)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_servers_endpoint_shape(live_settings: tuple[str, str]):
    """`GET /api/v1/servers` (CaddyUI 2.20.2+, upstream issue #18) returns what we expect."""
    url, token = live_settings
    client = CaddyUIClient(CaddyUISettings(caddyui_url=url, caddyui_token=token))
    try:
        raw = await client.servers()
        assert isinstance(raw, list) and raw
        for s in raw:
            assert isinstance(s["id"], int)
            assert s["name"]
            assert s["status"] == s["status"].lower()
            assert s["type"] in {"managed", "external"}
            assert isinstance(s["tags"], list)
            assert "last_contact_at" in s
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_sources_agree(live_settings: tuple[str, str]):
    """The JSON endpoint and the deprecated scraper must describe the same fleet identically.

    This validates the normalisation contract against reality rather than against fixtures.
    It is also the **deletion trigger**: once this has passed for months, the scraper has
    demonstrably stopped adding information, and it goes — along with this test.
    """
    url, token = live_settings
    client = CaddyUIClient(CaddyUISettings(caddyui_url=url, caddyui_token=token))
    try:
        api = normalise_api_servers(await client.servers())
        html = await client._scrape_servers()
        assert api, "expected the JSON endpoint to return servers"
        if html is None:
            pytest.skip("/servers page not readable (needs an admin-scoped token)")
        assert set(api) == set(html), "the two sources disagree on which servers exist"
        shared = ("name", "admin_url", "status", "type")
        for sid in api:
            for key in shared:
                assert api[sid][key] == html[sid][key], f"server {sid} disagrees on {key}"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_caddyui_version(live_settings: tuple[str, str]):
    url, token = live_settings
    client = CaddyUIClient(CaddyUISettings(caddyui_url=url, caddyui_token=token))
    try:
        v = await client.caddyui_version()
        assert {"current", "latest", "has_update"} <= set(v)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_certificate_shape(live_settings: tuple[str, str]):
    """`source` is one of three values; the DNS fields exist only on CaddyUI 2.17+.

    Version-gated so the suite is green on an older instance and tightens automatically once
    it is upgraded, rather than needing an edit at upgrade time.
    """
    url, token = live_settings
    client = CaddyUIClient(CaddyUISettings(caddyui_url=url, caddyui_token=token))
    try:
        current = (await client.caddyui_version()).get("current", "")
        parts = current.lstrip("v").split(".")
        has_dns_fields = (int(parts[0]), int(parts[1])) >= (2, 17)

        certs = await client.list_certificates()
        assert isinstance(certs, list)
        for c in certs:
            assert c["source"] in {"pem", "path", "managed"}
            if has_dns_fields:
                assert "dns_provider" in c
                assert "dns_profile_id" in c
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_validate_raw_route(live_settings: tuple[str, str]):
    """Non-mutating: validation only. Proves the form-encoded transport reaches r.FormValue."""
    url, token = live_settings
    client = CaddyUIClient(CaddyUISettings(caddyui_url=url, caddyui_token=token))
    try:
        bad = await client.validate_raw_route(json_data='{"nope": 1}')
        assert bad["ok"] is False

        good = await client.validate_raw_route(
            json_data='[{"handle":[{"handler":"static_response","body":"hi"}]}]'
        )
        assert good["ok"] is True, good
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_test_upstream_unreachable(live_settings: tuple[str, str]):
    """Port 9 is the discard port — reliably closed, so this is self-contained."""
    url, token = live_settings
    client = CaddyUIClient(CaddyUISettings(caddyui_url=url, caddyui_token=token))
    try:
        result = await client.test_upstream("127.0.0.1", 9)
        assert result["ok"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.live
async def test_live_find_unused_certificates(live_settings: tuple[str, str]):
    url, token = live_settings
    client = CaddyUIClient(CaddyUISettings(caddyui_url=url, caddyui_token=token))
    try:
        result = await client.unused_certificates()
        assert isinstance(result["unused"], list)
        assert isinstance(result["certificates_checked"], int)
        # Managed (ACME) certificates must never be reported as unused.
        assert all(c["source"] in {"pem", "path"} for c in result["unused"])
    finally:
        await client.close()


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
