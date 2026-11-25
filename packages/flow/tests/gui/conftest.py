"""A headless browser page for the GUI mechanism tests."""

from __future__ import annotations

from typing import Any, Iterator

import pytest


@pytest.fixture(scope="module")
def _browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(_browser: Any) -> Iterator[Any]:
    context = _browser.new_context()
    try:
        yield context.new_page()
    finally:
        context.close()
