"""Tests for the screenshot generation script.

These tests verify the scenario builder and metadata generation
without requiring Playwright. The actual screenshot generation
is tested separately (requires `uv run playwright install chromium`).

Run:
    uv run pytest tests/test_e2e/test_screenshots.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add scripts/ to path for importing
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from generate_screenshots import VIEWPORTS, build_scenarios


class TestViewports:
    """Tests for viewport configurations."""

    def test_desktop_viewport(self) -> None:
        """Desktop viewport should be 1280x800."""
        vp = VIEWPORTS["desktop"]
        assert vp.width == 1280
        assert vp.height == 800

    def test_tablet_viewport(self) -> None:
        """Tablet viewport should be 768x1024."""
        vp = VIEWPORTS["tablet"]
        assert vp.width == 768
        assert vp.height == 1024

    def test_mobile_viewport(self) -> None:
        """Mobile viewport should be 375x667."""
        vp = VIEWPORTS["mobile"]
        assert vp.width == 375
        assert vp.height == 667


class TestScenarios:
    """Tests for screenshot scenario builder."""

    def test_scenarios_not_empty(self) -> None:
        """build_scenarios should return at least one scenario."""
        scenarios = build_scenarios()
        assert len(scenarios) > 0

    def test_all_scenarios_have_required_fields(self) -> None:
        """Every scenario must have name, description, page, and viewport."""
        for s in build_scenarios():
            assert s.name, "Scenario missing name"
            assert s.description, f"Scenario {s.name} missing description"
            assert s.page, f"Scenario {s.name} missing page"
            assert s.viewport is not None, f"Scenario {s.name} missing viewport"

    def test_scenarios_cover_all_pages(self) -> None:
        """Scenarios should cover all three HTML pages."""
        pages = {s.page for s in build_scenarios()}
        assert "index.html" in pages
        assert "review.html" in pages
        assert "settings.html" in pages

    def test_scenario_names_are_unique(self) -> None:
        """Scenario names must be unique."""
        names = [s.name for s in build_scenarios()]
        assert len(names) == len(set(names))

    def test_scenarios_have_sort_order(self) -> None:
        """Scenario names should start with a number for sorting."""
        for s in build_scenarios():
            assert s.name[:2].isdigit(), f"Scenario {s.name} doesn't start with a number"
