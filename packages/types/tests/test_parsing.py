"""Comprehensive tests for openadapt_types.parsing module."""

import json

import pytest

from openadapt_types import (
    Action,
    ActionTarget,
    ActionType,
    from_benchmark_action,
    parse_action,
    parse_action_dsl,
    parse_action_json,
    to_benchmark_action_dict,
)


# ── DSL Parsing ──────────────────────────────────────────────────────


class TestParseDSL:
    def test_click_normalized_coords(self):
        action = parse_action_dsl("CLICK(x=0.50, y=0.30)")
        assert action.type == ActionType.CLICK
        assert action.target is not None
        assert action.target.x == pytest.approx(0.50)
        assert action.target.y == pytest.approx(0.30)
        assert action.target.is_normalized is True

    def test_click_pixel_coords(self):
        action = parse_action_dsl("CLICK(x=512, y=384)")
        assert action.type == ActionType.CLICK
        assert action.target.x == pytest.approx(512.0)
        assert action.target.y == pytest.approx(384.0)
        assert action.target.is_normalized is False

    def test_click_malformed_coords_dots(self):
        action = parse_action_dsl('CLICK(x="...", y=0.3)')
        assert action.type == ActionType.DONE

    def test_click_malformed_coords_negative(self):
        # Negative coordinates get clamped for normalized, but for pixels
        # they get clamped to 0.  The important thing is no crash.
        action = parse_action_dsl("CLICK(x=-1.0, y=-2.0)")
        # Negative coords mean not normalized (not in [0,1])
        # They should be clamped to 0
        assert action.type == ActionType.CLICK
        assert action.target.x == pytest.approx(0.0)
        assert action.target.y == pytest.approx(0.0)

    def test_type_simple_text(self):
        action = parse_action_dsl('TYPE(text="hello world")')
        assert action.type == ActionType.TYPE
        assert action.text == "hello world"

    def test_type_escaped_quotes(self):
        action = parse_action_dsl(r'TYPE(text="say \"hi\"")')
        assert action.type == ActionType.TYPE
        assert action.text == 'say "hi"'

    def test_type_empty_text(self):
        # Empty text violates TYPE validation -> DONE
        action = parse_action_dsl('TYPE(text="")')
        assert action.type == ActionType.DONE

    def test_key_standard(self):
        action = parse_action_dsl('KEY(key="enter")')
        assert action.type == ActionType.KEY
        assert action.key == "enter"

    def test_scroll_direction(self):
        action = parse_action_dsl('SCROLL(direction="down")')
        assert action.type == ActionType.SCROLL
        assert action.scroll_direction == "down"

    def test_wait(self):
        action = parse_action_dsl("WAIT()")
        assert action.type == ActionType.WAIT

    def test_done(self):
        action = parse_action_dsl("DONE()")
        assert action.type == ActionType.DONE

    def test_drag_with_all_coords(self):
        action = parse_action_dsl("DRAG(x=0.1, y=0.2, end_x=0.8, end_y=0.9)")
        assert action.type == ActionType.DRAG
        assert action.target.x == pytest.approx(0.1)
        assert action.target.y == pytest.approx(0.2)
        assert action.drag_end is not None
        assert action.drag_end.x == pytest.approx(0.8)
        assert action.drag_end.y == pytest.approx(0.9)

    def test_thought_and_action_prefix(self):
        text = "Thought: I need to click the submit button\nAction: CLICK(x=0.5, y=0.3)"
        action = parse_action_dsl(text)
        assert action.type == ActionType.CLICK
        assert action.reasoning == "I need to click the submit button"
        assert action.target.x == pytest.approx(0.5)

    def test_action_prefix_only(self):
        action = parse_action_dsl("Action: CLICK(x=0.5, y=0.3)")
        assert action.type == ActionType.CLICK
        assert action.target.x == pytest.approx(0.5)

    def test_bare_call_no_prefix(self):
        action = parse_action_dsl("CLICK(x=0.5, y=0.3)")
        assert action.type == ActionType.CLICK

    def test_case_insensitive(self):
        action = parse_action_dsl("click(x=0.5, y=0.3)")
        assert action.type == ActionType.CLICK

    def test_coordinate_clamping_normalized(self):
        action = parse_action_dsl("CLICK(x=1.5, y=2.0)")
        # 1.5 and 2.0 are > 1, so NOT normalized -> pixel coords
        assert action.type == ActionType.CLICK
        assert action.target.is_normalized is False
        assert action.target.x == pytest.approx(1.5)
        assert action.target.y == pytest.approx(2.0)

    def test_coordinate_clamping_normalized_within_range(self):
        # When values are in [0,1], they're normalized and clamped to [0,1]
        action = parse_action_dsl("CLICK(x=0.5, y=0.3)")
        assert action.target.is_normalized is True
        assert action.target.x == pytest.approx(0.5)

    def test_empty_input(self):
        action = parse_action_dsl("")
        assert action.type == ActionType.DONE

    def test_garbage_input(self):
        action = parse_action_dsl("just some random text with no action")
        assert action.type == ActionType.DONE

    def test_unknown_action_type(self):
        action = parse_action_dsl("FOOBAR(x=0.5, y=0.3)")
        assert action.type == ActionType.DONE

    def test_drag_missing_end_coords(self):
        action = parse_action_dsl("DRAG(x=0.1, y=0.2)")
        assert action.type == ActionType.DONE


# ── JSON Parsing ─────────────────────────────────────────────────────


class TestParseJSON:
    def test_canonical_format(self):
        text = '{"type": "click", "target": {"x": 0.5, "y": 0.3}}'
        action = parse_action_json(text)
        assert action.type == ActionType.CLICK
        assert action.target.x == pytest.approx(0.5)
        assert action.target.y == pytest.approx(0.3)
        assert action.target.is_normalized is True

    def test_legacy_flat_format(self):
        text = '{"type": "click", "x": 0.5, "y": 0.3}'
        action = parse_action_json(text)
        assert action.type == ActionType.CLICK
        assert action.target.x == pytest.approx(0.5)
        assert action.target.y == pytest.approx(0.3)

    def test_legacy_openadapt_ml_format(self):
        text = '{"action_type": "click", "coordinate": [0.5, 0.3]}'
        action = parse_action_json(text)
        assert action.type == ActionType.CLICK
        assert action.target.x == pytest.approx(0.5)
        assert action.target.y == pytest.approx(0.3)

    def test_with_reasoning(self):
        text = '{"type": "click", "x": 0.5, "y": 0.3, "reasoning": "need to click the button"}'
        action = parse_action_json(text)
        assert action.reasoning == "need to click the button"

    def test_with_thought_field(self):
        text = '{"type": "click", "x": 0.5, "y": 0.3, "thought": "I see the button"}'
        action = parse_action_json(text)
        assert action.reasoning == "I see the button"

    def test_with_thinking_field(self):
        text = '{"type": "click", "x": 0.5, "y": 0.3, "thinking": "analyzing the UI"}'
        action = parse_action_json(text)
        assert action.reasoning == "analyzing the UI"

    def test_with_markdown_fences(self):
        text = '```json\n{"type": "click", "x": 0.5, "y": 0.3}\n```'
        action = parse_action_json(text)
        assert action.type == ActionType.CLICK
        assert action.target.x == pytest.approx(0.5)

    def test_with_thinking_tokens_before_json(self):
        text = 'I think I should click the button. {"type": "click", "x": 0.5, "y": 0.3}'
        action = parse_action_json(text)
        assert action.type == ActionType.CLICK

    def test_nested_json_find_first(self):
        text = 'prefix text {"type": "click", "target": {"x": 0.5, "y": 0.3}} trailing'
        action = parse_action_json(text)
        assert action.type == ActionType.CLICK

    def test_invalid_json(self):
        text = '{not valid json at all'
        action = parse_action_json(text)
        assert action.type == ActionType.DONE

    def test_unknown_type(self):
        text = '{"type": "teleport", "x": 0.5, "y": 0.3}'
        action = parse_action_json(text)
        assert action.type == ActionType.DONE

    def test_type_action(self):
        text = '{"type": "type", "text": "hello"}'
        action = parse_action_json(text)
        assert action.type == ActionType.TYPE
        assert action.text == "hello"

    def test_key_action(self):
        text = '{"type": "key", "key": "enter"}'
        action = parse_action_json(text)
        assert action.type == ActionType.KEY
        assert action.key == "enter"

    def test_empty_input(self):
        action = parse_action_json("")
        assert action.type == ActionType.DONE

    def test_no_json_object(self):
        action = parse_action_json("just plain text no braces")
        assert action.type == ActionType.DONE

    def test_missing_type_field(self):
        text = '{"x": 0.5, "y": 0.3}'
        action = parse_action_json(text)
        assert action.type == ActionType.DONE

    def test_scroll_action(self):
        text = '{"type": "scroll", "scroll_direction": "down", "scroll_amount": 3}'
        action = parse_action_json(text)
        assert action.type == ActionType.SCROLL
        assert action.scroll_direction == "down"
        assert action.scroll_amount == 3

    def test_drag_with_nested_drag_end(self):
        text = json.dumps({
            "type": "drag",
            "target": {"x": 0.1, "y": 0.2},
            "drag_end": {"x": 0.8, "y": 0.9},
        })
        action = parse_action_json(text)
        assert action.type == ActionType.DRAG
        assert action.target.x == pytest.approx(0.1)
        assert action.drag_end.x == pytest.approx(0.8)

    def test_drag_with_flat_end_coords(self):
        text = '{"type": "drag", "x": 0.1, "y": 0.2, "end_x": 0.8, "end_y": 0.9}'
        action = parse_action_json(text)
        assert action.type == ActionType.DRAG
        assert action.drag_end.x == pytest.approx(0.8)


# ── Auto-detect (parse_action) ───────────────────────────────────────


class TestParseAction:
    def test_dsl_detected(self):
        action = parse_action("CLICK(x=0.5, y=0.3)")
        assert action.type == ActionType.CLICK
        assert action.target.x == pytest.approx(0.5)

    def test_json_detected(self):
        action = parse_action('{"type": "click", "x": 0.5, "y": 0.3}')
        assert action.type == ActionType.CLICK
        assert action.target.x == pytest.approx(0.5)

    def test_garbage_returns_done(self):
        action = parse_action("some random garbage")
        assert action.type == ActionType.DONE

    def test_empty_returns_done(self):
        action = parse_action("")
        assert action.type == ActionType.DONE

    def test_json_with_fences(self):
        action = parse_action('```json\n{"type": "type", "text": "hello"}\n```')
        assert action.type == ActionType.TYPE
        assert action.text == "hello"

    def test_dsl_with_thought(self):
        text = "Thought: need to type\nAction: TYPE(text=\"hello\")"
        action = parse_action(text)
        assert action.type == ActionType.TYPE
        assert action.text == "hello"
        assert action.reasoning == "need to type"


# ── BenchmarkAction Conversion ───────────────────────────────────────


class TestBenchmarkAction:
    def test_click_with_coords(self):
        data = {"type": "click", "x": 0.5, "y": 0.3}
        action = from_benchmark_action(data)
        assert action.type == ActionType.CLICK
        assert action.target.x == pytest.approx(0.5)
        assert action.target.y == pytest.approx(0.3)

    def test_type_with_text(self):
        data = {"type": "type", "text": "hello world"}
        action = from_benchmark_action(data)
        assert action.type == ActionType.TYPE
        assert action.text == "hello world"

    def test_with_target_node_id(self):
        data = {"type": "click", "x": 0.5, "y": 0.3, "target_node_id": "n42"}
        action = from_benchmark_action(data)
        assert action.target.node_id == "n42"

    def test_with_end_coords_drag(self):
        data = {
            "type": "drag",
            "x": 0.1,
            "y": 0.2,
            "end_x": 0.8,
            "end_y": 0.9,
        }
        action = from_benchmark_action(data)
        assert action.type == ActionType.DRAG
        assert action.drag_end is not None
        assert action.drag_end.x == pytest.approx(0.8)
        assert action.drag_end.y == pytest.approx(0.9)

    def test_with_modifiers(self):
        data = {"type": "click", "x": 100, "y": 200, "modifiers": ["ctrl", "shift"]}
        action = from_benchmark_action(data)
        assert action.modifiers == ["ctrl", "shift"]

    def test_with_target_role_and_name(self):
        data = {
            "type": "click",
            "x": 0.5,
            "y": 0.3,
            "target_role": "button",
            "target_name": "Submit",
        }
        action = from_benchmark_action(data)
        assert action.target.description == "button Submit"

    def test_with_raw_action(self):
        raw = {"original": "data", "source": "benchmark"}
        data = {"type": "click", "x": 0.5, "y": 0.3, "raw_action": raw}
        action = from_benchmark_action(data)
        assert action.raw == raw

    def test_with_answer(self):
        data = {"type": "answer", "answer": "42"}
        action = from_benchmark_action(data)
        assert action.type == ActionType.ANSWER
        assert action.answer == "42"

    def test_missing_type(self):
        data = {"x": 0.5, "y": 0.3}
        action = from_benchmark_action(data)
        assert action.type == ActionType.DONE

    def test_unknown_type(self):
        data = {"type": "teleport", "x": 0.5, "y": 0.3}
        action = from_benchmark_action(data)
        assert action.type == ActionType.DONE


class TestToBenchmarkActionDict:
    def test_click_roundtrip(self):
        data = {"type": "click", "x": 0.5, "y": 0.3}
        action = from_benchmark_action(data)
        result = to_benchmark_action_dict(action)
        assert result["type"] == "click"
        assert result["x"] == pytest.approx(0.5)
        assert result["y"] == pytest.approx(0.3)

    def test_type_roundtrip(self):
        data = {"type": "type", "text": "hello"}
        action = from_benchmark_action(data)
        result = to_benchmark_action_dict(action)
        assert result["type"] == "type"
        assert result["text"] == "hello"

    def test_drag_roundtrip(self):
        data = {"type": "drag", "x": 0.1, "y": 0.2, "end_x": 0.8, "end_y": 0.9}
        action = from_benchmark_action(data)
        result = to_benchmark_action_dict(action)
        assert result["end_x"] == pytest.approx(0.8)
        assert result["end_y"] == pytest.approx(0.9)

    def test_node_id_roundtrip(self):
        data = {"type": "click", "x": 0.5, "y": 0.3, "target_node_id": "n42"}
        action = from_benchmark_action(data)
        result = to_benchmark_action_dict(action)
        assert result["target_node_id"] == "n42"

    def test_modifiers_roundtrip(self):
        data = {"type": "click", "x": 100, "y": 200, "modifiers": ["ctrl"]}
        action = from_benchmark_action(data)
        result = to_benchmark_action_dict(action)
        assert result["modifiers"] == ["ctrl"]

    def test_scroll_roundtrip(self):
        data = {"type": "scroll", "scroll_direction": "down", "scroll_amount": 5}
        action = from_benchmark_action(data)
        result = to_benchmark_action_dict(action)
        assert result["scroll_direction"] == "down"
        assert result["scroll_amount"] == 5

    def test_answer_roundtrip(self):
        data = {"type": "answer", "answer": "42"}
        action = from_benchmark_action(data)
        result = to_benchmark_action_dict(action)
        assert result["answer"] == "42"

    def test_raw_action_roundtrip(self):
        raw = {"source": "benchmark"}
        data = {"type": "click", "x": 0.5, "y": 0.3, "raw_action": raw}
        action = from_benchmark_action(data)
        result = to_benchmark_action_dict(action)
        assert result["raw_action"] == raw

    def test_done_action(self):
        action = Action(type=ActionType.DONE)
        result = to_benchmark_action_dict(action)
        assert result["type"] == "done"
        assert "x" not in result
        assert "text" not in result
