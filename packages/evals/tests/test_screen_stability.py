"""Tests for screen stability detection (compare_screenshots, wait_for_stable_screen)."""

import io
from unittest.mock import patch

from PIL import Image

from openadapt_evals.infrastructure.screen_stability import (
    compare_screenshots as _compare_screenshots,
)
from openadapt_evals.infrastructure.screen_stability import (
    wait_for_stable_screen as _wait_for_stable_screen,
)


def _make_png(width: int = 100, height: int = 100, color: tuple = (0, 0, 0)) -> bytes:
    """Create a solid-color PNG image as bytes."""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_png_with_diff(
    width: int = 100,
    height: int = 100,
    base_color: tuple = (0, 0, 0),
    diff_pixels: int = 0,
    diff_color: tuple = (255, 255, 255),
) -> bytes:
    """Create a PNG with some pixels changed from the base color."""
    img = Image.new("RGB", (width, height), base_color)
    pixels = img.load()
    changed = 0
    for y in range(height):
        for x in range(width):
            if changed >= diff_pixels:
                break
            pixels[x, y] = diff_color
            changed += 1
        if changed >= diff_pixels:
            break
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestCompareScreenshots:
    """Tests for _compare_screenshots()."""

    def test_identical_images_return_1(self):
        """Identical images have similarity 1.0."""
        png = _make_png(color=(128, 128, 128))
        assert _compare_screenshots(png, png) == 1.0

    def test_completely_different_images_return_0(self):
        """Completely different images have similarity 0.0."""
        a = _make_png(color=(0, 0, 0))
        b = _make_png(color=(255, 255, 255))
        assert _compare_screenshots(a, b) == 0.0

    def test_different_sizes_return_0(self):
        """Images of different sizes return 0.0."""
        a = _make_png(width=100, height=100)
        b = _make_png(width=200, height=200)
        assert _compare_screenshots(a, b) == 0.0

    def test_small_diff_high_similarity(self):
        """A few changed pixels still yield high similarity."""
        total = 100 * 100  # 10000 pixels
        diff_pixels = 10  # 0.1% different
        a = _make_png(color=(0, 0, 0))
        b = _make_png_with_diff(diff_pixels=diff_pixels, diff_color=(255, 255, 255))
        similarity = _compare_screenshots(a, b)
        expected = (total - diff_pixels) / total  # 0.999
        assert abs(similarity - expected) < 0.001

    def test_half_different(self):
        """50% different pixels gives ~0.5 similarity."""
        total = 100 * 100
        a = _make_png(color=(0, 0, 0))
        b = _make_png_with_diff(diff_pixels=total // 2, diff_color=(255, 255, 255))
        similarity = _compare_screenshots(a, b)
        assert 0.49 < similarity < 0.51

    def test_clock_area_diff_above_threshold(self):
        """Simulated clock-area change (0.3% of pixels) stays above 99.5%."""
        clock_pixels = 3000  # ~0.3%
        a = _make_png(width=1000, height=1000, color=(50, 50, 50))
        b = _make_png_with_diff(
            width=1000, height=1000,
            base_color=(50, 50, 50),
            diff_pixels=clock_pixels,
            diff_color=(200, 200, 200),
        )
        similarity = _compare_screenshots(a, b)
        assert similarity >= 0.995, f"Clock-area diff should be above threshold: {similarity}"


class TestWaitForStableScreen:
    """Tests for _wait_for_stable_screen()."""

    def test_immediate_stability(self):
        """Returns quickly when screen is immediately stable."""
        stable_png = _make_png(color=(100, 100, 100))

        with patch(
            "openadapt_evals.infrastructure.screen_stability._take_screenshot",
            return_value=stable_png,
        ), patch("time.sleep"):
            result = _wait_for_stable_screen(
                "http://fake",
                poll_interval=0.01,
                stability_timeout=5,
                required_stable_checks=2,
            )

        assert result == stable_png

    def test_stabilizes_after_changes(self):
        """Returns stable screenshot after initial instability."""
        changing = _make_png(color=(255, 0, 0))
        changing2 = _make_png(color=(0, 255, 0))
        stable = _make_png(color=(0, 0, 255))

        screenshots = [changing, changing2, stable, stable, stable]
        call_count = [0]

        def mock_screenshot(server):
            idx = min(call_count[0], len(screenshots) - 1)
            call_count[0] += 1
            return screenshots[idx]

        with patch(
            "openadapt_evals.infrastructure.screen_stability._take_screenshot",
            side_effect=mock_screenshot,
        ), patch("time.sleep"):
            result = _wait_for_stable_screen(
                "http://fake",
                poll_interval=0.01,
                stability_timeout=30,
                required_stable_checks=2,
            )

        assert result == stable

    def test_timeout_returns_last_screenshot(self):
        """Returns last screenshot on timeout with warning."""
        call_count = [0]

        def mock_screenshot(server):
            """Return a different image each time to prevent stability."""
            call_count[0] += 1
            return _make_png(color=(call_count[0] % 256, 0, 0))

        with patch(
            "openadapt_evals.infrastructure.screen_stability._take_screenshot",
            side_effect=mock_screenshot,
        ), patch("time.sleep"), patch("time.time") as mock_time:
            # Simulate timeout: first call returns 0, subsequent calls exceed deadline
            times = iter([0.0, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 100.0])
            mock_time.side_effect = lambda: next(times)

            result = _wait_for_stable_screen(
                "http://fake",
                poll_interval=0.01,
                stability_timeout=2.0,
                required_stable_checks=2,
            )

        assert result is not None
        assert len(result) > 0

    def test_minor_diff_treated_as_stable(self):
        """Minor pixel differences (below threshold) count as stable."""
        base = _make_png(width=100, height=100, color=(50, 50, 50))
        # 2 pixels different out of 10000 = 0.02% diff → 99.98% similar
        slight_diff = _make_png_with_diff(
            width=100, height=100,
            base_color=(50, 50, 50),
            diff_pixels=2,
            diff_color=(51, 51, 51),
        )

        screenshots = [base, slight_diff, slight_diff, slight_diff]
        call_count = [0]

        def mock_screenshot(server):
            idx = min(call_count[0], len(screenshots) - 1)
            call_count[0] += 1
            return screenshots[idx]

        with patch(
            "openadapt_evals.infrastructure.screen_stability._take_screenshot",
            side_effect=mock_screenshot,
        ), patch("time.sleep"):
            result = _wait_for_stable_screen(
                "http://fake",
                poll_interval=0.01,
                stability_timeout=10,
                similarity_threshold=0.995,
                required_stable_checks=2,
            )

        assert result == slight_diff
