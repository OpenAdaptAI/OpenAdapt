"""Workflow cards: what a calling agent needs to choose and call a workflow.

A card gives each workflow a stable ``name``, a one-sentence ``purpose``, what
``done_means`` for it, and a JSON Schema for its ``inputs``. Types and choices
come from Flow's ``param_specs``. Words come from an optional operator-written
``openadapt-card.json`` file next to the bundle's ``workflow.json``::

    {
      "name": "enter_referral",
      "purpose": "Create an outgoing referral for an existing patient in the clinic EMR.",
      "done_means": "The referral appears on the patient's chart in the EMR's referral report.",
      "inputs": {
        "patient_mrn": {"description": "Clinic MRN of the patient.", "pattern": "^[0-9]{6,10}$"},
        "referral_date": {"description": "Date of the referral.", "format": "date"}
      }
    }

Card text reaches the calling agent and, through it, a hosted model. It is
operator-authored, length-limited, and refused when it looks like it carries
an identifier. Recorded example values and step intents never enter a card.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = [
    "CARD_FILENAME",
    "CardError",
    "WorkflowCard",
    "card_for_bundle",
    "coerce_inputs",
    "default_purpose",
    "input_problems",
    "inputs_schema",
]

CARD_FILENAME = "openadapt-card.json"
_LOG = logging.getLogger(__name__)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_TEXT_LIMITS = {"purpose": 300, "done_means": 300, "description": 200}
# Operator text is checked for the shapes identifiers usually take. This is a
# guard against accidents, not a PHI classifier.
_IDENTIFIER_SHAPES = (
    re.compile(r"\d{5,}"),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b\d{3}[-. )]+\d{3}[-. ]+\d{4}\b"),
)
_ALLOWED_INPUT_KEYS = {
    "description",
    "format",
    "pattern",
    "enum",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
}
_FORMATS = {"date", "date-time"}
_FLOW_TYPES = {
    "string": {"type": "string"},
    "entity_ref": {"type": "string"},
    "date": {"type": "string", "format": "date"},
    "enum": {"type": "string"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
}
DEFAULT_DONE_MEANS = (
    "OpenAdapt saved the change and its record check confirmed it in the app's records."
)


def default_purpose() -> str:
    return (
        "A workflow recorded on this computer. Its owner hasn't described it yet, "
        "so ask a person what it does before you run it."
    )


class CardError(ValueError):
    """An operator-written card is invalid. The message never repeats card text."""


@dataclass
class WorkflowCard:
    """The agent-facing description of one workflow."""

    name: str
    purpose: str
    done_means: str
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    changes_records: bool = True
    authored: bool = False

    def projection(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "done_means": self.done_means,
            "inputs": inputs_schema(self),
            "changes_records": self.changes_records,
        }


def _check_text(field_name: str, value: Any) -> str:
    limit = _TEXT_LIMITS[field_name]
    if not isinstance(value, str) or not value.strip():
        raise CardError(f"{field_name} must be non-empty text")
    text = " ".join(value.split())
    if len(text) > limit:
        raise CardError(f"{field_name} must be {limit} characters or fewer")
    if any(shape.search(text) for shape in _IDENTIFIER_SHAPES):
        raise CardError(f"{field_name} looks like it contains an identifier")
    return text


def _flow_schema(param_type: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    kind = (param_type or {}).get("type") or "string"
    schema = dict(_FLOW_TYPES.get(kind, {"type": "string"}))
    choices = [str(choice) for choice in (param_type or {}).get("choices") or []]
    if choices:
        schema["enum"] = choices
    return schema


def _default_description(name: str) -> str:
    return f"The {name.replace('_', ' ')} to enter."


def _merge_input(name: str, base: dict[str, Any], authored: Any) -> dict[str, Any]:
    if not isinstance(authored, Mapping):
        raise CardError("each card input must be an object")
    unknown = set(authored) - _ALLOWED_INPUT_KEYS
    if unknown:
        raise CardError("a card input uses an unsupported key")
    schema = dict(base)
    if "description" in authored:
        schema["description"] = _check_text("description", authored["description"])
    if "format" in authored:
        if authored["format"] not in _FORMATS or schema.get("type") != "string":
            raise CardError("format must be date or date-time on a text input")
        schema["format"] = authored["format"]
    if "pattern" in authored:
        pattern = authored["pattern"]
        if not isinstance(pattern, str) or len(pattern) > 200:
            raise CardError("pattern must be a short regular expression")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise CardError("pattern is not a valid regular expression") from exc
        schema["pattern"] = pattern
    if "enum" in authored:
        values = authored["enum"]
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) and value for value in values)
        ):
            raise CardError("enum must be a non-empty list of text values")
        if "enum" in schema and not set(values) <= set(schema["enum"]):
            raise CardError("enum can only narrow the workflow's own choices")
        schema["enum"] = list(values)
    for key in ("minLength", "maxLength"):
        if key in authored:
            value = authored[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CardError(f"{key} must be a whole number")
            schema[key] = value
    for key in ("minimum", "maximum"):
        if key in authored:
            value = authored[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise CardError(f"{key} must be a number")
            schema[key] = value
    return schema


def card_for_bundle(info: Any, *, default_name: str) -> WorkflowCard:
    """Build the card for one discovered bundle.

    ``info`` is a :class:`openadapt_agent.bundles.WorkflowInfo`. A missing card
    file gives a plain default card. An invalid one is ignored with a local
    warning, so a typo can't stop the server or leak its text.
    """
    params = sorted(getattr(info, "params", {}) or {})
    param_types = getattr(info, "param_types", {}) or {}
    inputs = {
        name: {**_flow_schema(param_types.get(name)), "description": _default_description(name)}
        for name in params
    }
    card = WorkflowCard(
        name=default_name,
        purpose=default_purpose(),
        done_means=DEFAULT_DONE_MEANS,
        inputs=inputs,
        required=list(params),
        changes_records=bool(getattr(info, "changes_records", True)),
    )
    path = Path(getattr(info, "bundle_dir", ".")) / CARD_FILENAME
    if not path.is_file() or path.is_symlink():
        return card
    try:
        authored = json.loads(path.read_text(encoding="utf-8"))
        return _apply_card(card, authored)
    except (OSError, ValueError, CardError) as exc:
        reason = str(exc) if isinstance(exc, CardError) else type(exc).__name__
        _LOG.warning("ignoring workflow card for %s: %s", default_name, reason)
        return card


def _apply_card(card: WorkflowCard, authored: Any) -> WorkflowCard:
    if not isinstance(authored, Mapping):
        raise CardError("the card must be a JSON object")
    unknown = set(authored) - {"name", "purpose", "done_means", "inputs"}
    if unknown:
        raise CardError("the card uses an unsupported key")
    name = authored.get("name", card.name)
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise CardError("name must be 3-64 lowercase letters, digits, or underscores")
    inputs = dict(card.inputs)
    authored_inputs = authored.get("inputs", {})
    if not isinstance(authored_inputs, Mapping):
        raise CardError("inputs must be an object")
    if set(authored_inputs) - set(inputs):
        raise CardError("the card describes an input the workflow doesn't have")
    for input_name, spec in authored_inputs.items():
        inputs[input_name] = _merge_input(input_name, inputs[input_name], spec)
    return WorkflowCard(
        name=name,
        purpose=_check_text("purpose", authored["purpose"])
        if "purpose" in authored
        else card.purpose,
        done_means=_check_text("done_means", authored["done_means"])
        if "done_means" in authored
        else card.done_means,
        inputs=inputs,
        required=list(card.required),
        changes_records=card.changes_records,
        authored=True,
    )


def inputs_schema(card: WorkflowCard, *, require_all: bool = True) -> dict[str, Any]:
    """JSON Schema for the ``inputs`` object of ``run_workflow``."""
    return {
        "type": "object",
        "properties": {name: dict(schema) for name, schema in card.inputs.items()},
        "required": sorted(card.required) if require_all else [],
        "additionalProperties": False,
    }


def _valid_date(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _valid_datetime(value: str) -> bool:
    try:
        _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return "T" in value or " " in value


def _problem(schema: Mapping[str, Any], value: Any) -> Optional[str]:
    kind = schema.get("type", "string")
    if kind == "string":
        if not isinstance(value, str):
            return "must be text"
        if "enum" in schema and value not in schema["enum"]:
            return "must be one of the listed values"
        if "minLength" in schema and len(value) < schema["minLength"]:
            return "is too short"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return "is too long"
        if schema.get("format") == "date" and not _valid_date(value):
            return "must be a date like 2026-10-08"
        if schema.get("format") == "date-time" and not _valid_datetime(value):
            return "must be a date and time like 2026-10-08T09:30:00"
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return "doesn't match the expected format"
        return None
    if kind == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return "must be a number"
    elif kind == "boolean":
        if not isinstance(value, bool):
            return "must be true or false"
        return None
    if "minimum" in schema and value < schema["minimum"]:
        return "is too small"
    if "maximum" in schema and value > schema["maximum"]:
        return "is too large"
    return None


def input_problems(
    card: WorkflowCard, inputs: Any, *, require_all: bool = True
) -> list[dict[str, str]]:
    """Closed-vocabulary problems with ``inputs``. Values are never repeated."""
    if not isinstance(inputs, Mapping):
        return [{"input": "inputs", "problem": "must be an object"}]
    problems: list[dict[str, str]] = []
    if set(inputs) - set(card.inputs):
        # Never echo a caller-supplied key: it could itself carry a value.
        problems.append(
            {
                "input": "(unknown)",
                "problem": "isn't an input of this workflow; list_workflows has the names",
            }
        )
    for name, schema in sorted(card.inputs.items()):
        if name not in inputs:
            if require_all and name in card.required:
                problems.append({"input": name, "problem": "is missing"})
            continue
        problem = _problem(schema, inputs[name])
        if problem is not None:
            problems.append({"input": name, "problem": problem})
    return problems


def coerce_inputs(card: WorkflowCard, inputs: Mapping[str, Any]) -> dict[str, str]:
    """Render validated inputs as the strings Flow's ``--params-file`` takes."""
    rendered: dict[str, str] = {}
    for name, value in inputs.items():
        if isinstance(value, bool):
            rendered[name] = "true" if value else "false"
        elif isinstance(value, float) and value.is_integer():
            rendered[name] = str(int(value))
        else:
            rendered[name] = str(value)
    return rendered
