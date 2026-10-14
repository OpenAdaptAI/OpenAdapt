"""Strict parsing helpers for one-action model output envelopes."""

from __future__ import annotations

import json
import re
from typing import Any

from openadapt_evals.errors import ActionParseError

_THOUGHT_ACTION = re.compile(
    r"Thought:\s*[^\r\n]+\r?\nAction:\s*(?P<action>.+)",
    re.IGNORECASE | re.DOTALL,
)
_THINK_ACTION = re.compile(
    r"<think>.+?</think>\s*(?:Action:\s*)?(?P<action>.+)",
    re.IGNORECASE | re.DOTALL,
)
_ACTION_PREFIX = re.compile(r"Action:\s*(?P<action>.+)", re.IGNORECASE | re.DOTALL)
_ACTION_CALL = re.compile(
    r"(?P<command>[A-Za-z_][A-Za-z0-9_]*)\((?P<arguments>.*)\)",
    re.DOTALL,
)
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_single_json_object(text: str) -> dict[str, Any] | None:
    """Return one exact JSON object, or None when the envelope is not JSON."""
    stripped = text.strip()
    if stripped.startswith("```"):
        match = re.fullmatch(
            r"```(?:json)?\s*(?P<body>.*?)\s*```",
            stripped,
            re.IGNORECASE | re.DOTALL,
        )
        if match is None:
            raise ActionParseError("Malformed fenced JSON action envelope")
        stripped = match.group("body").strip()
    elif not stripped.startswith("{"):
        return None

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ActionParseError(f"Duplicate JSON action field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(stripped, object_pairs_hook=reject_duplicates)
    except ActionParseError:
        raise
    except json.JSONDecodeError as exc:
        raise ActionParseError("Malformed or non-exclusive JSON action envelope") from exc
    if not isinstance(value, dict):
        raise ActionParseError("JSON action must be one object")
    return value


def parse_single_dsl_action(
    text: str,
    *,
    allowed_commands: set[str],
) -> tuple[str, dict[str, str]]:
    """Parse one exact optional-thought plus action DSL envelope."""
    envelope = text.strip()
    for pattern in (_THOUGHT_ACTION, _THINK_ACTION, _ACTION_PREFIX):
        match = pattern.fullmatch(envelope)
        if match is not None:
            envelope = match.group("action").strip()
            break

    action_match = _ACTION_CALL.fullmatch(envelope)
    if action_match is None:
        raise ActionParseError("Output must contain exactly one complete action")

    command = action_match.group("command").upper()
    if command not in allowed_commands:
        raise ActionParseError(f"Unsupported action command: {command!r}")
    arguments = _parse_keyword_arguments(action_match.group("arguments"))
    return command, arguments


def require_exact_fields(
    arguments: dict[str, str],
    required: set[str],
    command: str,
) -> None:
    """Require the exact action-specific keyword set."""
    missing = required - arguments.keys()
    extra = arguments.keys() - required
    if missing:
        raise ActionParseError(
            f"{command} requires {', '.join(sorted(missing))}"
        )
    if extra:
        raise ActionParseError(
            f"{command} has unsupported fields: {', '.join(sorted(extra))}"
        )


def _parse_keyword_arguments(arguments: str) -> dict[str, str]:
    """Parse exact comma-separated keyword arguments and reject duplicates."""
    if not arguments.strip():
        return {}

    result: dict[str, str] = {}
    position = 0
    length = len(arguments)
    while position < length:
        while position < length and arguments[position].isspace():
            position += 1
        key_match = _KEY.match(arguments, position)
        if key_match is None:
            raise ActionParseError("Action arguments must be named fields")
        key = key_match.group()
        position = key_match.end()
        while position < length and arguments[position].isspace():
            position += 1
        if position >= length or arguments[position] != "=":
            raise ActionParseError(f"Action field {key!r} is missing '='")
        position += 1
        while position < length and arguments[position].isspace():
            position += 1
        if position >= length:
            raise ActionParseError(f"Action field {key!r} has no value")

        if arguments[position] in ('"', "'"):
            quote = arguments[position]
            position += 1
            value_start = position
            escaped = False
            while position < length:
                char = arguments[position]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    break
                position += 1
            if position >= length:
                raise ActionParseError(f"Action field {key!r} has an open quote")
            value = arguments[value_start:position]
            position += 1
        else:
            value_start = position
            while position < length and arguments[position] not in ",()":
                if arguments[position].isspace():
                    break
                position += 1
            value = arguments[value_start:position]
            if not value:
                raise ActionParseError(f"Action field {key!r} has no value")

        if key in result:
            raise ActionParseError(f"Duplicate action field: {key}")
        result[key] = value

        while position < length and arguments[position].isspace():
            position += 1
        if position == length:
            break
        if arguments[position] != ",":
            raise ActionParseError("Action fields must be comma-separated")
        position += 1
        if not arguments[position:].strip():
            raise ActionParseError("Action arguments cannot end with a comma")

    return result
