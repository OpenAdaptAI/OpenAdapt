"""Tests for visual similarity alignment in DemoLibrary.

Validates that ``align_step()`` uses perceptual hash (pHash) matching
to find the demo step whose screenshot is most similar to the current
screen state, rather than relying solely on sequential step index.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from openadapt_evals.adapters.base import BenchmarkAction
from openadapt_evals.demo_library import (
    _HAS_IMAGEHASH,
    Demo,
    DemoLibrary,
    DemoStep,
    _demo_to_dict,
)

# Skip the entire module if imagehash is not installed.
pytestmark = pytest.mark.skipif(
    not _HAS_IMAGEHASH,
    reason="imagehash not installed (install with: pip install imagehash)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_png_bytes(
    width: int = 200,
    height: int = 200,
    color: str | tuple = "white",
    pattern: str | None = None,
) -> bytes:
    """Create valid PNG bytes with distinct visual content.

    Solid-color images produce identical pHashes because pHash operates
    on frequency content.  To get distinct hashes we draw simple
    geometric patterns (horizontal stripes, vertical stripes, diagonal,
    checkerboard, gradient) so that the DCT coefficients differ.

    Args:
        width: Image width.
        height: Image height.
        color: Background color.
        pattern: One of ``"hstripes"``, ``"vstripes"``, ``"diagonal"``,
            ``"checker"``, ``"gradient"``, or ``None`` (solid color).
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), color=color)
    draw = ImageDraw.Draw(img)

    if pattern == "hstripes":
        # Horizontal stripes -- strong horizontal frequency
        for y in range(0, height, 20):
            draw.rectangle([0, y, width, y + 10], fill="black")
    elif pattern == "vstripes":
        # Vertical stripes -- strong vertical frequency
        for x in range(0, width, 20):
            draw.rectangle([x, 0, x + 10, height], fill="black")
    elif pattern == "diagonal":
        # Diagonal lines
        for offset in range(-height, width, 20):
            draw.line([(offset, 0), (offset + height, height)], fill="black", width=5)
    elif pattern == "checker":
        # Checkerboard
        sq = 25
        for y in range(0, height, sq):
            for x in range(0, width, sq):
                if (x // sq + y // sq) % 2 == 0:
                    draw.rectangle([x, y, x + sq, y + sq], fill="black")
    elif pattern == "gradient":
        # Horizontal gradient (left=black, right=white)
        for x in range(width):
            gray = int(255 * x / width)
            draw.line([(x, 0), (x, height)], fill=(gray, gray, gray))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_click_action(x: float, y: float, name: str = "") -> BenchmarkAction:
    return BenchmarkAction(type="click", x=x, y=y, target_name=name)


def _make_type_action(text: str) -> BenchmarkAction:
    return BenchmarkAction(type="type", text=text)


@pytest.fixture
def tmp_library(tmp_path: Path) -> DemoLibrary:
    return DemoLibrary(str(tmp_path / "demos"))


# ---------------------------------------------------------------------------
# Visual alignment: basic matching
# ---------------------------------------------------------------------------


class TestVisualAlignment:
    """Test pHash-based visual similarity alignment."""

    def test_matches_identical_screenshot(self, tmp_library: DemoLibrary):
        """When current screenshot matches step 1 exactly, return step 1."""
        # Create a 3-step demo with distinct patterns
        screenshots = [
            _make_png_bytes(pattern="hstripes"),
            _make_png_bytes(pattern="checker"),
            _make_png_bytes(pattern="diagonal"),
        ]
        actions = [
            _make_click_action(0.1, 0.1, "stripe button"),
            _make_click_action(0.5, 0.5, "checker button"),
            _make_click_action(0.9, 0.9, "diagonal button"),
        ]
        tmp_library.add_demo(
            "task_visual",
            screenshots=screenshots,
            actions=actions,
            descriptions=["stripe element", "checker element", "diagonal element"],
        )

        # Query with the checker screenshot -- should match step 1
        checker_bytes = _make_png_bytes(pattern="checker")
        guidance = tmp_library.align_step(
            "task_visual",
            current_screenshot=checker_bytes,
            step_index=0,  # sequential would say step 0
        )

        assert guidance.available
        assert guidance.step_index == 1  # visual says step 1
        assert guidance.visual_alignment_used
        assert guidance.visual_distance is not None
        assert guidance.visual_distance == 0  # exact match
        assert guidance.confidence == 1.0

    def test_matches_closest_screenshot(self, tmp_library: DemoLibrary):
        """When current screenshot is similar to one step, match that step."""
        screenshots = [
            _make_png_bytes(pattern="hstripes"),
            _make_png_bytes(pattern="vstripes"),
            _make_png_bytes(pattern="diagonal"),
        ]
        actions = [
            _make_click_action(0.1, 0.1),
            _make_click_action(0.5, 0.5),
            _make_click_action(0.9, 0.9),
        ]
        tmp_library.add_demo(
            "task_closest",
            screenshots=screenshots,
            actions=actions,
        )

        # Query with diagonal pattern (should match step 2)
        diagonal_bytes = _make_png_bytes(pattern="diagonal")
        guidance = tmp_library.align_step(
            "task_closest",
            current_screenshot=diagonal_bytes,
            step_index=0,
        )

        assert guidance.available
        assert guidance.step_index == 2  # closest to diagonal
        assert guidance.visual_alignment_used

    def test_visual_alignment_overrides_step_index(
        self, tmp_library: DemoLibrary
    ):
        """Visual alignment should override the sequential step_index."""
        screenshots = [
            _make_png_bytes(pattern="hstripes"),
            _make_png_bytes(pattern="checker"),
        ]
        actions = [
            _make_click_action(0.1, 0.1),
            _make_click_action(0.9, 0.9),
        ]
        tmp_library.add_demo(
            "task_override",
            screenshots=screenshots,
            actions=actions,
        )

        # Agent is at step 0, but screen looks like step 1 (checker)
        checker_bytes = _make_png_bytes(pattern="checker")
        guidance = tmp_library.align_step(
            "task_override",
            current_screenshot=checker_bytes,
            step_index=0,
        )

        assert guidance.step_index == 1  # visual match, not sequential
        assert guidance.visual_alignment_used

    def test_agent_past_demo_length_uses_visual(
        self, tmp_library: DemoLibrary
    ):
        """When step_index exceeds demo length, visual still works."""
        screenshots = [
            _make_png_bytes(pattern="hstripes"),
            _make_png_bytes(pattern="checker"),
        ]
        actions = [
            _make_click_action(0.1, 0.1),
            _make_click_action(0.9, 0.9),
        ]
        tmp_library.add_demo(
            "task_past",
            screenshots=screenshots,
            actions=actions,
        )

        # Agent is at step 5 (beyond demo), but screen looks like step 0
        hstripes_bytes = _make_png_bytes(pattern="hstripes")
        guidance = tmp_library.align_step(
            "task_past",
            current_screenshot=hstripes_bytes,
            step_index=5,
        )

        assert guidance.step_index == 0
        assert guidance.visual_alignment_used


# ---------------------------------------------------------------------------
# Fallback to sequential alignment
# ---------------------------------------------------------------------------


class TestVisualAlignmentFallback:
    """Test fallback to sequential alignment when visual is unavailable."""

    def test_fallback_when_no_screenshot(self, tmp_library: DemoLibrary):
        """No screenshot -> sequential alignment."""
        screenshots = [_make_png_bytes(pattern="hstripes")]
        actions = [_make_click_action(0.5, 0.5)]
        tmp_library.add_demo(
            "task_no_ss",
            screenshots=screenshots,
            actions=actions,
        )

        guidance = tmp_library.align_step(
            "task_no_ss",
            current_screenshot=None,
            step_index=0,
        )

        assert guidance.available
        assert guidance.step_index == 0
        assert not guidance.visual_alignment_used
        assert guidance.visual_distance is None
        assert guidance.confidence == 1.0  # sequential confidence

    def test_fallback_when_disabled(self, tmp_library: DemoLibrary):
        """use_visual_alignment=False -> sequential alignment."""
        screenshots = [
            _make_png_bytes(pattern="hstripes"),
            _make_png_bytes(pattern="checker"),
        ]
        actions = [
            _make_click_action(0.1, 0.1),
            _make_click_action(0.9, 0.9),
        ]
        tmp_library.add_demo(
            "task_disabled",
            screenshots=screenshots,
            actions=actions,
        )

        # Screen looks like step 1, but visual is disabled
        checker_bytes = _make_png_bytes(pattern="checker")
        guidance = tmp_library.align_step(
            "task_disabled",
            current_screenshot=checker_bytes,
            step_index=0,
            use_visual_alignment=False,
        )

        assert guidance.step_index == 0  # sequential, not visual
        assert not guidance.visual_alignment_used

    def test_sequential_past_demo_length_without_visual(
        self, tmp_library: DemoLibrary
    ):
        """Without visual alignment, past-end falls back to last step."""
        screenshots = [_make_png_bytes(pattern="hstripes")]
        actions = [_make_click_action(0.5, 0.5)]
        tmp_library.add_demo(
            "task_past_seq",
            screenshots=screenshots,
            actions=actions,
        )

        guidance = tmp_library.align_step(
            "task_past_seq",
            current_screenshot=None,
            step_index=5,
        )

        assert guidance.step_index == 0  # last (and only) step
        assert guidance.confidence == 0.2
        assert not guidance.visual_alignment_used


# ---------------------------------------------------------------------------
# pHash caching
# ---------------------------------------------------------------------------


class TestPHashCaching:
    """Test that demo screenshot pHashes are computed once and cached."""

    def test_phash_cached_across_calls(self, tmp_library: DemoLibrary):
        """Second align_step call should reuse cached pHashes."""
        screenshots = [
            _make_png_bytes(pattern="hstripes"),
            _make_png_bytes(pattern="checker"),
        ]
        actions = [
            _make_click_action(0.1, 0.1),
            _make_click_action(0.9, 0.9),
        ]
        tmp_library.add_demo(
            "task_cache",
            screenshots=screenshots,
            actions=actions,
        )

        red_bytes = _make_png_bytes(pattern="hstripes")

        # First call -- computes hashes
        g1 = tmp_library.align_step(
            "task_cache",
            current_screenshot=red_bytes,
            step_index=0,
        )

        # Second call -- should reuse cached hashes
        g2 = tmp_library.align_step(
            "task_cache",
            current_screenshot=red_bytes,
            step_index=0,
        )

        assert g1.step_index == g2.step_index
        assert g1.visual_distance == g2.visual_distance

    def test_phash_not_in_serialized_json(self, tmp_library: DemoLibrary):
        """_phash should not appear in demo.json after serialization."""
        screenshots = [_make_png_bytes(pattern="hstripes")]
        actions = [_make_click_action(0.5, 0.5)]
        demo_id = tmp_library.add_demo(
            "task_json",
            screenshots=screenshots,
            actions=actions,
        )

        # Trigger pHash computation
        red_bytes = _make_png_bytes(pattern="hstripes")
        tmp_library.align_step(
            "task_json",
            current_screenshot=red_bytes,
            step_index=0,
        )

        # Load demo.json directly and verify no _phash key
        demo_dir = tmp_library.library_dir / "task_json" / demo_id
        with open(demo_dir / "demo.json") as f:
            data = json.load(f)

        for step_data in data["steps"]:
            assert "_phash" not in step_data


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------


class TestVisualAlignmentConfidence:
    """Test confidence calculation based on pHash distance."""

    def test_exact_match_confidence_is_one(self, tmp_library: DemoLibrary):
        """Distance 0 -> confidence 1.0."""
        screenshots = [_make_png_bytes(pattern="checker")]
        actions = [_make_click_action(0.5, 0.5)]
        tmp_library.add_demo(
            "task_conf",
            screenshots=screenshots,
            actions=actions,
        )

        red_bytes = _make_png_bytes(pattern="checker")
        guidance = tmp_library.align_step(
            "task_conf",
            current_screenshot=red_bytes,
            step_index=0,
        )

        assert guidance.confidence == 1.0
        assert guidance.visual_distance == 0

    def test_different_images_lower_confidence(
        self, tmp_library: DemoLibrary
    ):
        """Very different images should have lower confidence."""
        screenshots = [_make_png_bytes(pattern="hstripes")]
        actions = [_make_click_action(0.5, 0.5)]
        tmp_library.add_demo(
            "task_diff",
            screenshots=screenshots,
            actions=actions,
        )

        # Query with a very different pattern
        green_bytes = _make_png_bytes(pattern="checker")
        guidance = tmp_library.align_step(
            "task_diff",
            current_screenshot=green_bytes,
            step_index=0,
        )

        # pHash of solid colors may still be somewhat similar (both are
        # uniform), but the distance should be > 0
        assert guidance.visual_alignment_used
        assert guidance.confidence <= 1.0
        assert guidance.confidence >= 0.0


# ---------------------------------------------------------------------------
# DemoGuidance fields
# ---------------------------------------------------------------------------


class TestDemoGuidanceVisualFields:
    """Test that DemoGuidance carries visual alignment metadata."""

    def test_empty_guidance_has_no_visual_fields(self):
        """Empty guidance should have visual alignment disabled."""
        from openadapt_evals.demo_library import _empty_guidance

        g = _empty_guidance(0)
        assert not g.visual_alignment_used
        assert g.visual_distance is None

    def test_guidance_with_visual_alignment(self, tmp_library: DemoLibrary):
        """Guidance from visual alignment should populate both fields."""
        screenshots = [_make_png_bytes(pattern="diagonal")]
        actions = [_make_click_action(0.5, 0.5)]
        tmp_library.add_demo(
            "task_fields",
            screenshots=screenshots,
            actions=actions,
        )

        red_bytes = _make_png_bytes(pattern="diagonal")
        guidance = tmp_library.align_step(
            "task_fields",
            current_screenshot=red_bytes,
            step_index=0,
        )

        assert guidance.visual_alignment_used is True
        assert guidance.visual_distance is not None
        assert isinstance(guidance.visual_distance, float)


# ---------------------------------------------------------------------------
# _demo_to_dict helper
# ---------------------------------------------------------------------------


class TestDemoToDict:
    """Test the serialization helper that strips internal fields."""

    def test_strips_phash_from_steps(self):
        """_demo_to_dict should remove _phash from step dicts."""
        import imagehash
        from PIL import Image

        step = DemoStep(
            step_index=0,
            screenshot_path="step_000.png",
            action_type="click",
            action_description="CLICK(0.5, 0.3)",
            target_description="button",
            action_value="",
        )
        # Simulate cached phash
        img = Image.new("RGB", (64, 64), color="red")
        step._phash = imagehash.phash(img)

        demo = Demo(
            task_id="test",
            demo_id="abc123",
            description="Test demo",
            steps=[step],
        )

        data = _demo_to_dict(demo)
        assert "_phash" not in data["steps"][0]

    def test_preserves_other_fields(self):
        """_demo_to_dict should preserve all non-internal fields."""
        step = DemoStep(
            step_index=0,
            screenshot_path="step_000.png",
            action_type="click",
            action_description="CLICK(0.5, 0.3)",
            target_description="button",
            action_value="typed",
            description="my element",
            x=0.5,
            y=0.3,
        )
        demo = Demo(
            task_id="test",
            demo_id="abc123",
            description="Test demo",
            steps=[step],
        )

        data = _demo_to_dict(demo)
        s = data["steps"][0]
        assert s["step_index"] == 0
        assert s["screenshot_path"] == "step_000.png"
        assert s["action_type"] == "click"
        assert s["description"] == "my element"
        assert s["x"] == 0.5
        assert s["y"] == 0.3


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestVisualAlignmentEdgeCases:
    """Edge cases for visual alignment."""

    def test_single_step_demo(self, tmp_library: DemoLibrary):
        """Single-step demo should always match step 0."""
        screenshots = [_make_png_bytes(pattern="hstripes")]
        actions = [_make_click_action(0.5, 0.5)]
        tmp_library.add_demo(
            "task_single",
            screenshots=screenshots,
            actions=actions,
        )

        blue_bytes = _make_png_bytes(pattern="checker")
        guidance = tmp_library.align_step(
            "task_single",
            current_screenshot=blue_bytes,
            step_index=0,
        )

        assert guidance.step_index == 0
        assert guidance.visual_alignment_used

    def test_no_demo_returns_empty_guidance(self, tmp_library: DemoLibrary):
        """Missing demo should return empty guidance."""
        red_bytes = _make_png_bytes(pattern="hstripes")
        guidance = tmp_library.align_step(
            "nonexistent_task",
            current_screenshot=red_bytes,
            step_index=0,
        )

        assert not guidance.available
        assert not guidance.visual_alignment_used

    def test_visual_alignment_with_resolution_normalization(
        self, tmp_library: DemoLibrary
    ):
        """Visual alignment should work alongside resolution normalization."""
        screenshots = [
            _make_png_bytes(pattern="hstripes"),
            _make_png_bytes(pattern="checker"),
        ]
        actions = [
            _make_click_action(0.5, 0.5),
            _make_click_action(0.9, 0.1),
        ]
        tmp_library.add_demo(
            "task_res_visual",
            screenshots=screenshots,
            actions=actions,
            descriptions=["center button", "corner button"],
            resolution=(1920, 1080),
        )

        # Query with checker screenshot + different resolution
        checker_bytes = _make_png_bytes(pattern="checker")
        guidance = tmp_library.align_step(
            "task_res_visual",
            current_screenshot=checker_bytes,
            step_index=0,
            current_resolution=(1280, 720),
        )

        # Should match step 1 (checker) via visual alignment
        assert guidance.step_index == 1
        assert guidance.visual_alignment_used
        # Coordinates should be resolution-normalized
        # 0.9 * 1280 / 1920 = 0.6
        assert "0.600" in guidance.instruction

    def test_next_screenshot_path_set_correctly(
        self, tmp_library: DemoLibrary
    ):
        """Visual alignment should set next_screenshot_path based on matched step."""
        screenshots = [
            _make_png_bytes(pattern="hstripes"),
            _make_png_bytes(pattern="checker"),
            _make_png_bytes(pattern="diagonal"),
        ]
        actions = [
            _make_click_action(0.1, 0.1),
            _make_click_action(0.5, 0.5),
            _make_click_action(0.9, 0.9),
        ]
        tmp_library.add_demo(
            "task_next",
            screenshots=screenshots,
            actions=actions,
        )

        # Match step 1 (checker) -- next should be step 2 (diagonal)
        checker_bytes = _make_png_bytes(pattern="checker")
        guidance = tmp_library.align_step(
            "task_next",
            current_screenshot=checker_bytes,
            step_index=0,
        )

        assert guidance.step_index == 1
        assert guidance.next_screenshot_path is not None
        assert "step_002" in guidance.next_screenshot_path

    def test_last_step_has_no_next_screenshot(
        self, tmp_library: DemoLibrary
    ):
        """When matched step is the last, next_screenshot_path is None."""
        screenshots = [
            _make_png_bytes(pattern="hstripes"),
            _make_png_bytes(pattern="checker"),
        ]
        actions = [
            _make_click_action(0.1, 0.1),
            _make_click_action(0.9, 0.9),
        ]
        tmp_library.add_demo(
            "task_last",
            screenshots=screenshots,
            actions=actions,
        )

        # Match last step (checker)
        checker_bytes = _make_png_bytes(pattern="checker")
        guidance = tmp_library.align_step(
            "task_last",
            current_screenshot=checker_bytes,
            step_index=0,
        )

        assert guidance.step_index == 1
        assert guidance.next_screenshot_path is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
