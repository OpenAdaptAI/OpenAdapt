"""Closed native value and form-field wire formats shared by host consumers."""

from __future__ import annotations

import hashlib
import re
from typing import Any

FIELD_KEYS = frozenset(
    {"kind", "rect", "text", "state", "value_proof", "value_identity"}
)
FIELD_KINDS = frozenset({"title_bar", "edit", "value", "text", "state"})
FIELD_STATES = frozenset({None, "on", "off", "indeterminate", "selected", "unselected"})

# The host checks the format it understands, independently of the deployed
# guest's proof class and version. The guest also runs as a standalone file.
_VALUE_PROOF_KEYS = frozenset(
    {
        "schema",
        "method",
        "hwnd",
        "pid",
        "thread",
        "utf16_units",
        "sha256_utf16le",
        "native_reads",
        "uia_reads",
    }
)


def valid_value_observation(
    value: object, proof: object, identity: object, *, pid: int | None = None
) -> bool:
    if (
        not isinstance(value, str)
        or not isinstance(identity, str)
        or re.fullmatch(r"[0-9a-f]{64}", identity) is None
        or not isinstance(proof, dict)
        or set(proof) != _VALUE_PROOF_KEYS
    ):
        return False
    raw = value.encode("utf-16-le", errors="surrogatepass")
    return (
        proof["schema"] == "openadapt.native-value-completeness/v3"
        and proof["method"] == "uia-value+wm-gettext"
        and all(
            type(proof[k]) is int and proof[k] > 0 for k in ("hwnd", "pid", "thread")
        )
        and type(proof["utf16_units"]) is int
        and 0 <= proof["utf16_units"] <= 65_536
        and proof["utf16_units"] == len(raw) // 2
        and proof["sha256_utf16le"] == hashlib.sha256(raw).hexdigest()
        and type(proof["native_reads"]) is int
        and proof["native_reads"] == 2
        and type(proof["uia_reads"]) is int
        and proof["uia_reads"] == 2
        and (pid is None or proof["pid"] == pid)
    )


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
