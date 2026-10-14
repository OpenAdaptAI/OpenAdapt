"""Tests for VLM element description enrichment and resolution normalization.

Tests the DemoLibrary enrichment pipeline: VLM-based element descriptions,
resolution normalization, and enriched instruction generation -- without
requiring real API calls.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from openadapt_evals.adapters.base import BenchmarkAction
from openadapt_evals.demo_library import (
    DemoLibrary,
    DemoStep,
    _build_enriched_instruction,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_png_bytes(width: int = 200, height: int = 200) -> bytes:
    """Create minimal valid PNG bytes."""
    from PIL import Image

    img = Image.new("RGB", (width, height), color="white")
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


@pytest.fixture
def png_bytes() -> bytes:
    return _make_png_bytes()


@pytest.fixture
def sample_demo_id(tmp_library: DemoLibrary, png_bytes: bytes) -> str:
    """Create a simple 3-step demo and return the demo_id."""
    actions = [
        _make_click_action(0.5, 0.3, "Start button"),
        _make_type_action("hello"),
        _make_click_action(0.9, 0.1),
    ]
    screenshots = [png_bytes, png_bytes, png_bytes]
    return tmp_library.add_demo(
        "test_task",
        screenshots=screenshots,
        actions=actions,
        description="Test demo",
    )


# ---------------------------------------------------------------------------
# DemoStep.description field
# ---------------------------------------------------------------------------


class TestDemoStepDescription:
    def test_description_defaults_to_empty(self):
        step = DemoStep(
            step_index=0,
            screenshot_path="step_000.png",
            action_type="click",
            action_description="CLICK(0.5, 0.3)",
            target_description="button",
            action_value="",
        )
        assert step.description == ""

    def test_description_can_be_set(self):
        step = DemoStep(
            step_index=0,
            screenshot_path="step_000.png",
            action_type="click",
            action_description="CLICK(0.5, 0.3)",
            target_description="button",
            action_value="",
            description="Start menu button in taskbar",
        )
        assert step.description == "Start menu button in taskbar"

    def test_description_serializes_to_json(self):
        """Description must survive JSON round-trip (stored in demo.json)."""
        from dataclasses import asdict

        step = DemoStep(
            step_index=0,
            screenshot_path="step_000.png",
            action_type="click",
            action_description="CLICK(0.5, 0.3)",
            target_description="",
            action_value="",
            description="three-dot menu button",
        )
        data = asdict(step)
        restored = DemoStep(**data)
        assert restored.description == "three-dot menu button"


# ---------------------------------------------------------------------------
# add_demo() with descriptions parameter
# ---------------------------------------------------------------------------


class TestAddDemoDescriptions:
    def test_add_demo_with_descriptions(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        actions = [
            _make_click_action(0.5, 0.3),
            _make_click_action(0.9, 0.1),
        ]
        descriptions = [
            "Start menu button in taskbar",
            "Close button in top-right corner",
        ]
        tmp_library.add_demo(
            "task_a",
            screenshots=[png_bytes, png_bytes],
            actions=actions,
            descriptions=descriptions,
        )
        demo = tmp_library.get_demo("task_a")
        assert demo is not None
        assert demo.steps[0].description == "Start menu button in taskbar"
        assert demo.steps[1].description == "Close button in top-right corner"

    def test_add_demo_without_descriptions(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        actions = [_make_click_action(0.5, 0.3)]
        tmp_library.add_demo(
            "task_b",
            screenshots=[png_bytes],
            actions=actions,
        )
        demo = tmp_library.get_demo("task_b")
        assert demo is not None
        assert demo.steps[0].description == ""

    def test_add_demo_descriptions_length_mismatch_raises(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        actions = [_make_click_action(0.5, 0.3)]
        with pytest.raises(ValueError, match="descriptions"):
            tmp_library.add_demo(
                "task_c",
                screenshots=[png_bytes],
                actions=actions,
                descriptions=["one", "two"],  # mismatch
            )

    def test_add_demo_descriptions_persisted_to_disk(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        """Descriptions must survive a fresh get_demo() (read from disk)."""
        actions = [_make_click_action(0.5, 0.3)]
        tmp_library.add_demo(
            "task_d",
            screenshots=[png_bytes],
            actions=actions,
            descriptions=["search bar in Chrome"],
        )
        # Re-create library to force disk read
        library2 = DemoLibrary(tmp_library.library_dir)
        demo = library2.get_demo("task_d")
        assert demo is not None
        assert demo.steps[0].description == "search bar in Chrome"


# ---------------------------------------------------------------------------
# Resolution metadata
# ---------------------------------------------------------------------------


class TestResolutionMetadata:
    def test_resolution_stored_in_metadata(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        actions = [_make_click_action(0.5, 0.3)]
        tmp_library.add_demo(
            "task_res",
            screenshots=[png_bytes],
            actions=actions,
            resolution=(1920, 1080),
        )
        demo = tmp_library.get_demo("task_res")
        assert demo is not None
        assert demo.metadata["resolution"] == {"width": 1920, "height": 1080}

    def test_resolution_not_stored_when_none(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        actions = [_make_click_action(0.5, 0.3)]
        tmp_library.add_demo(
            "task_no_res",
            screenshots=[png_bytes],
            actions=actions,
        )
        demo = tmp_library.get_demo("task_no_res")
        assert demo is not None
        assert "resolution" not in demo.metadata


# ---------------------------------------------------------------------------
# Resolution normalization in align_step()
# ---------------------------------------------------------------------------


class TestResolutionNormalization:
    def test_coordinates_normalized_when_resolutions_differ(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        """Demo at 1920x1080, agent at 1280x720 -- coordinates scale."""
        actions = [_make_click_action(0.5, 0.5)]
        tmp_library.add_demo(
            "task_norm",
            screenshots=[png_bytes],
            actions=actions,
            descriptions=["center button"],
            resolution=(1920, 1080),
        )
        guidance = tmp_library.align_step(
            "task_norm",
            current_screenshot=None,
            step_index=0,
            current_resolution=(1280, 720),
        )
        # 0.5 * 1280 / 1920 = 0.333...
        assert "0.333" in guidance.instruction
        # 0.5 * 720 / 1080 = 0.333...
        assert "center button" in guidance.instruction

    def test_coordinates_unchanged_without_current_resolution(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        """Without current_resolution, coordinates pass through as-is."""
        actions = [_make_click_action(0.96, 0.07)]
        tmp_library.add_demo(
            "task_no_norm",
            screenshots=[png_bytes],
            actions=actions,
            descriptions=["menu button"],
            resolution=(1920, 1080),
        )
        guidance = tmp_library.align_step(
            "task_no_norm",
            current_screenshot=None,
            step_index=0,
        )
        assert "0.960" in guidance.instruction
        assert "0.070" in guidance.instruction

    def test_coordinates_unchanged_without_demo_resolution(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        """Without demo resolution metadata, coordinates pass through."""
        actions = [_make_click_action(0.96, 0.07)]
        tmp_library.add_demo(
            "task_no_demo_res",
            screenshots=[png_bytes],
            actions=actions,
            descriptions=["menu button"],
            # No resolution kwarg
        )
        guidance = tmp_library.align_step(
            "task_no_demo_res",
            current_screenshot=None,
            step_index=0,
            current_resolution=(1280, 720),
        )
        # Should use original coordinates since demo has no resolution
        assert "0.960" in guidance.instruction


# ---------------------------------------------------------------------------
# Enriched instruction building
# ---------------------------------------------------------------------------


class TestBuildEnrichedInstruction:
    def test_click_with_description_and_coords(self):
        step = DemoStep(
            step_index=0,
            screenshot_path="s.png",
            action_type="click",
            action_description="CLICK(0.960, 0.066)",
            target_description="",
            action_value="",
            x=0.96,
            y=0.066,
            description="three-dot menu in Chrome toolbar",
        )
        result = _build_enriched_instruction(step, 0.96, 0.066)
        assert result == (
            "three-dot menu in Chrome toolbar (approximately at "
            "(0.960, 0.066))"
        )

    def test_click_with_description_no_coords(self):
        step = DemoStep(
            step_index=0,
            screenshot_path="s.png",
            action_type="click",
            action_description="CLICK(0.5, 0.5)",
            target_description="",
            action_value="",
            description="save button",
        )
        result = _build_enriched_instruction(step)
        assert result == "save button"

    def test_click_without_description_falls_back(self):
        step = DemoStep(
            step_index=0,
            screenshot_path="s.png",
            action_type="click",
            action_description="CLICK(0.500, 0.300)",
            target_description="",
            action_value="",
            x=0.5,
            y=0.3,
        )
        result = _build_enriched_instruction(step, 0.5, 0.3)
        assert result == "CLICK(0.500, 0.300)"

    def test_type_action_uses_description_if_available(self):
        step = DemoStep(
            step_index=0,
            screenshot_path="s.png",
            action_type="type",
            action_description="TYPE('hello')",
            target_description="",
            action_value="hello",
            description="Type hello into the search box",
        )
        result = _build_enriched_instruction(step)
        assert result == "Type hello into the search box"

    def test_type_action_falls_back_without_description(self):
        step = DemoStep(
            step_index=0,
            screenshot_path="s.png",
            action_type="type",
            action_description="TYPE('hello')",
            target_description="",
            action_value="hello",
            description="",
        )
        result = _build_enriched_instruction(step)
        assert result == "TYPE('hello')"


# ---------------------------------------------------------------------------
# align_step() instruction enrichment end-to-end
# ---------------------------------------------------------------------------


class TestAlignStepEnriched:
    def test_enriched_instruction_in_guidance(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        actions = [_make_click_action(0.96, 0.066)]
        tmp_library.add_demo(
            "task_enrich",
            screenshots=[png_bytes],
            actions=actions,
            descriptions=["three-dot menu button"],
        )
        guidance = tmp_library.align_step(
            "task_enrich",
            current_screenshot=None,
            step_index=0,
        )
        assert guidance.available
        assert "three-dot menu button" in guidance.instruction
        assert "0.960" in guidance.instruction
        assert "0.066" in guidance.instruction

    def test_unenriched_instruction_in_guidance(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        """Without enrichment, falls back to raw action_description."""
        actions = [_make_click_action(0.5, 0.3)]
        tmp_library.add_demo(
            "task_raw",
            screenshots=[png_bytes],
            actions=actions,
        )
        guidance = tmp_library.align_step(
            "task_raw",
            current_screenshot=None,
            step_index=0,
        )
        assert guidance.available
        assert guidance.instruction == "CLICK(0.500, 0.300)"

    def test_to_prompt_text_includes_enriched_instruction(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        actions = [_make_click_action(0.5, 0.3)]
        tmp_library.add_demo(
            "task_prompt",
            screenshots=[png_bytes],
            actions=actions,
            descriptions=["File menu dropdown"],
        )
        guidance = tmp_library.align_step(
            "task_prompt",
            current_screenshot=None,
            step_index=0,
        )
        text = guidance.to_prompt_text()
        assert "DEMONSTRATION GUIDANCE" in text
        assert "File menu dropdown" in text
        assert "Adapt if the current UI state differs" in text


# ---------------------------------------------------------------------------
# enrich_demo() with mocked VLM
# ---------------------------------------------------------------------------


class TestEnrichDemo:
    def test_enrich_demo_calls_vlm_for_click_steps(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        actions = [
            _make_click_action(0.5, 0.3),
            _make_type_action("hello"),
            _make_click_action(0.9, 0.1),
        ]
        demo_id = tmp_library.add_demo(
            "task_enrich_vlm",
            screenshots=[png_bytes, png_bytes, png_bytes],
            actions=actions,
        )

        with patch(
            "openadapt_evals.demo_library._vlm_describe_element",
            return_value="Start menu button",
        ) as mock_vlm:
            tmp_library.enrich_demo("task_enrich_vlm", demo_id=demo_id)

        # VLM should be called only for click steps (steps 0 and 2)
        assert mock_vlm.call_count == 2

        # Verify descriptions were saved
        demo = tmp_library.get_demo("task_enrich_vlm")
        assert demo.steps[0].description == "Start menu button"
        assert demo.steps[1].description == ""  # type step, not enriched
        assert demo.steps[2].description == "Start menu button"

    def test_enrich_demo_is_idempotent(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        """Calling enrich_demo twice should not re-enrich already-described steps."""
        actions = [_make_click_action(0.5, 0.3)]
        demo_id = tmp_library.add_demo(
            "task_idempotent",
            screenshots=[png_bytes],
            actions=actions,
            descriptions=["already described"],
        )

        with patch(
            "openadapt_evals.demo_library._vlm_describe_element",
        ) as mock_vlm:
            tmp_library.enrich_demo("task_idempotent", demo_id=demo_id)

        # VLM should NOT be called since step already has description
        mock_vlm.assert_not_called()

    def test_enrich_demo_skips_steps_without_coordinates(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        actions = [BenchmarkAction(type="click")]  # no x, y
        demo_id = tmp_library.add_demo(
            "task_no_coords",
            screenshots=[png_bytes],
            actions=actions,
        )

        with patch(
            "openadapt_evals.demo_library._vlm_describe_element",
        ) as mock_vlm:
            tmp_library.enrich_demo("task_no_coords", demo_id=demo_id)

        mock_vlm.assert_not_called()

    def test_enrich_demo_raises_for_missing_task(
        self, tmp_library: DemoLibrary
    ):
        with pytest.raises(ValueError, match="No demo found"):
            tmp_library.enrich_demo("nonexistent_task")

    def test_enrich_demo_graceful_vlm_failure(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        """VLM failure should log warning but not raise."""
        actions = [_make_click_action(0.5, 0.3)]
        demo_id = tmp_library.add_demo(
            "task_vlm_fail",
            screenshots=[png_bytes],
            actions=actions,
        )

        with patch(
            "openadapt_evals.demo_library._vlm_describe_element",
            side_effect=RuntimeError("API unavailable"),
        ):
            # Should not raise
            tmp_library.enrich_demo("task_vlm_fail", demo_id=demo_id)

        # Description should remain empty
        demo = tmp_library.get_demo("task_vlm_fail")
        assert demo.steps[0].description == ""

    def test_enrich_demo_persists_to_disk(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        """Enrichment results must survive a fresh library load."""
        actions = [_make_click_action(0.5, 0.3)]
        demo_id = tmp_library.add_demo(
            "task_persist",
            screenshots=[png_bytes],
            actions=actions,
        )

        with patch(
            "openadapt_evals.demo_library._vlm_describe_element",
            return_value="Start menu button",
        ):
            tmp_library.enrich_demo("task_persist", demo_id=demo_id)

        # Reload from disk
        library2 = DemoLibrary(tmp_library.library_dir)
        demo = library2.get_demo("task_persist")
        assert demo.steps[0].description == "Start menu button"


# ---------------------------------------------------------------------------
# auto_enrich in add_demo()
# ---------------------------------------------------------------------------


class TestAutoEnrich:
    def test_auto_enrich_calls_enrich_demo(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        actions = [_make_click_action(0.5, 0.3)]

        with patch.object(
            tmp_library, "enrich_demo"
        ) as mock_enrich:
            tmp_library.add_demo(
                "task_auto",
                screenshots=[png_bytes],
                actions=actions,
                auto_enrich=True,
            )

        mock_enrich.assert_called_once()

    def test_auto_enrich_false_by_default(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        actions = [_make_click_action(0.5, 0.3)]

        with patch.object(
            tmp_library, "enrich_demo"
        ) as mock_enrich:
            tmp_library.add_demo(
                "task_no_auto",
                screenshots=[png_bytes],
                actions=actions,
            )

        mock_enrich.assert_not_called()

    def test_auto_enrich_skipped_when_descriptions_provided(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        """When descriptions are explicitly provided, auto_enrich is skipped."""
        actions = [_make_click_action(0.5, 0.3)]

        with patch.object(
            tmp_library, "enrich_demo"
        ) as mock_enrich:
            tmp_library.add_demo(
                "task_explicit",
                screenshots=[png_bytes],
                actions=actions,
                descriptions=["manual description"],
                auto_enrich=True,  # should be ignored
            )

        mock_enrich.assert_not_called()

    def test_auto_enrich_failure_does_not_break_add_demo(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        """Auto-enrich failure should log warning but demo is still saved."""
        actions = [_make_click_action(0.5, 0.3)]

        with patch.object(
            tmp_library,
            "enrich_demo",
            side_effect=RuntimeError("VLM down"),
        ):
            demo_id = tmp_library.add_demo(
                "task_fail_graceful",
                screenshots=[png_bytes],
                actions=actions,
                auto_enrich=True,
            )

        # Demo should still be saved
        assert demo_id is not None
        demo = tmp_library.get_demo("task_fail_graceful")
        assert demo is not None
        assert len(demo.steps) == 1


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_old_demo_json_without_description_loads(
        self, tmp_library: DemoLibrary
    ):
        """Demos saved before the description field was added should load."""
        task_dir = tmp_library.library_dir / "old_task" / "old_demo"
        task_dir.mkdir(parents=True)

        # Write demo.json without description field in steps
        old_data = {
            "task_id": "old_task",
            "demo_id": "old_demo",
            "description": "Old demo",
            "steps": [
                {
                    "step_index": 0,
                    "screenshot_path": "step_000.png",
                    "action_type": "click",
                    "action_description": "CLICK(0.5, 0.3)",
                    "target_description": "",
                    "action_value": "",
                    "metadata": {},
                    "x": 0.5,
                    "y": 0.3,
                    # No "description" key -- simulating old format
                }
            ],
            "created_at": "2025-01-01T00:00:00+00:00",
            "metadata": {},
        }
        with open(task_dir / "demo.json", "w") as f:
            json.dump(old_data, f)

        # Create a dummy screenshot
        (task_dir / "step_000.png").write_bytes(_make_png_bytes())

        demo = tmp_library.get_demo("old_task")
        assert demo is not None
        # description should default to ""
        assert demo.steps[0].description == ""

    def test_align_step_backward_compatible_signature(
        self, tmp_library: DemoLibrary, png_bytes: bytes
    ):
        """align_step() without current_resolution still works."""
        actions = [_make_click_action(0.5, 0.3)]
        tmp_library.add_demo(
            "task_compat",
            screenshots=[png_bytes],
            actions=actions,
        )
        # Call without current_resolution (old signature)
        guidance = tmp_library.align_step(
            "task_compat",
            current_screenshot=None,
            step_index=0,
        )
        assert guidance.available
        assert guidance.instruction == "CLICK(0.500, 0.300)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
