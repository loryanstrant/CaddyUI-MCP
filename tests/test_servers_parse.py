"""Offline tests for the ``/servers`` HTML parser, pinned against both markup generations.

CaddyUI has no JSON endpoint listing its Caddy servers, so ``list_caddy_servers`` scrapes the
HTML admin page. That page was rewritten once already (v2.20.0's "Caddy Fleet" redesign),
silently degrading the old ordinal-based parser. These tests pin **both** generations —
``classic`` (v2.16.9) and ``fleet`` (v2.20.1), each captured verbatim from the live instance —
so the next redesign is a failing test rather than a silent data-quality regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from caddyui_mcp.client import _detect_markup, parse_servers_html

FIXTURES = Path(__file__).parent / "fixtures"

#: The two generations captured from the real instance. Every test that isn't about a
#: generation-specific quirk runs against both.
GENERATIONS = {
    "classic": "servers_v2_16_9.html",
    "fleet": "servers_v2_20_1.html",
}

#: The six homelab servers present in both captures.
REAL_IDS = {4, 8, 9, 10, 11, 12}


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


@pytest.fixture(params=sorted(GENERATIONS), ids=sorted(GENERATIONS))
def real_page(request: pytest.FixtureRequest) -> tuple[str, dict]:
    """(generation, parsed) for each captured real ``/servers`` page."""
    return request.param, parse_servers_html(_load(GENERATIONS[request.param]))


@pytest.fixture
def edge_page() -> dict:
    """The synthetic v2.20.1 page covering cases the homelab doesn't currently produce."""
    return parse_servers_html(_load("servers_v2_20_1_edge.html"))


# ------------------------------------------------------------------ both real generations


def test_markup_generation_detected():
    assert _detect_markup(_load(GENERATIONS["classic"])) == "classic"
    assert _detect_markup(_load(GENERATIONS["fleet"])) == "fleet"
    assert _detect_markup("<html><body>login</body></html>") == "unknown"


def test_finds_every_server(real_page: tuple[str, dict]):
    _, parsed = real_page
    assert set(parsed) == REAL_IDS


def test_name_is_clean(real_page: tuple[str, dict]):
    """The regression that motivated this parser.

    v2.20's name cell holds ``<strong>NAME</strong>`` plus a "Current" badge, a version pill and
    tags. Substituting tags with spaces glued them all onto the name; splitting on them doesn't.
    """
    _, parsed = real_page
    assert parsed[9]["name"] == "SHOCKWAVE"
    for entry in parsed.values():
        name = entry["name"]
        assert name and "\n" not in name
        assert "Current" not in name
        assert "v2." not in name
        assert len(name) <= 64


def test_status_is_lowercase_normalised(real_page: tuple[str, dict]):
    """v2.16 renders ``online``, v2.20 renders ``Online``. Both must normalise."""
    _, parsed = real_page
    for entry in parsed.values():
        assert entry["status"] == "online"


def test_admin_url_extracted(real_page: tuple[str, dict]):
    _, parsed = real_page
    assert parsed[9]["admin_url"] == "http://shockwave.strant.casa:2019"
    assert all(e["admin_url"] for e in parsed.values())


def test_policy_extracted(real_page: tuple[str, dict]):
    _, parsed = real_page
    assert all(e["policy"] == "managed" for e in parsed.values())


def test_last_contact_extracted(real_page: tuple[str, dict]):
    """Taken from the cell before the actions cell — index 5 on classic, 4 on fleet."""
    _, parsed = real_page
    for entry in parsed.values():
        assert entry["last_contact"]
        # Must not have picked up the actions cell by mistake.
        assert "Select" not in entry["last_contact"]
        assert "Delete" not in entry["last_contact"]


def test_no_duplicate_or_phantom_ids(real_page: tuple[str, dict]):
    """The responsive mobile list repeats every server as <article>, not <tr>."""
    _, parsed = real_page
    assert len(parsed) == len(REAL_IDS)


def test_both_generations_agree(real_page: tuple[str, dict]):
    """Same fleet, same answer — only the volatile last_contact may differ."""

    def strip(parsed: dict) -> dict:
        return {
            sid: {k: v for k, v in entry.items() if k != "last_contact"}
            for sid, entry in parsed.items()
        }

    classic = parse_servers_html(_load(GENERATIONS["classic"]))
    fleet = parse_servers_html(_load(GENERATIONS["fleet"]))
    assert strip(classic) == strip(fleet)


# --------------------------------------------------------------------- edge cases (fleet)


def test_current_server_row_without_select_is_found(edge_page: dict):
    """The selected server loses its Select form, so /config and /edit are the anchors."""
    assert 9 in edge_page
    assert edge_page[9]["name"] == "SHOCKWAVE"


def test_current_badge_and_tags_do_not_bleed_into_name(edge_page: dict):
    assert edge_page[9]["name"] == "SHOCKWAVE"
    assert edge_page[9]["caddy_version"] == "v2.10.0"


def test_external_policy_detected(edge_page: dict):
    """`external` means writes are stored but never pushed to Caddy — a real safety signal."""
    assert edge_page[21]["policy"] == "external"
    assert edge_page[9]["policy"] == "managed"


def test_offline_and_unknown_status(edge_page: dict):
    assert edge_page[21]["status"] == "offline"
    assert edge_page[22]["status"] == "unknown"


def test_never_contacted(edge_page: dict):
    assert edge_page[21]["last_contact"] == "Never"


def test_unix_socket_admin_endpoint(edge_page: dict):
    """The v2.20 page recommends Unix sockets, which have no scheme and no port."""
    assert edge_page[22]["admin_url"] == "unix//run/caddy-admin.sock"


def test_html_entities_unescaped(edge_page: dict):
    assert edge_page[22]["name"] == "R&D box"


def test_blank_name_falls_back_to_admin_host(edge_page: dict):
    assert edge_page[23]["name"] == "metroplex"


def test_server_named_managed_does_not_poison_policy(edge_page: dict):
    """Policy is matched outside the name cell, so a server *named* "Managed" is safe."""
    assert edge_page[24]["name"] == "Managed"
    assert edge_page[24]["policy"] == "external"


def test_mobile_cards_do_not_duplicate_ids(edge_page: dict):
    assert sorted(edge_page) == [9, 21, 22, 23, 24]


# ------------------------------------------------------------------------- degradation


def test_empty_state_parses_to_nothing():
    assert parse_servers_html(_load("servers_empty.html")) == {}


@pytest.mark.parametrize(
    "html",
    ["", "<html><body>Sign in</body></html>", "<tr><td>not a server</td></tr>", "<<<>>>"],
    ids=["empty", "login-page", "unrelated-row", "garbage"],
)
def test_unparseable_input_returns_empty_without_raising(html: str):
    assert parse_servers_html(html) == {}
