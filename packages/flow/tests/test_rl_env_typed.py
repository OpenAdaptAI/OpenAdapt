"""openadapt-types actions and MCP tool schemas for the RL environment."""

from __future__ import annotations

import pytest

pytest.importorskip("openenv")

from openadapt_flow.rl_env import typed  # noqa: E402

ELEMENTS = [
    {
        "tag": "input",
        "type": "text",
        "selector": "#name",
        "label": "Name",
        "x": 100,
        "y": 50,
        "w": 200,
        "h": 30,
    },
    {
        "tag": "button",
        "type": "submit",
        "selector": "#save",
        "label": "Save",
        "x": 100,
        "y": 90,
        "w": 80,
        "h": 30,
    },
]
VIEWPORT = (1280, 800)


def test_computer_state_lists_nodes_with_boxes() -> None:
    state = typed.computer_state("http://app/#x", ELEMENTS, VIEWPORT)
    assert [n["node_id"] for n in state["nodes"]] == ["n0", "n1"]
    assert state["nodes"][0]["bbox"] == {"x": 0, "y": 35, "width": 200, "height": 30}
    assert state["nodes"][0]["is_editable"] is True


@pytest.mark.parametrize(
    ("raw", "native"),
    [
        (
            {"type": "click", "target": {"node_id": "n1"}},
            {"kind": "click", "selector": "#save"},
        ),
        (
            {"type": "type", "target": {"node_id": "n0"}, "text": "Ann"},
            {"kind": "fill", "selector": "#name", "text": "Ann"},
        ),
        ({"type": "type", "text": "x"}, {"kind": "type", "text": "x"}),
        (
            {"type": "click", "target": {"x": 0.5, "y": 0.25, "is_normalized": True}},
            {"kind": "click", "x": 640, "y": 200},
        ),
        (
            {"type": "key", "key": "a", "modifiers": ["Control"]},
            {"kind": "press", "text": "Control+a"},
        ),
        ({"type": "scroll", "scroll_direction": "up"}, {"kind": "scroll", "dy": -400}),
        ({"type": "done"}, {"kind": "done", "claim": "success"}),
        ({"type": "fail"}, {"kind": "done", "claim": "failure"}),
    ],
)
def test_openadapt_actions_translate(raw, native) -> None:
    assert typed.native_action(raw, ELEMENTS, VIEWPORT) == native


def test_unknown_node_and_unsupported_action_are_errors() -> None:
    with pytest.raises(ValueError, match="n7"):
        typed.native_action(
            {"type": "click", "target": {"node_id": "n7"}}, ELEMENTS, VIEWPORT
        )
    with pytest.raises(ValueError, match="not available"):
        typed.native_action({"type": "open_app", "app_name": "x"}, ELEMENTS, VIEWPORT)


def test_mcp_tools_cover_every_action_kind() -> None:
    from typing import get_args

    from openadapt_flow.rl_env.environment import ActionKind

    assert set(get_args(ActionKind)) | {"observe"} == typed.TOOL_NAMES
    for tool in typed.TOOLS:
        assert tool["input_schema"]["type"] == "object"
