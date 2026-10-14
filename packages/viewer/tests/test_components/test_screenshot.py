"""Tests for screenshot component."""


from openadapt_viewer.components import screenshot_display


class TestScreenshotDisplay:
    """Tests for screenshot_display component."""

    def test_basic_screenshot(self):
        """Test basic screenshot display."""
        html = screenshot_display("test.png")
        assert "oa-screenshot-container" in html
        assert 'src="test.png"' in html

    def test_screenshot_with_dimensions(self):
        """Test screenshot with custom dimensions."""
        html = screenshot_display("test.png", width=400, height=300)
        assert "width: 400px" in html
        assert "height: 300px" in html

    def test_screenshot_with_overlays(self):
        """Test screenshot with click overlays."""
        overlays = [
            {"type": "click", "x": 0.5, "y": 0.5, "label": "H", "variant": "human"},
        ]
        html = screenshot_display("test.png", overlays=overlays)
        assert "oa-overlay" in html
        assert "oa-overlay-click" in html
        assert "oa-overlay-human" in html
        assert "left: 50.0%" in html
        assert "top: 50.0%" in html
        assert ">H<" in html

    def test_screenshot_with_caption(self):
        """Test screenshot with caption."""
        html = screenshot_display("test.png", caption="Test Caption")
        assert "oa-screenshot-caption" in html
        assert "Test Caption" in html

    def test_screenshot_placeholder(self):
        """Test screenshot placeholder when no image."""
        html = screenshot_display(image_path=None)
        assert "oa-screenshot-placeholder" in html
        assert "No screenshot available" in html

    def test_screenshot_custom_placeholder(self):
        """Test screenshot with custom placeholder text."""
        html = screenshot_display(image_path=None, placeholder_text="Image coming soon")
        assert "Image coming soon" in html

    def test_screenshot_with_class_name(self):
        """Test screenshot with additional CSS class."""
        html = screenshot_display("test.png", class_name="custom-class")
        assert "custom-class" in html

    def test_screenshot_embed_image_missing_file(self):
        """Test embed mode with missing file."""
        html = screenshot_display("/nonexistent/path.png", embed_image=True)
        assert "Image not found" in html

    def test_screenshot_embed_image_real_file(self, tmp_path):
        """Test embed mode with real PNG file."""
        # Create a minimal valid PNG file
        png_data = bytes([
            0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,  # PNG signature
            0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,  # IHDR chunk
            0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
            0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
            0xDE, 0x00, 0x00, 0x00, 0x0C, 0x49, 0x44, 0x41,
            0x54, 0x08, 0xD7, 0x63, 0xF8, 0x00, 0x00, 0x00,
            0x01, 0x00, 0x01, 0x00, 0x05, 0xFE, 0xD4, 0xAA,
            0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44,
            0xAE, 0x42, 0x60, 0x82,  # IEND chunk
        ])

        png_file = tmp_path / "test.png"
        png_file.write_bytes(png_data)

        html = screenshot_display(str(png_file), embed_image=True)
        assert "data:image/png;base64," in html

    def test_screenshot_multiple_overlays(self):
        """Test screenshot with multiple overlays."""
        overlays = [
            {"type": "click", "x": 0.2, "y": 0.3, "label": "H", "variant": "human"},
            {"type": "click", "x": 0.7, "y": 0.8, "label": "AI", "variant": "predicted"},
        ]
        html = screenshot_display("test.png", overlays=overlays)
        assert html.count("oa-overlay-click") == 2
        assert "oa-overlay-human" in html
        assert "oa-overlay-predicted" in html
        assert ">H<" in html
        assert ">AI<" in html

    def test_screenshot_box_overlay(self):
        """Test screenshot with box overlay."""
        overlays = [
            {"type": "box", "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
        ]
        html = screenshot_display("test.png", overlays=overlays)
        assert "oa-overlay-box" in html
        assert "width: 30.0%" in html
        assert "height: 40.0%" in html
