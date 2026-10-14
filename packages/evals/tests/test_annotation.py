"""Tests for openadapt_evals.annotation module."""

from __future__ import annotations

import json

from openadapt_evals.annotation import (
    AnnotatedDemo,
    AnnotatedStep,
    format_annotated_demo,
    parse_annotation_response,
    validate_annotations,
)


def _make_step(**kwargs) -> AnnotatedStep:
    """Create an AnnotatedStep with sensible defaults."""
    defaults = {
        "step_index": 0,
        "timestamp_ms": None,
        "observation": "Windows desktop with Notepad open.",
        "intent": "Type text into the document.",
        "action": "Click the text area and type 'Hello World'.",
        "action_raw": "TYPE('Hello World')",
        "action_px": None,
        "result_observation": "Text 'Hello World' appears in Notepad.",
        "expected_result": "The text 'Hello World' is visible.",
    }
    defaults.update(kwargs)
    return AnnotatedStep(**defaults)


def _make_demo(steps=None, **kwargs) -> AnnotatedDemo:
    """Create an AnnotatedDemo with sensible defaults."""
    defaults = {
        "schema_version": "0.1",
        "task_id": "test-task-001",
        "instruction": "Open Notepad and type 'Hello World'.",
        "source": "recorded",
        "annotator": {"provider": "openai", "model": "gpt-4.1-mini"},
        "recording_meta": {"platform": "windows", "screen_px": [1920, 1080]},
        "steps": steps or [_make_step()],
    }
    defaults.update(kwargs)
    return AnnotatedDemo(**defaults)


class TestAnnotatedStepCreation:
    def test_basic_fields(self):
        step = _make_step(step_index=3, observation="Settings app open.")
        assert step.step_index == 3
        assert step.observation == "Settings app open."
        assert step.action_raw == "TYPE('Hello World')"
        assert step.timestamp_ms is None
        assert step.action_px is None

    def test_all_fields(self):
        step = _make_step(
            step_index=5,
            timestamp_ms=12345,
            action_px=[100, 200],
        )
        assert step.timestamp_ms == 12345
        assert step.action_px == [100, 200]


class TestAnnotatedDemoJsonRoundtrip:
    def test_to_json_and_load(self, tmp_path):
        demo = _make_demo(steps=[_make_step(step_index=0), _make_step(step_index=1)])
        json_str = demo.to_json()
        data = json.loads(json_str)
        assert data["task_id"] == "test-task-001"
        assert len(data["steps"]) == 2

        # Save and load
        path = tmp_path / "demo.json"
        demo.save(path)
        loaded = AnnotatedDemo.load(path)
        assert loaded.task_id == demo.task_id
        assert loaded.instruction == demo.instruction
        assert len(loaded.steps) == len(demo.steps)
        assert loaded.steps[0].observation == demo.steps[0].observation
        assert loaded.steps[1].step_index == 1

    def test_save_creates_parent_dirs(self, tmp_path):
        demo = _make_demo()
        path = tmp_path / "sub" / "dir" / "demo.json"
        demo.save(path)
        assert path.exists()
        loaded = AnnotatedDemo.load(path)
        assert loaded.task_id == demo.task_id


class TestParseAnnotationResponse:
    def test_clean_json(self):
        response = json.dumps({
            "observation": "Notepad is open.",
            "intent": "Type text.",
            "action": "TYPE('Hello')",
            "result_observation": "Text appears.",
            "expected_result": "Text visible.",
        })
        parsed = parse_annotation_response(response)
        assert parsed["observation"] == "Notepad is open."
        assert parsed["action"] == "TYPE('Hello')"

    def test_with_fences(self):
        response = (
            '```json\n'
            '{"observation": "Desktop", "intent": "Open app", '
            '"action": "CLICK(Start)", "result_observation": "Menu opens", '
            '"expected_result": "Start menu visible"}\n'
            '```'
        )
        parsed = parse_annotation_response(response)
        assert parsed["observation"] == "Desktop"
        assert parsed["action"] == "CLICK(Start)"

    def test_garbage(self):
        parsed = parse_annotation_response("This is not JSON at all.")
        # Should return fallback dict with partial text
        assert "observation" in parsed
        assert parsed["intent"] == ""
        assert parsed["action"] == ""

    def test_json_with_preamble(self):
        response = (
            'Here is the annotation:\n\n'
            '{"observation": "Settings open", "intent": "Toggle switch", '
            '"action": "CLICK(toggle)", "result_observation": "Toggled off", '
            '"expected_result": "Off state"}'
        )
        parsed = parse_annotation_response(response)
        assert parsed["observation"] == "Settings open"


class TestFormatAnnotatedDemoCompact:
    def test_compact_format(self):
        demo = _make_demo()
        text = format_annotated_demo(demo, compact=True)
        assert "DEMONSTRATION:" in text
        assert "Goal: Open Notepad" in text
        assert "[Screen:" in text
        assert "[Action:" in text
        assert "[Result:" in text
        # In compact mode, no [Intent:] line
        assert "[Intent:" not in text

    def test_verbose_format(self):
        demo = _make_demo()
        text = format_annotated_demo(demo, compact=False)
        assert "[Intent:" in text
        assert "[Screen:" in text

    def test_step_numbering(self):
        demo = _make_demo(steps=[
            _make_step(step_index=0),
            _make_step(step_index=1),
        ])
        text = format_annotated_demo(demo, compact=True)
        assert "Step 1:" in text
        assert "Step 2:" in text


class TestValidateAnnotationsEmptyFields:
    def test_empty_observation_intent_action(self):
        step = _make_step(observation="", intent="", action="")
        demo = _make_demo(steps=[step])
        warnings = validate_annotations(demo)
        assert any("empty observation" in w for w in warnings)
        assert any("empty intent" in w for w in warnings)
        assert any("empty action" in w for w in warnings)

    def test_no_result(self):
        step = _make_step(result_observation="", expected_result="")
        demo = _make_demo(steps=[step])
        warnings = validate_annotations(demo)
        assert any("no result_observation" in w for w in warnings)


class TestValidateAnnotationsRawCoordinates:
    def test_raw_coordinates_flagged(self):
        step = _make_step(action="CLICK(0.5, 0.3)")
        demo = _make_demo(steps=[step])
        warnings = validate_annotations(demo)
        assert any("raw coordinates" in w for w in warnings)

    def test_normal_action_no_warning(self):
        step = _make_step(action="Click the 'Save' button")
        demo = _make_demo(steps=[step])
        warnings = validate_annotations(demo)
        assert len(warnings) == 0

    def test_macos_term_in_windows(self):
        step = _make_step(observation="Finder window showing files")
        demo = _make_demo(steps=[step])
        warnings = validate_annotations(demo)
        assert any("finder" in w for w in warnings)
