from __future__ import annotations

import os

import pytest


@pytest.fixture
def live_settings() -> tuple[str, str]:
    """(url, token) for a real CaddyUI instance, or skip the test.

    Set ``CADDYUI_URL`` and ``CADDYUI_TOKEN`` to run the live smoke tests, e.g.::

        CADDYUI_URL=https://caddyui.example.com CADDYUI_TOKEN=cadu_... pytest -m live
    """
    url = os.environ.get("CADDYUI_URL")
    token = os.environ.get("CADDYUI_TOKEN")
    if not url or not token:
        pytest.skip("CADDYUI_URL / CADDYUI_TOKEN not set; skipping live CaddyUI test")
    return url, token
