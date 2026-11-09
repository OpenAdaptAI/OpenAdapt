"""The cache key: an explicit task id plus a typed parameter schema.

There is no fuzzy matching. Two tasks share a compiled program only when the
caller gave them the same ``task_id`` and the same parameter names and types.
A changed schema is a different task and starts with a new demonstration.

Reuse is also bound to intent. A caller can pin the record check it approved
(:func:`record_check_digest`) and the app environment it runs in; both enter
the key. Whether or not the caller pins them, the admission manifest records
both, and a run refuses an admitted program whose record check or environment
differs from the one admitted (see :mod:`openadapt_flow.jit.compile_agent`).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

__all__ = [
    "PARAM_TYPES",
    "ParamError",
    "TaskSignature",
    "param_text",
    "record_check_digest",
    "record_check_of",
]

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
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


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
        record_check: Optional. The digest of the record check the caller
            approved (:func:`record_check_digest` of the admitted program).
            A program whose record check differs is never reused for it.
        environment: Optional. The app environment the task is for, as the
            app reports it (for example ``"https://emr.example.test#7.0.2"``,
            see ``WebApp.environment``). A program admitted in another
            environment is never reused for it.

    The instruction is not part of the key: rewording it does not throw away
    an admitted program. The task id and the schema are, and so are
    ``record_check`` and ``environment`` when given. Leaving them out keeps
    the key a task had before they existed.
    """

    task_id: str
    instruction: str
    params: Mapping[str, Any] = field(default_factory=dict)
    record_check: Optional[str] = None
    environment: Optional[str] = None

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
        if self.record_check is not None and not _DIGEST_RE.match(self.record_check):
            raise ValueError(
                "record_check must be a sha256 hex digest (record_check_digest)"
            )
        if self.environment is not None and not self.environment.strip():
            raise ValueError("environment must not be empty")

    @property
    def schema(self) -> dict[str, str]:
        return dict(self.params)

    @property
    def key(self) -> str:
        """``<task_id>-<12 hex>``: the task id and a digest of its schema,
        and of the pinned record check and environment when given."""

        body: dict[str, Any] = {"task_id": self.task_id, "params": self.schema}
        if self.record_check is not None:
            body["record_check"] = self.record_check
        if self.environment is not None:
            body["environment"] = self.environment
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
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
        out: dict[str, Any] = {
            "task_id": self.task_id,
            "params": self.schema,
            "key": self.key,
        }
        if self.record_check is not None:
            out["record_check"] = self.record_check
        if self.environment is not None:
            out["environment"] = self.environment
        return out


def record_check_of(workflow: Any, *, literals: bool = True) -> dict[str, Any]:
    """The program's record check: what each saving step must leave behind.

    For every step the run gate treats as a save (fail-closed), its effect
    contracts (which name parameters, never values) and a digest of its
    record identity contract, in step order. Anchors,
    templates and locators are left out, so a repair that re-anchors a step
    or adds a bridge before it keeps the same record check, while any change
    to what is saved, or to which record, changes it.

    ``literals=False`` replaces every literal value in a contract with
    ``"<literal>"`` and keeps parameter references: the view a repair agent
    gets, since a contract may pin a value from a real record.
    """

    from openadapt_flow.run_gate import is_consequential

    saves = []
    for step in workflow.steps:
        if not is_consequential(step, workflow):
            continue
        saves.append(
            {
                "step_id": step.id,
                "intent": step.intent or "",
                "effects": [
                    _contract(e.model_dump(mode="json"), literals) for e in step.effects
                ],
                "identity": _identity_of(step),
            }
        )
    return {"workflow": getattr(workflow, "name", None), "saves": saves}


_VALUE_KEYS = ("match", "value", "new_records")


def _masked(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) <= {"literal", "param"}:
            # A ValueExpr: keep a parameter reference, mask a literal.
            if value.get("param") is not None:
                return {"param": value["param"]}
            return "<literal>"
        return {k: _masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return None if value is None else "<literal>"


def _contract(effect: dict[str, Any], literals: bool) -> dict[str, Any]:
    if literals:
        return effect
    return {k: (_masked(v) if k in _VALUE_KEYS else v) for k, v in effect.items()}


def _identity_of(step: Any) -> Optional[dict[str, str]]:
    """The save's record identity contract, as a sha256 per field.

    Older bundles carry the recorded identity as plain text, so only digests
    leave this function: a repair agent shown the record check never sees a
    name or a record number.
    """

    anchor = getattr(step, "anchor", None)
    if anchor is None:
        return None
    out = {}
    for name in ("identity_template", "structured_identity", "context_text"):
        value = getattr(anchor, name, None)
        if value is None:
            continue
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        out[name] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return out or None


def record_check_digest(workflow: Any) -> str:
    """sha256 hex of :func:`record_check_of`, step ids and intents left out.

    The contracts are what a person approved. A step id or the intent's
    wording can change in a repair without changing what is saved or where.
    """

    check = record_check_of(workflow)
    body = [
        {k: v for k, v in save.items() if k not in ("step_id", "intent")}
        for save in check["saves"]
    ]
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
