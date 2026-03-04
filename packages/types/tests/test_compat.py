"""Tests for backward-compatibility converters."""

from openadapt_types import ActionType, ComputerState
from openadapt_types._compat import (
    from_benchmark_action,
    from_benchmark_observation,
    from_ml_action,
    from_ml_observation,
    from_omnimcp_action_decision,
    from_omnimcp_screen_state,
)


class TestBenchmarkCompat:
    def test_observation(self):
        obs = {
            "screenshot": b"\x89PNG...",
            "screenshot_path": "/tmp/screenshot.png",
            "viewport": (1920, 1080),
            "accessibility_tree": {"role": "window", "children": []},
            "dom_html": "<html></html>",
            "url": "https://example.com",
            "window_title": "My App",
            "app_name": "Chrome",
            "focused_element": {"node_id": "n5", "bbox": [10, 20, 100, 50]},
            "raw_observation": {"custom": "data"},
        }
        state = from_benchmark_observation(obs)
        assert isinstance(state, ComputerState)
        assert state.screenshot_png == b"\x89PNG..."
        assert state.viewport == (1920, 1080)
        assert state.url == "https://example.com"
        assert state.focused_node_id == "n5"
        assert state.raw == {"custom": "data"}

    def test_action_click(self):
        act = {
            "type": "click",
            "x": 512.0,
            "y": 384.0,
            "target_node_id": "btn_submit",
            "text": None,
            "key": None,
            "modifiers": None,
        }
        action = from_benchmark_action(act)
        assert action.type == ActionType.CLICK
        assert action.target.node_id == "btn_submit"
        assert action.target.x == 512.0

    def test_action_type(self):
        act = {"type": "type", "text": "hello", "x": None, "y": None}
        action = from_benchmark_action(act)
        assert action.type == ActionType.TYPE
        assert action.text == "hello"

    def test_action_drag(self):
        act = {
            "type": "drag",
            "x": 10.0, "y": 20.0,
            "end_x": 100.0, "end_y": 200.0,
        }
        action = from_benchmark_action(act)
        assert action.type == ActionType.DRAG
        assert action.drag_end.x == 100.0

    def test_unknown_type(self):
        act = {"type": "some_future_action"}
        action = from_benchmark_action(act)
        assert action.type == ActionType.DONE  # fallback


class TestMLCompat:
    def test_observation(self):
        obs = {
            "screenshot_path": "/tmp/step_0.png",
            "screenshot_base64": None,
            "a11y_tree": {"role": "desktop"},
            "dom": None,
            "window_title": "Settings",
            "app_name": "System Settings",
            "url": None,
            "screen_size": (1920, 1080),
            "timestamp": 1709500000.0,
            "raw": None,
        }
        state = from_ml_observation(obs)
        assert state.screenshot_path == "/tmp/step_0.png"
        assert state.viewport == (1920, 1080)
        assert state.timestamp == 1709500000.0

    def test_action_with_coordinates(self):
        act = {
            "type": "click",
            "coordinates": {"x": 500, "y": 300},
            "normalized_coordinates": None,
            "element": None,
            "text": None,
            "key": None,
            "modifiers": None,
            "raw": None,
        }
        action = from_ml_action(act)
        assert action.type == ActionType.CLICK
        assert action.target.x == 500.0

    def test_action_with_normalized(self):
        act = {
            "type": "click",
            "coordinates": None,
            "normalized_coordinates": (0.5, 0.375),
            "element": None,
        }
        action = from_ml_action(act)
        assert action.target.is_normalized is True
        assert action.target.x == 0.5

    def test_action_with_element(self):
        act = {
            "type": "click",
            "coordinates": None,
            "normalized_coordinates": None,
            "element": {"element_id": "btn42", "role": "button"},
        }
        action = from_ml_action(act)
        assert action.target.node_id == "btn42"


class TestOmniMCPCompat:
    def test_screen_state(self):
        state = {
            "elements": [
                {
                    "id": 0,
                    "type": "button",
                    "content": "Submit",
                    "bounds": (0.1, 0.2, 0.05, 0.03),
                    "confidence": 0.95,
                    "attributes": {"enabled": True},
                },
                {
                    "id": 1,
                    "type": "text_field",
                    "content": "Username",
                    "bounds": (0.3, 0.2, 0.2, 0.03),
                    "confidence": 0.9,
                    "attributes": {},
                },
            ],
            "dimensions": (1920, 1080),
            "timestamp": 1709500000.0,
        }
        cs = from_omnimcp_screen_state(state)
        assert len(cs.nodes) == 2
        assert cs.nodes[0].node_id == "0"
        assert cs.nodes[0].role == "button"
        assert cs.nodes[0].text == "Submit"
        assert cs.nodes[0].confidence == 0.95
        assert cs.viewport == (1920, 1080)
        assert cs.source == "omnimcp"
        # Check bbox was denormalized
        assert cs.nodes[0].bbox.x == int(0.1 * 1920)

    def test_action_decision_click(self):
        decision = {
            "action_type": "click",
            "target_element_id": 5,
            "parameters": {},
            "analysis_reasoning": "Clicking the submit button to proceed",
            "is_goal_complete": False,
        }
        action = from_omnimcp_action_decision(decision)
        assert action.type == ActionType.CLICK
        assert action.target.node_id == "5"
        assert action.reasoning == "Clicking the submit button to proceed"

    def test_action_decision_type(self):
        decision = {
            "action_type": "type",
            "target_element_id": 3,
            "parameters": {"text_to_type": "admin"},
            "analysis_reasoning": "Typing username",
        }
        action = from_omnimcp_action_decision(decision)
        assert action.type == ActionType.TYPE
        assert action.text == "admin"

    def test_action_decision_finish(self):
        decision = {
            "action_type": "finish",
            "target_element_id": None,
            "parameters": {},
            "analysis_reasoning": "Goal achieved",
            "is_goal_complete": True,
        }
        action = from_omnimcp_action_decision(decision)
        assert action.type == ActionType.DONE
