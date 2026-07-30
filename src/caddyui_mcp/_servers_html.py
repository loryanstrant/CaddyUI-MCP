"""**Deprecated.** HTML parser for CaddyUI's ``/servers`` page.

CaddyUI v2.20.2 added ``GET /api/v1/servers`` (upstream issue #18, filed by this project),
which returns the same data as JSON and — unlike this page — is not admin-gated. This module
exists **only** for instances older than v2.20.2 and is scheduled for removal; see the
2026-07-31 entry in ``DECISIONS.md`` for the horizon. Do not add features here.
"""

from __future__ import annotations

import html as html_mod
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# --------------------------------------------------------------- /servers scraping
#
# Until CaddyUI v2.20.2 there was no JSON endpoint listing the Caddy servers it manages, so
# the ``/servers`` page had to be scraped. That page was rewritten once in that time — v2.20.0
# replaced the "Caddy Servers" table with the "Caddy Fleet" inventory — so these rules are
# deliberately **ordinal-free**: they anchor on element role (``<strong>``), stable class
# names (``version-pill``), whole-segment token equality, and position *relative to a stable
# anchor* rather than raw column indices. Both generations are pinned by fixtures in
# ``tests/fixtures/``. See DECISIONS.md (2026-07-30) before changing any of this.

# Non-content elements whose text must never be mistaken for a field value.
_NOISE_RE = re.compile(r"<(script|style|svg)\b.*?</\1>", re.S | re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
# Every row links to at least one of these, in both generations. The fleet markup drops the
# ``Select`` form on the currently-selected server, so ``select`` alone is not enough.
_SERVER_ID_RE = re.compile(r"/servers/(\d+)/(?:select|edit|config|delete)")
# A Caddy admin endpoint: ``http://host:2019`` or a Unix socket (which the v2.20 page
# actively recommends, and which has no scheme or port).
_ADMIN_URL_RE = re.compile(r"(?:https?://[^\s\"'<>]+:\d+|unix/[^\s\"'<>]+)")
# v2.16 renders these lowercase, v2.20 capitalises them — hence re.I, and callers lower().
_STATUS_RE = re.compile(r"\b(online|offline|unknown|error)\b", re.I)
_TYPE_RE = re.compile(r"\A(managed|external)\Z", re.I)
_STRONG_RE = re.compile(r"<strong[^>]*>(.*?)</strong>", re.S | re.I)
_VERSION_PILL_RE = re.compile(
    r"<span[^>]*class=\"[^\"]*version-pill[^\"]*\"[^>]*>(.*?)</span>", re.S | re.I
)
# Text that sits alongside the name but is not part of it. The escapes are em/en dashes,
# which CaddyUI uses as the "no value" placeholder in several cells.
_BADGE_TEXT_RE = re.compile(r"\A(current|no tags|never|managed|external|[-\u2014\u2013])\Z", re.I)


def _text_segments(fragment: str) -> list[str]:
    """Split an HTML fragment into its visible text runs, one per element.

    Uses ``_HTML_TAG_RE.split`` rather than ``.sub`` on purpose: substituting tags with a
    space **destroys the element boundaries that carry the meaning**, which is exactly how
    the v2.20 "Caddy Fleet" rewrite turned a name cell into
    ``"SHOCKWAVE \\n Current \\n v2.10.0 edge"``. Splitting keeps each element's text
    separate so the caller can pick the one it wants.
    """
    fragment = _NOISE_RE.sub(" ", fragment)
    return [t for t in (html_mod.unescape(p).strip() for p in _HTML_TAG_RE.split(fragment)) if t]


def _detect_markup(html: str) -> str:
    """Identify which ``/servers`` template generation produced ``html`` (for logging only).

    Deliberately not surfaced in tool output — it is noise for an LLM. It exists so a future
    third generation shows up as ``markup=unknown`` in the container logs instead of as a
    silent data-quality regression.
    """
    if "fleet-table" in html or "fleet-name" in html:
        return "fleet"  # v2.20.0+
    if "md:table" in html or "version-pill" in html:
        return "classic"  # v2.16.x and earlier
    return "unknown"


def _parse_server_row(row: str) -> tuple[int, dict[str, Any]] | None:
    """Parse one ``<tr>`` into ``(server_id, {...})``, or ``None`` if it isn't a server row."""
    id_m = _SERVER_ID_RE.search(row)
    if id_m is None:
        return None  # header row, or the colspan empty-state row
    server_id = int(id_m.group(1))

    cells = _CELL_RE.findall(row)
    name_cell = cells[0] if cells else ""

    # --- admin_url: searched over the whole row, so column order is irrelevant.
    admin_m = _ADMIN_URL_RE.search(row)
    admin_url = admin_m.group(0) if admin_m else None

    # --- caddy_version: the ``version-pill`` class is identical in both generations.
    version_m = _VERSION_PILL_RE.search(name_cell)
    caddy_version = None
    if version_m is not None:
        segs = _text_segments(version_m.group(1))
        caddy_version = segs[0] if segs else None

    # --- name: prefer the semantic element, then the first non-badge text run, then the host.
    name = None
    strong_m = _STRONG_RE.search(name_cell)
    if strong_m is not None:  # fleet markup wraps the name in <strong>
        segs = _text_segments(strong_m.group(1))
        name = segs[0] if segs else None
    if not name:
        # Classic markup has no <strong>. Drop the version pill (it is a sibling of the
        # name, not part of it) and take the first text run that isn't a status badge.
        stripped = _VERSION_PILL_RE.sub(" ", name_cell)
        candidates = [s for s in _text_segments(stripped) if not _BADGE_TEXT_RE.match(s)]
        name = candidates[0] if candidates else None
    if not name and admin_url:  # last resort: derive a label from the admin host
        name = admin_url.split("//", 1)[-1].split(":", 1)[0].split(".", 1)[0]

    # --- status: cell 1 in both generations, but fall back to the row so a reorder degrades
    # to "still finds it" rather than "returns None".
    status_source = cells[1] if len(cells) > 1 else row
    status_m = _STATUS_RE.search(" ".join(_text_segments(status_source))) or _STATUS_RE.search(row)
    status = status_m.group(1).lower() if status_m is not None else None

    # --- type (managed/external): whole-segment token match in any cell other than the name
    # cell and the actions cell. Token matching (rather than "cell 3") survives a future column
    # reorder; excluding the name cell stops a server literally *named* "Managed" poisoning it.
    server_type = None
    for idx, cell in enumerate(cells):
        if idx == 0 or _SERVER_ID_RE.search(cell):
            continue
        match = next((s for s in _text_segments(cell) if _TYPE_RE.match(s)), None)
        if match is not None:
            server_type = match.lower()
            break

    # Must match caddyui_mcp.client.SERVER_FIELDS exactly — asserted by tests over both
    # sources. `tags` and `last_contact_at` are only available from GET /api/v1/servers; this
    # deprecated path reports them as empty/unknown rather than guessing (there is no way to
    # turn "7s ago" back into a timestamp without manufacturing precision).
    return server_id, {
        "name": name,
        "admin_url": admin_url,
        "status": status,
        "type": server_type,
        "caddy_version": caddy_version,
        "tags": [],
        "last_contact_at": None,
    }


def parse_servers_html(html: str) -> dict[int, dict[str, Any]]:
    """Parse CaddyUI's ``/servers`` page into ``{id: {name, admin_url, status, ...}}``.

    Pure and I/O-free so both markup generations can be pinned by fixtures. Rows that don't
    link to ``/servers/{id}/…`` (the header, the empty-state row) are skipped, as are the
    responsive mobile cards — they use ``<article>``/``<div>``, not ``<tr>``, so they never
    produce duplicate ids.
    """
    out: dict[int, dict[str, Any]] = {}
    for row in _ROW_RE.findall(html):
        parsed = _parse_server_row(row)
        if parsed is not None:
            out[parsed[0]] = parsed[1]
    return out
