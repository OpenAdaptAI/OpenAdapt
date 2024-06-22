"""Package for interacting with browser.

Module: openadapt/browser/__init__.py
"""

import asyncio  # noqa
import websockets
from typing import Any, Dict

from loguru import logger
from openadapt.config import config


async def fetch_browser_data() -> Dict[str, Any]:
    """Fetch browser data from WebSocket server."""
    logger.info("here 005")
    async with websockets.connect("ws://localhost:8765") as websocket:
        data = await websocket.recv()
        return {"browser_data": data}


async def get_active_browser_data(
    include_browser_data: bool = config.RECORD_BROWSER_DATA,
    browser_data: dict[str, Any] = {},
) -> dict[str, Any] | None:
    """Get data of the active chrome browser.

    Params:
        include_browser_data (bool): A boolean indicating
        whether to include browser data.
        browser_data (dict): A dictionary containing
        information about the active browser

    Returns:
        dict or None: A dictionary containing information about the active browser,
        or None if the state is not available.

    """
    logger.info("here 006")
    if include_browser_data:
        try:
            browser_data = await fetch_browser_data()
        except Exception as e:
            logger.error(f"Failed to fetch browser data: {e}")
    return browser_data
