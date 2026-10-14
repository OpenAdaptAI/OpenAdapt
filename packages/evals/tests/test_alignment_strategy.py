"""Tests for pluggable alignment strategies and monotonic progress bias.

Validates:
- AlignmentStrategy protocol and PHashAlignmentStrategy
- Monotonic progress bias (backward penalty)
- Adaptive guidance disabling (low-confidence threshold)
- AlignmentTraceEntry metadata
- reset_alignment_state()
- HybridAlignmentStrategy with mocked CLIP
- Backward compatibility of DemoLibrary constructor
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from openadapt_evals.adapters.base import BenchmarkAction
from openadapt_evals.demo_library import (
    _HAS_IMAGEHASH,
    AlignmentStrategy,
    AlignmentTraceEntry,
    Demo,
    DemoLibrary,
    DemoStep,
    HybridAlignmentStrategy,
    PHashAlignmentStrategy,
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
    """Create valid PNG bytes with distinct visual content."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), color=color)
    draw = ImageDraw.Draw(img)

    if pattern == "hstripes":
        for y in range(0, height, 20):
            draw.rectangle([0, y, width, y + 10], fill="black")
    elif pattern == "vstripes":
        for x in range(0, width, 20):
            draw.rectangle([x, 0, x + 10, height], fill="black")
    elif pattern == "diagonal":
        for offset in range(-height, width, 20):
            draw.line(
                [(offset, 0), (offset + height, height)],
                fill="black",
                width=5,
            )
    elif pattern == "checker":
        sq = 25
        for y in range(0, height, sq):
            for x in range(0, width, sq):
                if (x // sq + y // sq) % 2 == 0:
                    draw.rectangle([x, y, x + sq, y + sq], fill="black")
    elif pattern == "gradient":
        for x in range(width):
            gray = int(255 * x / width)
            draw.line([(x, 0), (x, height)], fill=(gray, gray, gray))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_click_action(x: float, y: float, name: str = "") -> BenchmarkAction:
    return BenchmarkAction(type="click", x=x, y=y, target_name=name)


@pytest.fixture
def tmp_library(tmp_path: Path) -> DemoLibrary:
    return DemoLibrary(str(tmp_path / "demos"))


def _make_three_step_demo(library: DemoLibrary) -> str:
    """Create a 3-step demo with guaranteed-distinct pHash patterns.

    Step 0: hstripes, Step 1: checker, Step 2: diagonal.
    These patterns produce reliably distinct pHash values.
    """
    patterns = ["hstripes", "checker", "diagonal"]
    screenshots = [_make_png_bytes(pattern=p) for p in patterns]
    actions = [
        _make_click_action(0.1 * (i + 1), 0.1 * (i + 1)) for i in range(3)
    ]
    return library.add_demo(
        "task_three",
        screenshots=screenshots,
        actions=actions,
        descriptions=[f"element_{i}" for i in range(3)],
    )


# ---------------------------------------------------------------------------
# AlignmentStrategy protocol
# ---------------------------------------------------------------------------


class TestAlignmentStrategyProtocol:
    """Test that PHashAlignmentStrategy satisfies the protocol."""

    def test_phash_strategy_is_alignment_strategy(self):
        strategy = PHashAlignmentStrategy()
        assert isinstance(strategy, AlignmentStrategy)

    def test_phash_strategy_returns_correct_types(self, tmp_library):
        _make_three_step_demo(tmp_library)
        demo = tmp_library.get_demo("task_three")
        demo_dir = tmp_library._demo_dir("task_three", demo.demo_id)

        strategy = PHashAlignmentStrategy()
        checker_bytes = _make_png_bytes(pattern="checker")

        step_idx, distance, meta = strategy.find_closest_step(
            checker_bytes,
            demo,
            demo_dir,
            min_step=0,
            backward_penalty=0.3,
        )

        assert isinstance(step_idx, int)
        assert isinstance(distance, float)
        assert isinstance(meta, dict)
        assert "method" in meta

    def test_phash_strategy_finds_exact_match(self, tmp_library):
        _make_three_step_demo(tmp_library)
        demo = tmp_library.get_demo("task_three")
        demo_dir = tmp_library._demo_dir("task_three", demo.demo_id)

        strategy = PHashAlignmentStrategy()

        # Checker is step 1
        checker_bytes = _make_png_bytes(pattern="checker")
        step_idx, distance, meta = strategy.find_closest_step(
            checker_bytes,
            demo,
            demo_dir,
            min_step=0,
            backward_penalty=0.3,
        )

        assert step_idx == 1
        assert distance == 0.0
        assert meta["method"] == "phash"

    def test_custom_strategy_can_be_passed_to_library(self, tmp_path):
        """A custom AlignmentStrategy can be passed to DemoLibrary."""

        class FixedStrategy:
            """Always returns step 0 with distance 0.5."""

            def find_closest_step(
                self,
                current_screenshot,
                demo,
                demo_dir,
                min_step,
                backward_penalty,
            ):
                return 0, 0.5, {"method": "fixed"}

        library = DemoLibrary(
            str(tmp_path / "demos"),
            alignment_strategy=FixedStrategy(),
        )

        screenshots = [_make_png_bytes(pattern="hstripes")]
        actions = [_make_click_action(0.5, 0.5)]
        library.add_demo(
            "task_custom", screenshots=screenshots, actions=actions
        )

        guidance = library.align_step(
            "task_custom",
            current_screenshot=_make_png_bytes(pattern="checker"),
            step_index=0,
        )

        assert guidance.available
        assert guidance.step_index == 0
        assert guidance.confidence == 0.5  # 1.0 - 0.5


# ---------------------------------------------------------------------------
# Monotonic progress bias
# ---------------------------------------------------------------------------


class TestMonotonicProgressBias:
    """Test backward penalty in visual alignment."""

    def test_no_backward_penalty_when_min_step_zero(self, tmp_library):
        """First alignment call has no backward penalty."""
        _make_three_step_demo(tmp_library)

        checker_bytes = _make_png_bytes(pattern="checker")
        guidance = tmp_library.align_step(
            "task_three",
            current_screenshot=checker_bytes,
            step_index=0,
        )

        # Step 1 (checker) should match without penalty
        assert guidance.step_index == 1
        assert guidance.confidence == 1.0

    def test_forward_match_preferred_after_progress(self, tmp_library):
        """After matching step 1, forward match to step 2 works."""
        _make_three_step_demo(tmp_library)

        # First call: match step 1 (checker)
        checker_bytes = _make_png_bytes(pattern="checker")
        g1 = tmp_library.align_step(
            "task_three",
            current_screenshot=checker_bytes,
            step_index=0,
        )
        assert g1.step_index == 1

        # Second call: present diagonal (step 2) -- should match step 2
        diagonal_bytes = _make_png_bytes(pattern="diagonal")
        g2 = tmp_library.align_step(
            "task_three",
            current_screenshot=diagonal_bytes,
            step_index=1,
        )
        assert g2.step_index == 2

    def test_backward_penalty_in_metadata(self, tmp_library):
        """Alignment trace should include method info."""
        _make_three_step_demo(tmp_library)

        # First call: match step 2 (diagonal)
        diagonal_bytes = _make_png_bytes(pattern="diagonal")
        g1 = tmp_library.align_step(
            "task_three",
            current_screenshot=diagonal_bytes,
            step_index=0,
        )
        assert g1.step_index == 2

        # Second call: present hstripes (step 0) -- backward from step 2
        hstripes_bytes = _make_png_bytes(pattern="hstripes")
        g2 = tmp_library.align_step(
            "task_three",
            current_screenshot=hstripes_bytes,
            step_index=1,
        )

        # The alignment should still work and have trace
        trace = g2.metadata.get("alignment_trace", {})
        assert trace.get("method") == "phash"

    def test_backward_penalty_zero_disables_bias(self, tmp_path):
        """backward_penalty=0.0 disables monotonic bias."""
        library = DemoLibrary(
            str(tmp_path / "demos"),
            backward_penalty=0.0,
        )
        patterns = ["hstripes", "checker", "diagonal"]
        screenshots = [_make_png_bytes(pattern=p) for p in patterns]
        actions = [
            _make_click_action(0.1 * (i + 1), 0.1 * (i + 1))
            for i in range(3)
        ]
        library.add_demo(
            "task_no_bias", screenshots=screenshots, actions=actions
        )

        # Match step 2 first (diagonal)
        diagonal_bytes = _make_png_bytes(pattern="diagonal")
        g1 = library.align_step(
            "task_no_bias",
            current_screenshot=diagonal_bytes,
            step_index=0,
        )
        assert g1.step_index == 2

        # Present step 0 (hstripes) -- should match step 0 without penalty
        hstripes_bytes = _make_png_bytes(pattern="hstripes")
        g2 = library.align_step(
            "task_no_bias",
            current_screenshot=hstripes_bytes,
            step_index=1,
        )
        assert g2.step_index == 0

    def test_reset_alignment_state_clears_progress(self, tmp_library):
        """reset_alignment_state() should clear monotonic tracking."""
        _make_three_step_demo(tmp_library)

        # Match step 2 (diagonal)
        diagonal_bytes = _make_png_bytes(pattern="diagonal")
        tmp_library.align_step(
            "task_three",
            current_screenshot=diagonal_bytes,
            step_index=0,
        )

        assert "task_three" in tmp_library._last_matched_step
        assert tmp_library._last_matched_step["task_three"] == 2

        # Reset
        tmp_library.reset_alignment_state("task_three")

        assert "task_three" not in tmp_library._last_matched_step

    def test_reset_alignment_state_all(self, tmp_library):
        """reset_alignment_state() without task_id clears all."""
        _make_three_step_demo(tmp_library)

        diagonal_bytes = _make_png_bytes(pattern="diagonal")
        tmp_library.align_step(
            "task_three",
            current_screenshot=diagonal_bytes,
            step_index=0,
        )

        tmp_library.reset_alignment_state()

        assert len(tmp_library._last_matched_step) == 0


# ---------------------------------------------------------------------------
# Adaptive guidance disabling
# ---------------------------------------------------------------------------


class TestAdaptiveGuidanceDisabling:
    """Test that guidance is disabled after consecutive low-confidence matches."""

    def test_guidance_disabled_after_consecutive_low_confidence(
        self, tmp_path
    ):
        """After 3 consecutive low-confidence matches, guidance is disabled."""
        library = DemoLibrary(
            str(tmp_path / "demos"),
            low_confidence_threshold=0.99,  # high threshold
            max_consecutive_low_confidence=3,
        )

        # Create a 2-step demo with distinct patterns
        screenshots = [
            _make_png_bytes(pattern="hstripes"),
            _make_png_bytes(pattern="checker"),
        ]
        actions = [
            _make_click_action(0.5, 0.5),
            _make_click_action(0.9, 0.9),
        ]
        library.add_demo(
            "task_disable", screenshots=screenshots, actions=actions
        )

        # Use a different pattern so confidence won't be 1.0
        query_bytes = _make_png_bytes(pattern="diagonal")

        # Call align_step 3 times -- should trigger disabling
        for i in range(3):
            g = library.align_step(
                "task_disable",
                current_screenshot=query_bytes,
                step_index=i,
            )
            if i < 2:
                assert g.available

        # 4th call should return empty guidance
        g4 = library.align_step(
            "task_disable",
            current_screenshot=query_bytes,
            step_index=3,
        )
        assert not g4.available

    def test_guidance_not_disabled_with_high_confidence(self, tmp_library):
        """High-confidence matches should not trigger disabling."""
        _make_three_step_demo(tmp_library)

        # Present exact matches -- high confidence
        for pattern in ["hstripes", "checker", "diagonal"]:
            query = _make_png_bytes(pattern=pattern)
            g = tmp_library.align_step(
                "task_three",
                current_screenshot=query,
                step_index=0,
            )
            assert g.available

        # Should NOT be disabled
        assert not tmp_library._guidance_disabled.get("task_three", False)

    def test_guidance_disabled_zero_disables_feature(self, tmp_path):
        """max_consecutive_low_confidence=0 disables the feature."""
        library = DemoLibrary(
            str(tmp_path / "demos"),
            max_consecutive_low_confidence=0,
        )

        screenshots = [_make_png_bytes(pattern="hstripes")]
        actions = [_make_click_action(0.5, 0.5)]
        library.add_demo(
            "task_no_disable", screenshots=screenshots, actions=actions
        )

        query_bytes = _make_png_bytes(pattern="diagonal")

        # Many low-confidence calls should not disable
        for i in range(10):
            g = library.align_step(
                "task_no_disable",
                current_screenshot=query_bytes,
                step_index=i,
            )
            assert g.available

    def test_reset_clears_guidance_disabled(self, tmp_path):
        """reset_alignment_state() should re-enable guidance."""
        library = DemoLibrary(
            str(tmp_path / "demos"),
            low_confidence_threshold=0.99,
            max_consecutive_low_confidence=2,
        )

        screenshots = [_make_png_bytes(pattern="hstripes")]
        actions = [_make_click_action(0.5, 0.5)]
        library.add_demo(
            "task_reset", screenshots=screenshots, actions=actions
        )

        query_bytes = _make_png_bytes(pattern="diagonal")

        # Trigger disabling
        for i in range(3):
            library.align_step(
                "task_reset",
                current_screenshot=query_bytes,
                step_index=i,
            )

        assert library._guidance_disabled.get("task_reset", False)

        # Reset
        library.reset_alignment_state("task_reset")

        # Should be re-enabled
        g = library.align_step(
            "task_reset",
            current_screenshot=query_bytes,
            step_index=0,
        )
        assert g.available

    def test_high_confidence_resets_counter(self, tmp_library):
        """A high-confidence match should reset the low-confidence counter."""
        _make_three_step_demo(tmp_library)

        # Override threshold to make non-exact matches "low confidence"
        tmp_library._low_confidence_threshold = 0.99
        tmp_library._max_consecutive_low_confidence = 5

        # Two low-confidence calls with a non-exact pattern
        query_bad = _make_png_bytes(pattern="vstripes")
        for i in range(2):
            tmp_library.align_step(
                "task_three",
                current_screenshot=query_bad,
                step_index=i,
            )

        count_before = tmp_library._consecutive_low_confidence.get(
            "task_three", 0
        )
        assert count_before == 2

        # One exact match (checker = step 1, distance 0, confidence 1.0)
        # Even with threshold 0.99, confidence 1.0 > 0.99 passes
        query_good = _make_png_bytes(pattern="checker")
        tmp_library.align_step(
            "task_three",
            current_screenshot=query_good,
            step_index=2,
        )

        assert (
            tmp_library._consecutive_low_confidence.get("task_three", 0) == 0
        )


# ---------------------------------------------------------------------------
# AlignmentTraceEntry
# ---------------------------------------------------------------------------


class TestAlignmentTraceEntry:
    """Test alignment trace metadata in DemoGuidance."""

    def test_trace_included_in_guidance_metadata(self, tmp_library):
        """Visual alignment should include trace in guidance.metadata."""
        _make_three_step_demo(tmp_library)

        checker_bytes = _make_png_bytes(pattern="checker")
        guidance = tmp_library.align_step(
            "task_three",
            current_screenshot=checker_bytes,
            step_index=0,
        )

        assert "alignment_trace" in guidance.metadata
        trace = guidance.metadata["alignment_trace"]
        assert trace["matched_demo_step"] == 1
        assert trace["method"] == "phash"
        assert trace["visual_alignment_used"] is True
        assert trace["elapsed_ms"] >= 0
        assert trace["confidence"] == 1.0

    def test_trace_not_included_for_sequential_alignment(self, tmp_library):
        """Sequential alignment should not include trace."""
        _make_three_step_demo(tmp_library)

        guidance = tmp_library.align_step(
            "task_three",
            current_screenshot=None,
            step_index=0,
        )

        assert "alignment_trace" not in guidance.metadata

    def test_trace_has_top_k_candidates(self, tmp_library):
        """Trace metadata should include top-K candidate info."""
        _make_three_step_demo(tmp_library)

        checker_bytes = _make_png_bytes(pattern="checker")
        guidance = tmp_library.align_step(
            "task_three",
            current_screenshot=checker_bytes,
            step_index=0,
        )

        trace = guidance.metadata["alignment_trace"]
        meta = trace["metadata"]
        assert "top_k_candidates" in meta
        assert len(meta["top_k_candidates"]) > 0
        assert meta["top_k_candidates"][0]["step"] == 1

    def test_alignment_trace_dataclass(self):
        """AlignmentTraceEntry should serialize cleanly."""
        from dataclasses import asdict

        entry = AlignmentTraceEntry(
            agent_step_index=5,
            matched_demo_step=3,
            distance=0.125,
            confidence=0.875,
            method="phash",
            visual_alignment_used=True,
            candidates_considered=10,
            elapsed_ms=1.5,
        )
        data = asdict(entry)
        assert data["agent_step_index"] == 5
        assert data["distance"] == 0.125
        assert data["method"] == "phash"
        assert data["backward_penalty_applied"] is False


# ---------------------------------------------------------------------------
# HybridAlignmentStrategy (pHash fallback without CLIP)
# ---------------------------------------------------------------------------


class TestHybridAlignmentStrategy:
    """Test HybridAlignmentStrategy with pHash fallback."""

    def test_hybrid_without_clip_falls_back_to_phash(self, tmp_library):
        """Without CLIP, hybrid should use pHash only."""
        _make_three_step_demo(tmp_library)
        demo = tmp_library.get_demo("task_three")
        demo_dir = tmp_library._demo_dir("task_three", demo.demo_id)

        # Create a hybrid strategy that has no CLIP
        strategy = HybridAlignmentStrategy.__new__(HybridAlignmentStrategy)
        strategy._top_k = 5
        strategy._clip = None

        checker_bytes = _make_png_bytes(pattern="checker")
        step_idx, distance, meta = strategy.find_closest_step(
            checker_bytes,
            demo,
            demo_dir,
            min_step=0,
            backward_penalty=0.3,
        )

        assert step_idx == 1
        assert distance == 0.0
        assert meta["method"] == "phash"

    def test_hybrid_top_k_limits_candidates(self, tmp_library):
        """Hybrid with top_k=2 should only report 2 candidates."""
        _make_three_step_demo(tmp_library)
        demo = tmp_library.get_demo("task_three")
        demo_dir = tmp_library._demo_dir("task_three", demo.demo_id)

        strategy = HybridAlignmentStrategy.__new__(HybridAlignmentStrategy)
        strategy._top_k = 2
        strategy._clip = None

        checker_bytes = _make_png_bytes(pattern="checker")
        step_idx, distance, meta = strategy.find_closest_step(
            checker_bytes,
            demo,
            demo_dir,
            min_step=0,
            backward_penalty=0.3,
        )

        assert step_idx == 1
        assert len(meta.get("top_k_candidates", [])) <= 2

    def test_library_with_hybrid_strategy(self, tmp_path):
        """DemoLibrary should accept HybridAlignmentStrategy."""
        strategy = HybridAlignmentStrategy.__new__(HybridAlignmentStrategy)
        strategy._top_k = 5
        strategy._clip = None

        library = DemoLibrary(
            str(tmp_path / "demos"),
            alignment_strategy=strategy,
        )

        screenshots = [
            _make_png_bytes(pattern="hstripes"),
            _make_png_bytes(pattern="checker"),
        ]
        actions = [
            _make_click_action(0.5, 0.5),
            _make_click_action(0.9, 0.9),
        ]
        library.add_demo(
            "task_hybrid", screenshots=screenshots, actions=actions
        )

        checker_bytes = _make_png_bytes(pattern="checker")
        guidance = library.align_step(
            "task_hybrid",
            current_screenshot=checker_bytes,
            step_index=0,
        )

        assert guidance.step_index == 1
        assert guidance.visual_alignment_used


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Test that existing DemoLibrary API is preserved."""

    def test_constructor_without_new_params(self, tmp_path):
        """DemoLibrary() without new params should use defaults."""
        library = DemoLibrary(str(tmp_path / "demos"))
        assert library._backward_penalty == 0.3
        assert library._alignment_strategy is not None

    def test_align_step_signature_unchanged(self, tmp_library):
        """align_step() should still accept the same arguments."""
        screenshots = [_make_png_bytes(pattern="hstripes")]
        actions = [_make_click_action(0.5, 0.5)]
        tmp_library.add_demo(
            "task_compat", screenshots=screenshots, actions=actions
        )

        # Old-style call
        guidance = tmp_library.align_step(
            "task_compat",
            current_screenshot=None,
            step_index=0,
        )
        assert guidance.available
        assert guidance.confidence == 1.0

    def test_guidance_fields_unchanged(self, tmp_library):
        """DemoGuidance should still have all original fields."""
        screenshots = [_make_png_bytes(pattern="hstripes")]
        actions = [_make_click_action(0.5, 0.5)]
        tmp_library.add_demo(
            "task_fields", screenshots=screenshots, actions=actions
        )

        guidance = tmp_library.align_step(
            "task_fields",
            current_screenshot=_make_png_bytes(pattern="hstripes"),
            step_index=0,
        )

        assert hasattr(guidance, "available")
        assert hasattr(guidance, "step_index")
        assert hasattr(guidance, "instruction")
        assert hasattr(guidance, "confidence")
        assert hasattr(guidance, "visual_alignment_used")
        assert hasattr(guidance, "visual_distance")
        assert hasattr(guidance, "metadata")

    def test_demo_to_dict_strips_clip_embedding(self):
        """_demo_to_dict should strip _clip_embedding from metadata."""
        step = DemoStep(
            step_index=0,
            screenshot_path="step_000.png",
            action_type="click",
            action_description="CLICK(0.5, 0.3)",
            target_description="button",
            action_value="",
            metadata={"_clip_embedding": [1.0, 2.0, 3.0]},
        )
        demo = Demo(
            task_id="test",
            demo_id="abc123",
            description="Test demo",
            steps=[step],
        )

        data = _demo_to_dict(demo)
        assert "_clip_embedding" not in data["steps"][0]["metadata"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
