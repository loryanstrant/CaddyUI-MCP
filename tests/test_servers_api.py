"""Offline tests for ``GET /api/v1/servers`` normalisation and the fallback glue.

CaddyUI v2.20.2 added that endpoint at this project's request (upstream issue #18), replacing
the HTML scraping. The fixture is captured verbatim from the live instance, the same discipline
that turned the v2.20.0 markup rewrite into a failing test rather than a silent regression.

The load-bearing test here is :func:`test_both_sources_emit_the_same_key_set` — it pins the
contract that lets ``discover_servers`` stay ignorant of which source produced its data.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from caddyui_mcp._servers_html import parse_servers_html
from caddyui_mcp.client import (
    SERVER_FIELDS,
    CaddyUIClient,
    CaddyUIError,
    CaddyUISettings,
    normalise_api_servers,
)

FIXTURES = Path(__file__).parent / "fixtures"
API_FIXTURE = FIXTURES / "servers_api_v2_20_2.json"

#: The six homelab servers, present in both the JSON and HTML fixtures.
REAL_IDS = {4, 8, 9, 10, 11, 12}


@pytest.fixture
def api_page() -> dict[int, dict[str, Any]]:
    return normalise_api_servers(json.loads(API_FIXTURE.read_text()))


# --------------------------------------------------------------------- normalisation


def test_parses_every_server(api_page: dict):
    assert set(api_page) == REAL_IDS
    assert api_page[9]["name"] == "SHOCKWAVE"
    assert api_page[9]["admin_url"] == "http://shockwave.strant.casa:2019"


def test_status_is_lowercase(api_page: dict):
    assert all(e["status"] == "online" for e in api_page.values())


def test_type_extracted(api_page: dict):
    assert all(e["type"] == "managed" for e in api_page.values())


def test_tags_is_always_a_list(api_page: dict):
    assert all(isinstance(e["tags"], list) for e in api_page.values())


def test_empty_version_becomes_none():
    """Upstream sends "" for unknown; None is what an LLM reads as "unknown"."""
    parsed = normalise_api_servers([{"id": 1, "name": "x", "version": ""}])
    assert parsed[1]["caddy_version"] is None
    parsed = normalise_api_servers([{"id": 1, "name": "x", "version": "v2.10.0"}])
    assert parsed[1]["caddy_version"] == "v2.10.0"


def test_last_contact_at_passes_through_unmodified(api_page: dict):
    """An RFC3339 timestamp must not be reformatted — reformatting invites subtle drift."""
    raw = json.loads(API_FIXTURE.read_text())
    expected = {s["id"]: s["last_contact_at"] for s in raw}
    assert {i: e["last_contact_at"] for i, e in api_page.items()} == expected
    assert normalise_api_servers([{"id": 1, "last_contact_at": None}])[1]["last_contact_at"] is None


def test_status_is_lowercased_even_if_upstream_changes():
    """Lowercase by construction, not by upstream's continued goodwill."""
    assert normalise_api_servers([{"id": 1, "status": "Online"}])[1]["status"] == "online"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        "error string",
        [],
        [None, 3, "x"],
        [{"name": "no id"}],
        [{"id": "4", "name": "string id"}],
    ],
    ids=["none", "dict", "string", "empty", "junk-entries", "missing-id", "non-int-id"],
)
def test_malformed_payload_degrades_without_raising(payload: Any):
    assert normalise_api_servers(payload) == {}


def test_both_sources_emit_the_same_key_set():
    """The contract that lets discover_servers stay source-agnostic."""
    api = normalise_api_servers(json.loads(API_FIXTURE.read_text()))
    html = parse_servers_html((FIXTURES / "servers_v2_20_1.html").read_text())
    assert api and html
    for source in (api, html):
        for entry in source.values():
            assert set(entry) == set(SERVER_FIELDS)
            assert isinstance(entry["tags"], list)
    # Same fleet, so the fields both sources can produce must agree.
    shared = ("name", "admin_url", "status", "type")
    assert {i: {k: e[k] for k in shared} for i, e in api.items()} == {
        i: {k: e[k] for k in shared} for i, e in html.items()
    }


# ------------------------------------------------------------------- fallback glue


def _client(monkeypatch: pytest.MonkeyPatch, *, servers_exc: Exception) -> CaddyUIClient:
    c = CaddyUIClient(CaddyUISettings(caddyui_url="https://x", caddyui_token="t"))

    async def boom() -> Any:
        raise servers_exc

    monkeypatch.setattr(c, "servers", boom)
    return c


def test_404_falls_back_to_html_and_warns(monkeypatch: pytest.MonkeyPatch, caplog):
    """An old CaddyUI 404s the route; we scrape, and we say so loudly."""
    c = _client(monkeypatch, servers_exc=CaddyUIError("nope", status_code=404))
    scraped = {9: dict.fromkeys(SERVER_FIELDS)}

    async def fake_scrape() -> dict:
        return scraped

    monkeypatch.setattr(c, "_scrape_servers", fake_scrape)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(c.registered_servers()) == scraped
    # The removal horizon depends on this warning existing and being greppable in the wild.
    assert any("predates v2.20.2" in r.message % r.args for r in caplog.records)


@pytest.mark.parametrize("status", [401, 403, 422, 500])
def test_non_404_does_not_fall_back(monkeypatch: pytest.MonkeyPatch, status: int):
    """An auth/scope/server failure is not an old CaddyUI. Scraping would mask it."""
    c = _client(monkeypatch, servers_exc=CaddyUIError("boom", status_code=status))

    async def must_not_run() -> dict:
        raise AssertionError("must not fall back to scraping on a non-404")

    monkeypatch.setattr(c, "_scrape_servers", must_not_run)
    assert asyncio.run(c.registered_servers()) is None


# ------------------------------------------------------------------ discover_servers


def _discovering(monkeypatch: pytest.MonkeyPatch, registered, hosts_by_id) -> CaddyUIClient:
    c = CaddyUIClient(CaddyUISettings(caddyui_url="https://x", caddyui_token="t"))

    async def fake_registered() -> Any:
        return registered

    async def fake_hosts(server_id: int | None = None) -> Any:
        return [{"domains": f"a{i}.example.com"} for i in range(hosts_by_id.get(server_id, 0))]

    monkeypatch.setattr(c, "registered_servers", fake_registered)
    monkeypatch.setattr(c, "list_proxy_hosts", fake_hosts)
    return c


def test_orphans_are_flagged(monkeypatch: pytest.MonkeyPatch):
    """An id holding hosts but no longer registered is a leftover from a deleted server."""
    c = _discovering(monkeypatch, {4: dict.fromkeys(SERVER_FIELDS)}, {2: 6, 4: 11})
    out = {s["server_id"]: s for s in asyncio.run(c.discover_servers(probe_max=8))}
    assert out[2]["orphaned"] is True
    assert out[2]["name"] is None
    assert out[2]["proxy_host_count"] == 6
    assert out[4]["orphaned"] is False
    assert 5 not in out  # neither registered nor holding hosts


def test_unknown_listing_does_not_claim_orphans(monkeypatch: pytest.MonkeyPatch):
    """The regression this tri-state exists for: a failed listing must not mark all orphaned.

    The tool instructions tell the model to ignore ``orphaned`` entries, so reporting True here
    would make a working fleet look empty.
    """
    c = _discovering(monkeypatch, None, {2: 6, 4: 11})
    out = asyncio.run(c.discover_servers(probe_max=8))
    assert out, "servers holding hosts must still be reported"
    assert all(s["orphaned"] is None for s in out)


def test_registered_server_outside_probe_range_is_listed(monkeypatch: pytest.MonkeyPatch):
    """probe_max bounds the orphan hunt only; registered servers are enumerated exactly."""
    c = _discovering(monkeypatch, {30: dict.fromkeys(SERVER_FIELDS)}, {})
    out = {s["server_id"]: s for s in asyncio.run(c.discover_servers(probe_max=8))}
    assert 30 in out
    assert out[30]["orphaned"] is False


def test_entry_shape_is_stable(monkeypatch: pytest.MonkeyPatch):
    c = _discovering(monkeypatch, {4: dict.fromkeys(SERVER_FIELDS)}, {4: 1})
    (entry,) = asyncio.run(c.discover_servers(probe_max=4))
    assert set(entry) == {
        "server_id",
        *SERVER_FIELDS,
        "orphaned",
        "proxy_host_count",
        "sample_domains",
    }
    assert entry["tags"] == []
