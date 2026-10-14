"""Tests for playback controls component."""


from openadapt_viewer.components import playback_controls


class TestPlaybackControls:
    """Tests for playback_controls component."""

    def test_basic_playback(self):
        """Test basic playback controls."""
        html = playback_controls(step_count=10)
        assert "oa-playback-controls" in html
        assert "Step 1 of 10" in html

    def test_playback_with_initial_step(self):
        """Test playback starting at specific step."""
        html = playback_controls(step_count=10, initial_step=5)
        assert "Step 6 of 10" in html

    def test_playback_with_custom_speeds(self):
        """Test playback with custom speed options."""
        html = playback_controls(step_count=5, speeds=[0.25, 0.5, 1, 3])
        assert '0.25x' in html
        assert '3x' in html

    def test_playback_buttons(self):
        """Test that all playback buttons are present."""
        html = playback_controls(step_count=5)
        # Should have rewind, prev, play/pause, next, end buttons
        assert html.count("oa-playback-btn") >= 5
        # Check for SVG icons
        assert "viewBox" in html

    def test_playback_speed_selector(self):
        """Test speed selector is present."""
        html = playback_controls(step_count=5)
        assert "oa-playback-speed" in html
        assert "<select" in html
        assert "<option" in html

    def test_playback_without_counter(self):
        """Test playback without step counter."""
        html = playback_controls(step_count=5, show_step_counter=False)
        assert "oa-playback-counter" not in html

    def test_playback_with_class_name(self):
        """Test playback with additional CSS class."""
        html = playback_controls(step_count=5, class_name="custom-playback")
        assert "custom-playback" in html

    def test_playback_alpine_bindings(self):
        """Test that Alpine.js bindings are present."""
        html = playback_controls(step_count=5)
        assert "x-data=" in html
        assert "@click=" in html
        assert ":disabled=" in html
        assert "@keydown" in html

    def test_playback_keyboard_shortcuts(self):
        """Test keyboard shortcut bindings."""
        html = playback_controls(step_count=5)
        assert "@keydown.space" in html
        assert "@keydown.left" in html
        assert "@keydown.right" in html
        assert "@keydown.home" in html
        assert "@keydown.end" in html
