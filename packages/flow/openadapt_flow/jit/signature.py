"""The cache key: an explicit task id plus a typed parameter schema.

There is no fuzzy matching. Two tasks share a compiled program only when the
caller gave them the same ``task_id`` and the same parameter names and types.
A changed schema is a different task and starts with a new demonstration.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["PARAM_TYPES", "ParamError", "TaskSignature", "param_text"]

#: Parameter types a task can declare, with the Python types they accept.
PARAM_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}
_PY_TO_PARAM = {str: "string", int: "integer", float: "number", bool: "boolean"}
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class ParamError(ValueError):
    """The run's parameters do not match the task's schema."""


def _type_name(value: Any) -> str:
    if isinstance(value, str) and value in PARAM_TYPES:
        return value
    if isinstance(value, type) and value in _PY_TO_PARAM:
        return _PY_TO_PARAM[value]
    raise ValueError(
        f"unknown parameter type {value!r} (expected one of {sorted(PARAM_TYPES)} "
        "or str, int, float, bool)"
    )


def param_text(value: Any) -> str:
    """The text a parameter value is typed as. Booleans are ``true``/``false``."""

    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass(frozen=True)
class TaskSignature:
    """What a cached program is for.

    Args:
        task_id: A stable id the caller picks, such as ``mockmed.add_note``.
        instruction: The task as told to the computer-use agent. ``{name}``
            placeholders are filled from the run's parameters.
        params: Parameter name to type (``"string"``, ``"integer"``,
            ``"number"``, ``"boolean"``, or the matching Python type).

    The instruction is not part of the key: rewording it does not throw away
    an admitted program. The task id and the schema are.
    """

    task_id: str
    instruction: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _TASK_ID_RE.match(self.task_id):
            raise ValueError(
                f"task_id {self.task_id!r} must match {_TASK_ID_RE.pattern}"
            )
        typed: dict[str, str] = {}
        for name, kind in dict(self.params).items():
            if not _PARAM_RE.match(name):
                raise ValueError(
                    f"parameter name {name!r} must match {_PARAM_RE.pattern}"
                )
            typed[name] = _type_name(kind)
        object.__setattr__(self, "params", dict(sorted(typed.items())))

    @property
    def schema(self) -> dict[str, str]:
        return dict(self.params)

    @property
    def key(self) -> str:
        """``<task_id>-<12 hex>``: the task id and a digest of its schema."""

        canonical = json.dumps(
            {"task_id": self.task_id, "params": self.schema},
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        return f"{self.task_id}-{digest}"

    def validate(self, values: Mapping[str, Any]) -> dict[str, str]:
        """Check ``values`` against the schema; return them as run text.

        Raises:
            ParamError: a parameter is missing, unknown, or the wrong type.
        """

        missing = sorted(set(self.params) - set(values))
        unknown = sorted(set(values) - set(self.params))
        if missing or unknown:
            raise ParamError(
                f"parameters do not match task {self.task_id!r}: "
                f"missing {missing or 'none'}, unknown {unknown or 'none'}"
            )
        out: dict[str, str] = {}
        for name, kind in self.params.items():
            value = values[name]
            # bool is an int subclass; an integer parameter must not take True.
            if kind != "boolean" and isinstance(value, bool):
                raise ParamError(f"parameter {name!r} must be {kind}, got a boolean")
            if not isinstance(value, PARAM_TYPES[kind]):
                raise ParamError(
                    f"parameter {name!r} must be {kind}, got {type(value).__name__}"
                )
            out[name] = param_text(value)
        return out

    def render_instruction(self, values: Mapping[str, Any]) -> str:
        return self.instruction.format(**{k: param_text(v) for k, v in values.items()})

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "params": self.schema, "key": self.key}
