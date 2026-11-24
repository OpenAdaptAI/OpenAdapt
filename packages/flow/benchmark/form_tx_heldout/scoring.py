"""Score a case's stored submissions against ``answers.json``.

``score_case(case_id, stored_records)`` takes the JSON list returned by the
portal's ``GET /__oracle/submissions`` and returns::

    {"case_id", "submitted", "submit_count", "correct", "record_correct",
     "wrong_fields", "missing_fields", "wrong_details", "expected_behavior"}

Rules:

* Records are filtered to ``record["case_id"] == case_id`` (records with no
  ``case_id`` are kept). ``submit_count`` is the number of such records.
* The LAST stored record is compared. Every field of the answer key's
  ``expected_record`` is compared; there is no subset check. Fields the
  portal stores that are not in ``expected_record`` are metadata
  (``metadata_fields`` per portal) and are ignored.
* ``record_correct``: no wrong and no missing fields.
* ``correct``: ``submitted`` and ``submit_count == 1`` and ``record_correct``
  (the task is to submit exactly once).
* ``missing_fields``: names of expected fields absent from the stored record.
  ``wrong_fields``: names of fields present but unequal after normalization.
  ``wrong_details``: one ``{"field", "expected", "stored"}`` per wrong field.
  With no submission, every expected field is listed as missing.

Normalization, by the field's declared type in ``field_types``:

* ``str`` / ``enum``: Unicode NFKC, case-insensitive (casefold), whitespace
  runs collapsed to one space, ends stripped. No truncation or prefix match.
* ``phone``: digits only.
* ``int`` / ``money``: ``$``, ``,`` and spaces removed, then parsed as a whole
  number; a value with a fractional part or a suffix such as ``K``/``M`` does
  not parse and compares unequal. ``None`` and ``""`` are equal to each other.
* ``date``: ``YYYY-MM-DD`` or ``MM/DD/YYYY``, compared as ISO dates.
* ``bool``: true/false, yes/no, y/n, 1/0, on/off.
* ``list``: lists of objects; each object is normalized with the portal's
  ``item_types`` for that field, and the two lists must be equal as
  multisets (same length, same items, order ignored).
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def load_answer_key() -> dict[str, Any]:
    return json.loads((HERE / "answers.json").read_text())


_MISSING = object()


def _norm_str(v: Any) -> str:
    if v is None:
        return ""
    s = unicodedata.normalize("NFKC", str(v)).casefold()
    return re.sub(r"\s+", " ", s).strip()


def _norm_int(v: Any) -> Any:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return ("bad", str(v))
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else ("bad", repr(v))
    s = re.sub(r"[\s$,]", "", str(v))
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.0+", s):
        return int(s.split(".")[0])
    return ("bad", s)


def _norm_date(v: Any) -> Any:
    s = str(v or "").strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ("bad", s)


def _norm_bool(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    s = _norm_str(v)
    if s in ("true", "yes", "y", "1", "on"):
        return True
    if s in ("false", "no", "n", "0", "off", ""):
        return False
    return ("bad", s)


def normalize(value: Any, ftype: str, item_types: dict[str, str] | None = None) -> Any:
    if ftype in ("str", "enum"):
        return _norm_str(value)
    if ftype == "phone":
        return re.sub(r"\D", "", str(value or ""))
    if ftype in ("int", "money"):
        return _norm_int(value)
    if ftype == "date":
        return _norm_date(value)
    if ftype == "bool":
        return _norm_bool(value)
    if ftype == "list":
        if not isinstance(value, list):
            return ("bad", repr(value))
        types = item_types or {}
        items = []
        for item in value:
            if not isinstance(item, dict):
                return ("bad", repr(value))
            items.append(
                tuple(sorted((k, repr(normalize(item.get(k), t))) for k, t in types.items()))
            )
        return tuple(sorted(items))
    raise ValueError(f"unknown field type {ftype!r}")


def compare_record(portal: str, expected: dict[str, Any], stored: dict[str, Any]) -> tuple[list, list]:
    key = load_answer_key()["portals"][portal]
    ftypes: dict[str, str] = key["field_types"]
    item_types: dict[str, dict[str, str]] = key.get("item_types", {})
    wrong: list[dict[str, Any]] = []
    missing: list[str] = []
    for name, exp in expected.items():
        ftype = ftypes[name]
        got = stored.get(name, _MISSING)
        if got is _MISSING:
            missing.append(name)
            continue
        if normalize(exp, ftype, item_types.get(name)) != normalize(got, ftype, item_types.get(name)):
            wrong.append({"field": name, "expected": exp, "stored": got})
    return wrong, missing


def score_case(case_id: str, stored_records: list[dict[str, Any]]) -> dict[str, Any]:
    ak = load_answer_key()
    case = ak["cases"][case_id]
    recs = [r for r in stored_records if r.get("case_id", case_id) == case_id]
    out: dict[str, Any] = {
        "case_id": case_id,
        "submitted": bool(recs),
        "submit_count": len(recs),
        "expected_behavior": case["expected_behavior"],
    }
    if not recs:
        out.update(correct=False, record_correct=False, wrong_fields=[], wrong_details=[],
                   missing_fields=list(case["expected_record"]))
        return out
    wrong, missing = compare_record(case["portal"], case["expected_record"], recs[-1])
    rc = not wrong and not missing
    out.update(record_correct=rc, correct=rc and len(recs) == 1, wrong_fields=[w["field"] for w in wrong],
               wrong_details=wrong, missing_fields=missing)
    return out
