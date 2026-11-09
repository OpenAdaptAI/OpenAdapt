"""Tests for the MockLoan demo app: server, screens, and the fault boundary.

Runs headless chromium against a localhost ephemeral-port server. Mirrors
``tests/test_mockmed.py`` for the non-healthcare loan-origination target.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Iterator

import pytest
import requests
from playwright.sync_api import Browser, Page, sync_playwright

from openadapt_flow.mockloan.fault_server import serve as fault_serve
from openadapt_flow.mockloan.server import serve

MEMO = "Funding released per approval, first advance"


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    url, stop = serve(port=0)
    yield url
    stop()


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def page(browser: Browser) -> Iterator[Page]:
    page = browser.new_page(
        viewport={"width": 1280, "height": 800}, device_scale_factor=1
    )
    yield page
    page.close()


def login(page: Page, url: str) -> None:
    """Sign in and land on the pipeline screen."""
    page.goto(url)
    page.fill("#username", "officer.demo")
    page.fill("#password", "mockloan-demo-pass")
    page.click("#signin")
    page.wait_for_selector("#pipeline-table")


def open_first_loan(page: Page) -> None:
    page.locator(".open-btn").first.click()
    page.wait_for_selector("#new-disbursement")


def goto_disburse(page: Page) -> None:
    page.click("#new-disbursement")
    page.wait_for_selector("#authorize")


def test_server_serves_index(server_url: str) -> None:
    with urllib.request.urlopen(server_url, timeout=5) as resp:
        body = resp.read().decode("utf-8")
    assert "MockLoan" in body
    assert "app.js" in body


def test_no_external_resources(server_url: str) -> None:
    """The app must reference no off-host URLs (localhost-only guarantee)."""
    for name in ("index.html", "app.js", "styles.css"):
        with urllib.request.urlopen(server_url + name, timeout=5) as resp:
            body = resp.read().decode("utf-8")
        assert "http://" not in body.replace(server_url, "")
        assert "https://" not in body


def test_login_to_pipeline(page: Page, server_url: str) -> None:
    login(page, server_url)
    assert page.locator("#pipeline-table").count() == 1
    # Three seeded loans in the pipeline.
    assert page.locator(".open-btn").count() == 3


def test_disburse_shows_authorized_banner(page: Page, server_url: str) -> None:
    login(page, server_url)
    open_first_loan(page)
    goto_disburse(page)
    page.click("#product-personal")
    page.fill("#memo", MEMO)
    page.click("#authorize")
    page.wait_for_selector("#authorized-banner")
    banner = page.locator("#authorized-banner").inner_text()
    assert "authorized" in banner.lower()


class TestFaultBoundary:
    """The flag-gated ?fault hook: inert when off, active when on."""

    @pytest.fixture()
    def fault_url(self) -> Iterator[str]:
        url, _db, stop = fault_serve(port=0)
        yield url
        stop()

    def _authorize(self, page: Page, base_url: str, query: str) -> None:
        page.goto(base_url + query)
        page.fill("#username", "officer.demo")
        page.fill("#password", "mockloan-demo-pass")
        page.click("#signin")
        page.wait_for_selector("#pipeline-table")
        page.locator(".open-btn").first.click()
        page.wait_for_selector("#new-disbursement")
        page.click("#new-disbursement")
        page.wait_for_selector("#authorize")
        page.click("#product-personal")
        page.fill("#memo", MEMO)
        page.click("#authorize")

    def test_off_state_never_calls_the_api(self, page: Page, fault_url: str) -> None:
        """With no ?fault query the app never touches the ledger boundary."""
        requests.post(
            fault_url + "api/reset", json={"seed_concurrent": False}, timeout=10
        )
        self._authorize(page, fault_url, "")
        page.wait_for_selector("#authorized-banner")
        snap = requests.get(fault_url + "api/db", timeout=10).json()
        assert snap["records"] == [], "no ?fault => the API must never be called"

    def test_ok_fault_books_one_row(self, page: Page, fault_url: str) -> None:
        requests.post(
            fault_url + "api/reset", json={"seed_concurrent": False}, timeout=10
        )
        self._authorize(page, fault_url, "?fault=ok")
        page.wait_for_selector("#authorized-banner")
        snap = requests.get(fault_url + "api/db", timeout=10).json()
        assert len(snap["records"]) == 1
        rec = snap["records"][0]
        assert rec["loan_id"] == "L1001"
        assert rec["product"] == "Personal"
        assert rec["memo"] == MEMO
