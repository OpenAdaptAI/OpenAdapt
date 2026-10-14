"""Strict one-action envelope regression tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from openadapt_evals.adapters.verl_env import _parse_action_str
from openadapt_evals.errors import ActionParseError
from openadapt_evals.training.standalone.prompt import parse_vlm_output_to_action
from openadapt_evals.training.trl_rollout import parse_action_json

_DSL_PARSERS: tuple[Callable[[str], object], ...] = (
    _parse_action_str,
    parse_action_json,
    parse_vlm_output_to_action,
)
_JSON_PARSERS: tuple[Callable[[str], object], ...] = (
    parse_action_json,
    parse_vlm_output_to_action,
)


@pytest.mark.parametrize("parser", _DSL_PARSERS)
@pytest.mark.parametrize(
    "output",
    [
        "CLICK(x=0.2, y=0.3)\nDONE()",
        "Action: CLICK(x=0.2, y=0.3)\nAction: DONE()",
        "CLICK(x=0.2, y=0.3) trailing command",
        "prefix CLICK(x=0.2, y=0.3)",
        "Thought: choose target\nAction: CLICK(x=0.2, y=0.3)\nextra",
    ],
)
def test_dsl_parser_rejects_multiple_or_extra_commands(
    parser: Callable[[str], object], output: str
) -> None:
    with pytest.raises(ActionParseError):
        parser(output)


@pytest.mark.parametrize("parser", _DSL_PARSERS)
def test_dsl_parser_rejects_duplicate_keyword_fields(
    parser: Callable[[str], object],
) -> None:
    with pytest.raises(ActionParseError, match="Duplicate action field"):
        parser("CLICK(x=0.1, x=0.2, y=0.3)")


@pytest.mark.parametrize("parser", _JSON_PARSERS)
def test_json_parser_rejects_two_objects(parser: Callable[[str], object]) -> None:
    with pytest.raises(ActionParseError):
        parser(
            '{"type":"done","action_type":"done"} '
            '{"type":"done","action_type":"done"}'
        )


@pytest.mark.parametrize("parser", _JSON_PARSERS)
def test_json_parser_rejects_duplicate_fields(parser: Callable[[str], object]) -> None:
    with pytest.raises(ActionParseError, match="Duplicate JSON action field"):
        parser('{"type":"done","type":"done"}')


@pytest.mark.parametrize("parser", _DSL_PARSERS)
def test_optional_thought_with_one_action_remains_valid(
    parser: Callable[[str], object],
) -> None:
    action = parser("Thought: choose the exact target\nAction: CLICK(x=0.2, y=0.3)")
    assert getattr(action, "type") == "click"
