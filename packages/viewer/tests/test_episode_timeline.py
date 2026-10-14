"""
Tests for the Episode Timeline component.

This test suite verifies the JavaScript-based Episode Timeline component
using Playwright for browser automation.

NOTE: These tests require:
1. Playwright browsers installed: `uv run playwright install chromium`
2. A local server running at localhost:8080: `uv run python -m http.server 8080`

Run with: pytest tests/test_episode_timeline.py -m playwright
Skip with: pytest -m "not playwright"
"""


import pytest
from playwright.sync_api import Page, expect

# Mark all tests in this module as requiring playwright
pytestmark = [
    pytest.mark.playwright,
    pytest.mark.skip(reason="Requires localhost:8080 server - run manually with `python -m http.server 8080`"),
]


# Test data matching test_episodes.json
TEST_EPISODES = [
    {
        "episode_id": "episode_001",
        "name": "Navigate to System Settings",
        "description": "User opens System Settings application from the dock and navigates to the Displays section.",
        "start_time": 0.0,
        "end_time": 3.5,
        "duration": 3.5,
        "steps": [
            "Click System Settings icon in dock",
            "Wait for Settings window to open",
            "Click on Displays in sidebar"
        ],
        "boundary_confidence": 0.92
    },
    {
        "episode_id": "episode_002",
        "name": "Disable Night Shift",
        "description": "User scrolls down to find Night Shift settings and toggles it off.",
        "start_time": 3.5,
        "end_time": 6.7,
        "duration": 3.2,
        "steps": [
            "Scroll down in Displays settings",
            "Click on Night Shift option",
            "Toggle Night Shift switch to off position"
        ],
        "boundary_confidence": 0.95
    }
]


@pytest.fixture
def test_page_url():
    """Return the URL to the test page."""
    return "http://localhost:8080/test_episode_timeline.html"


class TestEpisodeTimelineComponent:
    """Test suite for Episode Timeline component."""

    def test_timeline_renders_successfully(self, page: Page, test_page_url: str):
        """Test that the timeline component renders without errors."""
        page.goto(test_page_url)

        # Wait for timeline to initialize
        page.wait_for_selector(".oa-episode-timeline", timeout=5000)

        # Check that main container exists
        timeline = page.locator(".oa-episode-timeline")
        expect(timeline).to_be_visible()

    def test_episode_labels_displayed(self, page: Page, test_page_url: str):
        """Test that episode labels are displayed correctly."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-episode-label", timeout=5000)

        # Should have 2 episode labels
        labels = page.locator(".oa-episode-label")
        expect(labels).to_have_count(2)

        # Check episode names
        first_label = labels.nth(0)
        expect(first_label).to_contain_text("Navigate to System Settings")

        second_label = labels.nth(1)
        expect(second_label).to_contain_text("Disable Night Shift")

    def test_episode_boundaries_visible(self, page: Page, test_page_url: str):
        """Test that episode boundaries are visible."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-episode-boundary", timeout=5000)

        # Should have 1 boundary (between 2 episodes)
        boundaries = page.locator(".oa-episode-boundary")
        expect(boundaries).to_have_count(1)

    def test_current_episode_indicator(self, page: Page, test_page_url: str):
        """Test that current episode indicator updates correctly."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-episode-current-indicator", timeout=5000)

        # Initially should show episode 1
        indicator = page.locator(".oa-episode-current-indicator")
        expect(indicator).to_contain_text("Episode")
        expect(indicator).to_contain_text("1")
        expect(indicator).to_contain_text("of")
        expect(indicator).to_contain_text("2")

    def test_click_episode_label_navigates(self, page: Page, test_page_url: str):
        """Test that clicking an episode label triggers navigation."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-episode-label", timeout=5000)

        # Get initial time
        initial_time = page.locator("#current-time").text_content()

        # Click second episode label
        second_label = page.locator(".oa-episode-label").nth(1)
        second_label.click()

        # Wait for time to update
        page.wait_for_timeout(500)

        # Time should have changed to episode 2 start time (3.5s)
        current_time = page.locator("#current-time").text_content()
        assert current_time != initial_time
        assert "3.5" in current_time

    def test_prev_next_buttons(self, page: Page, test_page_url: str):
        """Test that prev/next episode buttons work."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-episode-nav-btn", timeout=5000)

        # Initially at start, prev button should be disabled
        prev_btn = page.locator(".oa-episode-nav-btn[data-action='prev']")
        expect(prev_btn).to_be_disabled()

        # Click next button
        next_btn = page.locator(".oa-episode-nav-btn[data-action='next']")
        next_btn.click()

        # Wait for navigation
        page.wait_for_timeout(500)

        # Now prev button should be enabled
        expect(prev_btn).to_be_enabled()

        # Time should be at episode 2 start
        current_time = page.locator("#current-time").text_content()
        assert "3.5" in current_time

    def test_keyboard_shortcuts(self, page: Page, test_page_url: str):
        """Test keyboard shortcuts for episode navigation."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-episode-timeline", timeout=5000)

        # Press right arrow to go to next episode
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(500)

        # Should be at episode 2
        current_time = page.locator("#current-time").text_content()
        assert "3.5" in current_time

        # Press left arrow to go back
        page.keyboard.press("ArrowLeft")
        page.wait_for_timeout(500)

        # Should be back at episode 1
        current_time = page.locator("#current-time").text_content()
        assert "0.0" in current_time

    def test_timeline_track_click_seeks(self, page: Page, test_page_url: str):
        """Test that clicking the timeline track seeks to that position."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-timeline-track", timeout=5000)

        track = page.locator(".oa-timeline-track")

        # Click in the middle of the track
        track.click(position={"x": 100, "y": 4})
        page.wait_for_timeout(500)

        # Time should have updated
        current_time = page.locator("#current-time").text_content()
        # Time should be greater than 0
        time_value = float(current_time.replace("s", ""))
        assert time_value > 0

    def test_current_marker_position_updates(self, page: Page, test_page_url: str):
        """Test that the current position marker updates during playback."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-current-marker", timeout=5000)

        # Get initial marker position
        marker = page.locator(".oa-current-marker")
        initial_style = marker.get_attribute("style")

        # Jump to middle of timeline
        page.evaluate("jumpToTime(3.0)")
        page.wait_for_timeout(500)

        # Marker position should have changed
        new_style = marker.get_attribute("style")
        assert initial_style != new_style

    def test_episode_hover_shows_tooltip(self, page: Page, test_page_url: str):
        """Test that hovering over an episode label shows the tooltip."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-episode-label", timeout=5000)

        # Hover over first episode
        first_label = page.locator(".oa-episode-label").nth(0)
        first_label.hover()

        # Wait for tooltip
        page.wait_for_timeout(500)

        # Tooltip should be visible
        tooltip = page.locator(".oa-episode-tooltip")
        expect(tooltip).to_be_visible()

        # Tooltip should contain episode description
        expect(tooltip).to_contain_text("Navigate to System Settings")
        expect(tooltip).to_contain_text("3.5s")

    def test_episode_segments_in_timeline(self, page: Page, test_page_url: str):
        """Test that episode segments are rendered in the timeline track."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-episode-segment", timeout=5000)

        # Should have 2 segments
        segments = page.locator(".oa-episode-segment")
        expect(segments).to_have_count(2)

    def test_current_episode_highlighted(self, page: Page, test_page_url: str):
        """Test that the current episode is highlighted."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-episode-label", timeout=5000)

        # Jump to episode 2
        page.evaluate("jumpToTime(4.0)")
        page.wait_for_timeout(500)

        # Second episode label should have current class
        second_label = page.locator(".oa-episode-label").nth(1)
        class_attr = second_label.get_attribute("class")
        assert "oa-episode-current" in class_attr

    def test_simulate_playback(self, page: Page, test_page_url: str):
        """Test the simulate playback functionality."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-episode-timeline", timeout=5000)

        # Click simulate playback button
        page.get_by_text("Simulate Playback").click()

        # Wait a bit for playback
        page.wait_for_timeout(1000)

        # Time should have advanced
        current_time = page.locator("#current-time").text_content()
        time_value = float(current_time.replace("s", ""))
        assert time_value > 0

    def test_episode_durations_displayed(self, page: Page, test_page_url: str):
        """Test that episode durations are displayed in labels."""
        page.goto(test_page_url)
        page.wait_for_selector(".oa-episode-label-duration", timeout=5000)

        # Check durations
        durations = page.locator(".oa-episode-label-duration")
        expect(durations).to_have_count(2)

        # First episode is 3.5s
        first_duration = durations.nth(0)
        expect(first_duration).to_contain_text("3.5s")

        # Second episode is 3.2s
        second_duration = durations.nth(1)
        expect(second_duration).to_contain_text("3.2s")

    def test_responsive_on_mobile(self, page: Page, test_page_url: str):
        """Test that timeline is responsive on mobile viewport."""
        # Set mobile viewport
        page.set_viewport_size({"width": 375, "height": 667})

        page.goto(test_page_url)
        page.wait_for_selector(".oa-episode-timeline", timeout=5000)

        # Timeline should still be visible
        timeline = page.locator(".oa-episode-timeline")
        expect(timeline).to_be_visible()

        # Navigation buttons should show only icons on mobile
        # (text hidden via CSS)
        nav_btns = page.locator(".oa-episode-nav-btn")
        expect(nav_btns).to_have_count(2)


class TestEpisodeTimelineIntegration:
    """Integration tests for Episode Timeline in capture viewer."""

    def test_capture_viewer_loads_episodes(self, page: Page):
        """Test that capture viewer loads episodes successfully."""
        page.goto("http://localhost:8080/capture_viewer.html")

        # Wait for page to load
        page.wait_for_load_state("networkidle", timeout=10000)

        # Check console for episode loading message
        messages = []

        def handle_console(msg):
            messages.append(msg.text)

        page.on("console", handle_console)

        # Reload to capture console messages
        page.reload()
        page.wait_for_timeout(2000)

        # Should have logged episode loading
        assert any("episodes" in msg.lower() for msg in messages)

    def test_capture_viewer_timeline_integration(self, page: Page):
        """Test that timeline integrates with capture viewer playback."""
        page.goto("http://localhost:8080/capture_viewer.html")
        page.wait_for_load_state("networkidle", timeout=10000)

        # Timeline should be visible if episodes loaded
        timeline = page.locator("#episode-timeline-container")

        # If episodes loaded, verify timeline
        if timeline.is_visible():
            # Should have episode labels
            labels = page.locator(".oa-episode-label")
            expect(labels).to_have_count(2)

            # Click next step button
            next_btn = page.locator("button").filter(has_text="Play")
            if next_btn.is_visible():
                next_btn.click()
                page.wait_for_timeout(1000)

                # Timeline should update


@pytest.mark.slow
class TestEpisodeTimelinePerformance:
    """Performance tests for Episode Timeline."""

    def test_timeline_renders_with_many_episodes(self, page: Page, test_page_url: str):
        """Test performance with many episodes."""
        # This would require injecting many episodes into the test page
        # For now, just verify current implementation is fast
        page.goto(test_page_url)

        # Should render within 2 seconds
        page.wait_for_selector(".oa-episode-timeline", timeout=2000)

        # Should be responsive to interactions
        first_label = page.locator(".oa-episode-label").nth(0)
        first_label.click()

        # Should respond within 500ms
        page.wait_for_timeout(500)
        current_time = page.locator("#current-time").text_content()
        assert "0.0" in current_time


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
