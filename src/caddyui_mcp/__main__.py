import asyncio
import logging
import os
import time

from caddyui_mcp.client import get_client
from caddyui_mcp.server import mcp

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

_MAX_RETRIES = 5
_RETRY_DELAY = 5  # seconds between retries

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(format=LOG_FORMAT, level=getattr(logging, level, logging.INFO))


def _check_connectivity() -> None:
    """Verify connectivity + auth to CaddyUI, retrying on failure.

    Uses ``GET /api/v1/proxy-hosts`` — a cheap authenticated read that returns 200 with a
    valid token (even on an empty instance) and surfaces a bad/missing token as an error.
    """

    async def _probe() -> int:
        client = get_client()
        hosts = await client.list_proxy_hosts()
        return len(hosts) if isinstance(hosts, list) else 0

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            count = asyncio.run(_probe())
            logger.info("Connected to CaddyUI (%d proxy host(s) visible).", count)
            return
        except Exception as e:
            logger.error(
                "Failed to connect to CaddyUI (attempt %d/%d): %s", attempt, _MAX_RETRIES, e
            )
            if attempt < _MAX_RETRIES:
                logger.info("Retrying in %d seconds...", _RETRY_DELAY)
                time.sleep(_RETRY_DELAY)
    raise SystemExit(1)


def main() -> None:
    _configure_logging()
    _check_connectivity()
    mcp.run()


def main_web() -> None:
    _configure_logging()
    _check_connectivity()
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("MCP_HTTP_PORT", "8080")))


if __name__ == "__main__":
    main()
