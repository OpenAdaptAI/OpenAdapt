"""Regressions against a failure rendered as a successful result.

`ActionType.DONE` is a successful terminal outcome: a runner that sees it ends
the episode as complete. Every path that could not read or convert its input
must therefore produce `ActionType.FAIL`, never `DONE`.
"""

from __future__ import annotations

from importlib import metadata

import pytest

import openadapt_types
from openadapt_types import (
    PARSE_ERROR_KEY,
    ActionType,
    ComputerState,
    UINode,
    parse_action,
    parse_action_dsl,
    parse_action_json,
)
from openadapt_types._compat import (
    UNCONVERTIBLE_ACTION_KEY,
    from_benchmark_action,
    from_ml_action,
    from_omnimcp_action_decision,
)


class TestParseFailureIsNotCompletion:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "just some random text with no action",
            "FOOBAR(x=0.5, y=0.3)",
            'CLICK(x="...", y=0.3)',
            "DRAG(x=0.1, y=0.2)",
        ],
    )
    def test_dsl_failure_is_fail_with_a_reason(self, text: str) -> None:
        action = parse_action_dsl(text)
        assert action.type is ActionType.FAIL
        assert action.raw is not None and PARSE_ERROR_KEY in action.raw
        assert action.reasoning and action.reasoning.startswith("parse failure:")

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "{not valid json at all",
            '{"type": "teleport", "x": 0.5, "y": 0.3}',
            '{"x": 0.5, "y": 0.3}',
            "just plain text no braces",
        ],
    )
    def test_json_failure_is_fail_with_a_reason(self, text: str) -> None:
        action = parse_action_json(text)
        assert action.type is ActionType.FAIL
        assert action.raw is not None and PARSE_ERROR_KEY in action.raw

    def test_a_real_done_is_still_done_and_carries_no_parse_error(self) -> None:
        # The distinction this whole change exists for: a model that genuinely
        # reports completion must be separable from a response nobody could
        # read.
        action = parse_action_json('{"type": "done"}')
        assert action.type is ActionType.DONE
        assert action.raw is None

    def test_parse_action_does_not_retry_a_successful_done_as_dsl(self) -> None:
        # `parse_action` used to treat DONE as its failure marker, so a valid
        # JSON `done` was re-parsed as DSL before being returned.
        action = parse_action('{"type": "done", "reasoning": "task complete"}')
        assert action.type is ActionType.DONE
        assert action.reasoning == "task complete"

    def test_parse_action_garbage_is_fail(self) -> None:
        assert parse_action("some random garbage").type is ActionType.FAIL


class TestCompatConversionFailureIsNotCompletion:
    @pytest.mark.parametrize(
        "converter, payload",
        [
            (from_benchmark_action, {"x": 0.5, "y": 0.3}),
            (from_benchmark_action, {"type": "some_future_action"}),
            (from_ml_action, {"coordinates": {"x": 1, "y": 2}}),
            (from_ml_action, {"type": "some_future_action"}),
            (from_omnimcp_action_decision, {"parameters": {}}),
            (from_omnimcp_action_decision, {"action_type": "teleport"}),
        ],
    )
    def test_unconvertible_source_is_fail(self, converter, payload) -> None:
        action = converter(payload)
        assert action.type is ActionType.FAIL
        assert action.raw is not None and UNCONVERTIBLE_ACTION_KEY in action.raw

    def test_a_real_finish_still_converts_to_done(self) -> None:
        action = from_omnimcp_action_decision({"action_type": "finish", "parameters": {}})
        assert action.type is ActionType.DONE


class TestComputerStateLookup:
    def test_children_of_an_unknown_node_raises(self) -> None:
        state = ComputerState(
            nodes=[
                UINode(node_id="root", children_ids=["a"]),
                UINode(node_id="a", parent_id="root"),
            ],
        )

        # "this node is not here" must not look like "this node has no
        # children".
        with pytest.raises(KeyError):
            state.get_children("n99")

        assert state.get_children("a") == []


class TestVersionReporting:
    def test_reported_version_matches_installed_distribution(self) -> None:
        assert openadapt_types.__version__ == metadata.version("openadapt-types")
