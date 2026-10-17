"""Tests for openadapt-types schemas."""

import json

import pytest

from openadapt_types import (
    Action,
    ActionResult,
    ActionTarget,
    ActionType,
    BoundingBox,
    ComputerState,
    ElementRole,
    Episode,
    FailureCategory,
    FailureRecord,
    ProcessInfo,
    Step,
    UINode,
)


# ── ComputerState ────────────────────────────────────────────────────


class TestBoundingBox:
    def test_center(self):
        bb = BoundingBox(x=100, y=200, width=50, height=30)
        assert bb.center == (125, 215)

    def test_x2_y2(self):
        bb = BoundingBox(x=10, y=20, width=100, height=50)
        assert bb.x2 == 110
        assert bb.y2 == 70

    def test_normalized(self):
        bb = BoundingBox(x=100, y=200, width=50, height=30)
        n = bb.normalized((1000, 1000))
        assert n == (0.1, 0.2, 0.05, 0.03)

    def test_normalized_zero_viewport(self):
        bb = BoundingBox(x=100, y=200, width=50, height=30)
        n = bb.normalized((0, 0))
        assert n == (0.0, 0.0, 0.0, 0.0)


class TestUINode:
    def test_minimal(self):
        node = UINode(node_id="n0")
        assert node.role == "unknown"
        assert node.is_visible is True
        assert node.confidence == 1.0

    def test_full(self):
        node = UINode(
            node_id="n1",
            role=ElementRole.BUTTON,
            name="Submit",
            text="Submit",
            bbox=BoundingBox(x=10, y=20, width=80, height=30),
            parent_id="n0",
            children_ids=["n2", "n3"],
            automation_id="btnSubmit",
            is_focused=True,
        )
        assert node.name == "Submit"
        assert node.bbox.center == (50, 35)
        assert node.is_focused is True


class TestComputerState:
    def test_empty(self):
        state = ComputerState()
        assert state.nodes == []
        assert state.schema_version == "0.1.0"

    def test_with_nodes(self):
        state = ComputerState(
            viewport=(1920, 1080),
            nodes=[
                UINode(node_id="n0", role="window", name="App", children_ids=["n1"]),
                UINode(node_id="n1", role="button", name="OK", parent_id="n0"),
            ],
        )
        assert len(state.nodes) == 2
        assert state.get_node("n1").name == "OK"
        assert state.get_node("n99") is None

    def test_get_children(self):
        state = ComputerState(
            nodes=[
                UINode(node_id="root", children_ids=["a", "b"]),
                UINode(node_id="a", parent_id="root"),
                UINode(node_id="b", parent_id="root"),
            ],
        )
        children = state.get_children("root")
        assert len(children) == 2
        assert {c.node_id for c in children} == {"a", "b"}

    def test_to_text_tree(self):
        state = ComputerState(
            nodes=[
                UINode(node_id="r", role="window", name="App", children_ids=["c"]),
                UINode(node_id="c", role="button", name="OK", parent_id="r"),
            ],
        )
        tree = state.to_text_tree()
        assert "[r] window: App" in tree
        assert "  [c] button: OK" in tree

    def test_json_roundtrip(self):
        state = ComputerState(
            viewport=(800, 600),
            nodes=[UINode(node_id="n0", role="button", name="Test")],
            platform="windows",
        )
        dumped = state.model_dump_json()
        restored = ComputerState.model_validate_json(dumped)
        assert restored.viewport == (800, 600)
        assert restored.nodes[0].name == "Test"

    def test_json_schema_export(self):
        schema = ComputerState.model_json_schema()
        assert "properties" in schema
        assert "nodes" in schema["properties"]


# ── Action ───────────────────────────────────────────────────────────


class TestAction:
    def test_click_with_node_id(self):
        action = Action(
            type=ActionType.CLICK,
            target=ActionTarget(node_id="n5"),
        )
        assert action.target.node_id == "n5"

    def test_click_with_coordinates(self):
        action = Action(
            type=ActionType.CLICK,
            target=ActionTarget(x=512.0, y=384.0),
        )
        assert action.target.x == 512.0

    def test_click_with_description(self):
        action = Action(
            type=ActionType.CLICK,
            target=ActionTarget(description="the submit button"),
        )
        assert action.target.description == "the submit button"

    def test_type_requires_text(self):
        with pytest.raises(ValueError, match="TYPE action requires 'text'"):
            Action(type=ActionType.TYPE)

    def test_type_valid(self):
        action = Action(type=ActionType.TYPE, text="hello world")
        assert action.text == "hello world"

    def test_key_requires_key(self):
        with pytest.raises(ValueError, match="KEY action requires 'key'"):
            Action(type=ActionType.KEY)

    def test_goto_requires_url(self):
        with pytest.raises(ValueError, match="GOTO action requires 'url'"):
            Action(type=ActionType.GOTO)

    def test_drag_requires_drag_end(self):
        with pytest.raises(ValueError, match="DRAG action requires 'drag_end'"):
            Action(type=ActionType.DRAG, target=ActionTarget(x=0, y=0))

    def test_drag_valid(self):
        action = Action(
            type=ActionType.DRAG,
            target=ActionTarget(x=10, y=20),
            drag_end=ActionTarget(x=100, y=200),
        )
        assert action.drag_end.x == 100

    def test_done(self):
        action = Action(type=ActionType.DONE)
        assert action.type == ActionType.DONE

    def test_with_reasoning(self):
        action = Action(
            type=ActionType.CLICK,
            target=ActionTarget(node_id="n0"),
            reasoning="The submit button is visible and ready",
        )
        assert action.reasoning is not None

    def test_json_roundtrip(self):
        action = Action(
            type=ActionType.CLICK,
            target=ActionTarget(node_id="n0", description="submit"),
        )
        dumped = action.model_dump_json()
        restored = Action.model_validate_json(dumped)
        assert restored.target.node_id == "n0"


class TestActionResult:
    def test_success(self):
        result = ActionResult(success=True, duration_ms=150)
        assert result.success is True

    def test_failure(self):
        result = ActionResult(
            success=False,
            error="Element not found",
            error_type="grounding_error",
        )
        assert result.error_type == "grounding_error"


# ── Episode ──────────────────────────────────────────────────────────


class TestEpisode:
    def test_minimal(self):
        episode = Episode(
            episode_id="ep_001",
            instruction="Click the button",
            steps=[
                Step(
                    step_index=0,
                    observation=ComputerState(),
                    action=Action(type=ActionType.CLICK, target=ActionTarget(x=100, y=200)),
                ),
            ],
        )
        assert episode.num_steps == 1

    def test_json_roundtrip(self):
        episode = Episode(
            episode_id="ep_002",
            instruction="Type hello",
            steps=[
                Step(
                    step_index=0,
                    observation=ComputerState(viewport=(800, 600)),
                    action=Action(type=ActionType.TYPE, text="hello"),
                    reasoning="Type into the text field",
                ),
            ],
            success=True,
        )
        json_str = episode.to_json()
        restored = Episode.from_json(json_str)
        assert restored.episode_id == "ep_002"
        assert restored.steps[0].action.text == "hello"
        assert restored.success is True

    def test_json_schema_export(self):
        schema = Episode.model_json_schema()
        assert "properties" in schema


# ── Failure ──────────────────────────────────────────────────────────


class TestFailure:
    def test_failure_record(self):
        record = FailureRecord(
            category=FailureCategory.GROUNDING,
            step_index=3,
            message="Could not find the submit button",
            action_type="click",
            target_description="submit button",
        )
        assert record.category == FailureCategory.GROUNDING
        assert record.step_index == 3
