"""Turn field values into match booleans before anything leaves the item.

A decision provider sees a fixed vocabulary of numbers and booleans and
nothing else. Names, dates of birth, record numbers and every other typed or
read-back value are compared here, on this computer, and only the result of
the comparison (equal or not, read or not) is kept.

:func:`assert_minimized` is the boundary check. Every
:class:`~openadapt_flow.decision.provider.DecisionRequest` runs it on
construction, so a request that carries a string, an unknown key or a
non-finite number can't be built.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

#: How a field's value is compared. ``identity``, ``date`` and ``id`` are the
#: kinds that must never reach a provider; the others are dropped too, but
#: these three get kind-specific normalisation and their own match booleans.
FieldKind = Literal["identity", "date", "id", "value", "number", "choice"]

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%m/%d/%y",
    "%d.%m.%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
)
_FIELD_NAME = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class PhiBoundaryError(ValueError):
    """A payload bound for a provider carries something other than features."""


class FieldComparison(BaseModel):
    """One intended value and what the record check read back for it.

    Local only. ``expected`` and ``observed`` never leave this module except
    as the booleans :func:`field_matches` returns.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_FIELD_NAME)
    kind: FieldKind = "value"
    expected: Optional[str] = None
    #: ``None`` when the read-back didn't read this field.
    observed: Optional[str] = None


def _collapse(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.split()).casefold()


def _date(text: str) -> Optional[str]:
    cleaned = " ".join(text.strip().split())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _number(text: str) -> Optional[Decimal]:
    try:
        value = Decimal(text.strip().replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def normalize(kind: FieldKind, value: str) -> str:
    """The comparable form of ``value``. Deterministic, no model."""

    if kind == "date":
        parsed = _date(value)
        return parsed if parsed is not None else _collapse(value)
    if kind == "id":
        return re.sub(r"[^0-9a-z]", "", _collapse(value))
    if kind == "number":
        number = _number(value)
        return str(number.normalize()) if number is not None else _collapse(value)
    if kind == "identity":
        return re.sub(r"[^\w ]", "", _collapse(value))
    return _collapse(value)


def field_matches(comparison: FieldComparison) -> Optional[bool]:
    """``True`` or ``False`` for a read field, ``None`` for an unread one."""

    if comparison.observed is None or comparison.expected is None:
        return None
    kind = comparison.kind
    return normalize(kind, comparison.expected) == normalize(kind, comparison.observed)


#: Every name a provider may see, with its type. Nothing else passes.
FEATURE_TYPES: dict[str, type] = {
    "readback_confirmed": bool,
    "readback_reconciled": bool,
    "run_clean": bool,
    "fields_compared": int,
    "fields_unread": int,
    "identity_fields_match": bool,
    "date_fields_match": bool,
    "id_fields_match": bool,
    "value_fields_mismatch": int,
    "all_fields_exact": bool,
    "param_numeric_max_abs_z": float,
    "param_out_of_range": int,
    "param_unseen_values": int,
    "param_history_items": int,
    "intake_min_confidence": float,
    "intake_below_floor": int,
    "intake_unconfirmed": int,
    "intake_from_ocr": bool,
    "intake_from_agent": bool,
    "workflow_recent_declines": int,
    "workflow_decline_rate": float,
}
FEATURE_NAMES: tuple[str, ...] = tuple(FEATURE_TYPES)


def minimize_comparisons(
    comparisons: list[FieldComparison],
) -> tuple[dict[str, bool | int], list[tuple[str, str]]]:
    """Reduce comparisons to match booleans and counts.

    Returns the provider-safe facts and a local list of ``(field, code)``
    flags for the reviewer. The flags name the field, never its value.
    """

    flags: list[tuple[str, str]] = []
    unread = 0
    value_mismatch = 0
    kind_ok = {"identity": True, "date": True, "id": True}
    for comparison in comparisons:
        result = field_matches(comparison)
        if result is None:
            unread += 1
            flags.append((comparison.name, "field_unread"))
            continue
        if result:
            continue
        if comparison.kind in kind_ok:
            kind_ok[comparison.kind] = False
            flags.append((comparison.name, f"{comparison.kind}_mismatch"))
        else:
            value_mismatch += 1
            flags.append((comparison.name, "value_mismatch"))
    facts: dict[str, bool | int] = {
        "fields_compared": len(comparisons),
        "fields_unread": unread,
        "identity_fields_match": kind_ok["identity"],
        "date_fields_match": kind_ok["date"],
        "id_fields_match": kind_ok["id"],
        "value_fields_mismatch": value_mismatch,
        "all_fields_exact": bool(comparisons)
        and unread == 0
        and value_mismatch == 0
        and all(kind_ok.values()),
    }
    return facts, flags


def assert_minimized(features: Mapping[str, Any]) -> dict[str, bool | int | float]:
    """Return ``features`` unchanged if it is provider-safe, else raise.

    Provider-safe means: exactly the names in :data:`FEATURE_NAMES`, each a
    bool, int or finite float of its declared type. A string anywhere is
    refused, whatever it holds.
    """

    if not isinstance(features, Mapping):
        raise PhiBoundaryError("features must be a mapping")
    keys = set(features)
    if keys != set(FEATURE_NAMES):
        unknown = sorted(keys - set(FEATURE_NAMES))
        missing = sorted(set(FEATURE_NAMES) - keys)
        raise PhiBoundaryError(
            f"features must use the fixed vocabulary (unknown={len(unknown)}, "
            f"missing={len(missing)})"
        )
    out: dict[str, bool | int | float] = {}
    for name in FEATURE_NAMES:
        value = features[name]
        want = FEATURE_TYPES[name]
        if want is bool:
            ok = type(value) is bool
        elif want is int:
            ok = type(value) is int
        else:
            ok = type(value) in (int, float) and math.isfinite(float(value))
        if not ok:
            raise PhiBoundaryError(f"feature {name!r} is not a {want.__name__}")
        out[name] = value
    return out


__all__ = [
    "FEATURE_NAMES",
    "FEATURE_TYPES",
    "FieldComparison",
    "FieldKind",
    "PhiBoundaryError",
    "assert_minimized",
    "field_matches",
    "minimize_comparisons",
    "normalize",
]
