"""Closed native form-field wire contract shared by client and approval gate."""

from __future__ import annotations

from typing import Any

from openadapt_flow.backends.win_agent.value_protocol import valid_value_observation

FIELD_KEYS = frozenset(
    {"kind", "rect", "text", "state", "value_proof", "value_identity"}
)
FIELD_KINDS = frozenset({"title_bar", "edit", "value", "text", "state"})
FIELD_STATES = frozenset({None, "on", "off", "indeterminate", "selected", "unselected"})


def valid_form_field(field: Any, *, pid: int | None = None) -> bool:
    """No incomplete native value may authorize either a digest or a mask.

    ``text`` without a value proof is the established visible-label channel;
    value-backed cells must carry a proof. Containers and empty values enter
    the digest but leave their pixels visible. A proof is observation metadata,
    not a signature or caller-provided authorization.
    """
    try:
        if not isinstance(field, dict) or set(field) != FIELD_KEYS:
            return False
        kind, rect, text, state, proof = (
            field[key] for key in ("kind", "rect", "text", "state", "value_proof")
        )
        if (
            kind not in FIELD_KINDS
            or state not in FIELD_STATES
            or not isinstance(text, str)
            or not isinstance(rect, (tuple, list))
            or len(rect) != 4
            or any(type(value) is not int for value in rect)
            or rect[2] <= 0
            or rect[3] <= 0
            or (kind in {"edit", "value"} and (proof is None or state is not None))
            or (kind in {"title_bar", "state"} and proof is not None)
            or (kind == "state" and (text != "" or state is None))
        ):
            return False
        identity = field["value_identity"]
        if proof is None:
            return identity is None
        return valid_value_observation(text, proof, identity, pid=pid)
    except (KeyError, TypeError, ValueError):
        return False
