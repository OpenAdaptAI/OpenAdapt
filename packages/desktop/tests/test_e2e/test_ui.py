"""UI tests for the WebView HTML pages using Playwright.

These tests verify that the HTML pages render correctly and that
interactive elements are present. They do NOT require the Tauri shell
or Python sidecar -- they test the HTML/CSS/JS in isolation.

Requires: playwright (install via `uv run playwright install chromium`)

Run:
    uv run pytest tests/test_e2e/test_ui.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _playwright_available() -> bool:
    """Check if playwright is available."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


# Skip entire module if playwright not installed
pytestmark = pytest.mark.skipif(
    not _playwright_available(),
    reason="Playwright not installed (uv sync --extra dev && uv run playwright install chromium)",
)


@pytest.fixture(scope="module")
def browser():
    """Launch a headless Chromium browser for the test module."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    b = pw.chromium.launch(headless=True)
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def page(browser, src_dir: Path):
    """Create a new browser page with default viewport."""
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    p = context.new_page()
    yield p
    context.close()


def load_page(page, src_dir: Path, filename: str):
    """Load an HTML file into the page."""
    file_path = src_dir / filename
    assert file_path.exists(), f"{file_path} not found"
    page.goto(f"file://{file_path.resolve()}")
    page.wait_for_load_state("networkidle")


class TestDashboard:
    """Tests for the main dashboard (index.html)."""

    def test_dashboard_renders(self, page, src_dir: Path) -> None:
        """Dashboard page should render without errors."""
        load_page(page, src_dir, "index.html")
        assert page.title() == "OpenAdapt Desktop"

    def test_dashboard_has_status_section(self, page, src_dir: Path) -> None:
        """Dashboard should show a status section."""
        load_page(page, src_dir, "index.html")
        status = page.locator("#status")
        assert status.is_visible()
        assert "Idle" in status.text_content()

    def test_dashboard_has_record_button(self, page, src_dir: Path) -> None:
        """Dashboard should have a Start Recording button."""
        load_page(page, src_dir, "index.html")
        btn = page.locator("#btn-record")
        assert btn.is_visible()
        assert "Start Recording" in btn.text_content()

    def test_dashboard_has_navigation(self, page, src_dir: Path) -> None:
        """Dashboard should have Settings and Review buttons."""
        load_page(page, src_dir, "index.html")
        buttons = page.locator(".btn-secondary")
        assert buttons.count() >= 2

    def test_dashboard_has_captures_list(self, page, src_dir: Path) -> None:
        """Dashboard should have a captures list section."""
        load_page(page, src_dir, "index.html")
        captures = page.locator("#captures-list")
        assert captures.is_visible()

    def test_no_console_errors(self, page, src_dir: Path) -> None:
        """Dashboard should load without JavaScript errors."""
        errors = []
        page.on("console", lambda msg: errors.append(msg) if msg.type == "error" else None)
        load_page(page, src_dir, "index.html")
        assert len(errors) == 0, f"Console errors: {errors}"


class TestReviewPage:
    """Tests for the upload review page (review.html)."""

    def test_review_renders(self, page, src_dir: Path) -> None:
        """Review page should render without errors."""
        load_page(page, src_dir, "review.html")
        assert "Upload Review" in page.title()

    def test_review_has_pending_list(self, page, src_dir: Path) -> None:
        """Review page should have a pending reviews list."""
        load_page(page, src_dir, "review.html")
        pending = page.locator("#pending-list")
        assert pending.is_visible()

    def test_review_has_action_buttons(self, page, src_dir: Path) -> None:
        """Review page should have Review All and Dismiss All buttons."""
        load_page(page, src_dir, "review.html")
        actions = page.locator("#review-actions")
        assert actions.is_visible()
        buttons = actions.locator("button")
        assert buttons.count() >= 2

    def test_review_has_back_link(self, page, src_dir: Path) -> None:
        """Review page should have a back link to the dashboard."""
        load_page(page, src_dir, "review.html")
        back = page.locator("a[href='index.html']")
        assert back.is_visible()


class TestSettingsPage:
    """Tests for the settings page (settings.html)."""

    def test_settings_renders(self, page, src_dir: Path) -> None:
        """Settings page should render without errors."""
        load_page(page, src_dir, "settings.html")
        assert "Settings" in page.title()
