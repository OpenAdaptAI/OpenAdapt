"""Generate input types and conversions from sample source documents.

Each demonstration pairs a source document (a referral, a claim form) with
the values the person typed from it. For every parameter the generator finds
the document line whose value, converted, is what the person typed:

- the **label** before it (an alias for intake's label parse);
- the **type**: text, or a date when the document writes it differently from
  the canonical ISO date the person typed;
- for a numeric date, the **source's order** (``MM/DD/YYYY`` or
  ``DD/MM/YYYY``), from a date that can only be read one way or from the
  person's own reading of an ambiguous one; samples that disagree, or none
  that decide, leave the order undeclared and every ambiguous date halts;
- a **code table** when the record stores a code for what the document
  writes out (``Massachusetts`` -> ``MA``), learned one entry per value the
  demonstrations show; any other value halts.

Admission is the round trip: every sample, read back with the generated
labels and converted, must give exactly what the person typed. The document
is data: a line that reads like an instruction is a line.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Optional

from openadapt_flow.intake import extract as _extract

if True:  # the same rules as intake's normalize_date (PR #1514), until it lands

    def _fallback_normalize_date(
        raw: str, date_format: Optional[str] = None
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        value = raw.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            try:
                return _date.fromisoformat(value).isoformat(), None, None
            except ValueError:
                return None, f"not a valid date: {value!r}", None
        m = re.fullmatch(r"(\d{1,2})([/-])(\d{1,2})\2(\d{4})", value)
        if not m:
            return None, f"not a recognized date: {value!r}", None
        a, b, year = int(m.group(1)), int(m.group(3)), int(m.group(4))
        if date_format is not None:
            month_first = date_format == "MM/DD/YYYY"
        elif a > 12 >= b:
            month_first = False
        elif b > 12 >= a:
            month_first = True
        elif a == b:
            month_first = True
        else:
            return None, f"ambiguous date {value!r}; declare the source's format", None
        month, day = (a, b) if month_first else (b, a)
        try:
            return _date(year, month, day).isoformat(), None, "numeric to ISO"
        except ValueError:
            return None, f"not a valid date: {value!r}", None


_normalize_date = getattr(_extract, "normalize_date", None) or _fallback_normalize_date

DATE_ORDERS = ("MM/DD/YYYY", "DD/MM/YYYY")
_SEP = re.compile(r"\s*(?:[:#=]|-\s)\s*")


def _norm(value: Any) -> str:
    return " ".join(("" if value is None else str(value)).split()).casefold()


@dataclass
class Sample:
    """One source document and what the person typed from it."""

    lines: list[str]
    typed: dict[str, str]


@dataclass
class InputPart:
    name: str
    kind: str = "text"  # text | date | code_table
    labels: list[str] = field(default_factory=list)
    date_format: Optional[str] = None
    #: code_table: what the document writes -> what the record stores.
    table: dict[str, str] = field(default_factory=dict)
    #: code_table: the record field the code goes to, and the parameter that
    #: carries it.
    stores_as: Optional[str] = None
    status: str = "ok"  # ok | not_found | needs_format | inconsistent
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "labels": self.labels,
            "status": self.status,
        }
        if self.date_format:
            out["date_format"] = self.date_format
        if self.table:
            out["table"] = dict(sorted(self.table.items()))
            out["stores_as"] = self.stores_as
        if self.notes:
            out["notes"] = self.notes
        return out

    def describe(self) -> str:
        where = ", ".join(repr(label) for label in self.labels) or "no label"
        if self.status != "ok":
            return f"{self.name}: {self.status} ({'; '.join(self.notes)})"
        if self.kind == "date":
            order = (
                f"numeric dates read {self.date_format}"
                if self.date_format
                else "an ambiguous numeric date halts"
            )
            return f"{self.name}: a date, from {where}; {order}"
        if self.kind == "code_table":
            pairs = ", ".join(f"{k} -> {v}" for k, v in sorted(self.table.items()))
            return (
                f"{self.name}: from {where}, stored as {self.stores_as} by the "
                f"table {pairs}; any other value halts"
            )
        return f"{self.name}: text, from {where}, kept as written"


def _split(line: str) -> Optional[tuple[str, str]]:
    parts = _SEP.split(line, maxsplit=1)
    if len(parts) != 2:
        return None
    label, value = parts[0].strip(), parts[1].strip()
    return (label, value) if label and value else None


def _date_readings(raw: str) -> dict[str, str]:
    """Order -> ISO, for each order a numeric date can be read in."""

    out: dict[str, str] = {}
    for order in DATE_ORDERS:
        iso, error, _conv = _normalize_date(raw, order)
        if iso and not error:
            out[order] = iso
    return out


def generate_inputs(
    samples: Sequence[Sample],
    *,
    codes: Optional[Mapping[str, Sequence[str]]] = None,
) -> list[InputPart]:
    """One part per parameter the samples typed.

    ``codes`` maps a parameter to the record values its demonstrations stored
    (one per sample, in order), when the contract found the record stores
    something other than what was typed (``state_label`` -> ``state``).
    """

    if not samples:
        return []
    names = sorted(samples[0].typed)
    parts: list[InputPart] = []
    for name in names:
        part = InputPart(name)
        orders: set[str] = set()
        undecided = 0
        kinds: set[str] = set()
        for n, sample in enumerate(samples, start=1):
            typed = sample.typed.get(name, "")
            found: Optional[tuple[str, str, Optional[str]]] = None
            for line in sample.lines:
                split = _split(line)
                if split is None:
                    continue
                label, value = split
                if _norm(value) == _norm(typed):
                    iso_typed = re.fullmatch(r"\d{4}-\d{2}-\d{2}", typed.strip())
                    found = (label, "date" if iso_typed else "text", None)
                    break
                readings = _date_readings(value)
                matching = sorted(o for o, iso in readings.items() if iso == typed)
                if matching:
                    iso, error, _c = _normalize_date(value)
                    decided = iso == typed and not error
                    if len(set(readings.values())) == 1:
                        found = (label, "date", None)  # reads the same either way
                    elif decided:
                        found = (label, "date", matching[0])
                    else:
                        found = (
                            label,
                            "date",
                            matching[0] if len(matching) == 1 else None,
                        )
                    break
            if found is None:
                part.status = "not_found"
                part.notes.append(f"sample {n}: no line holds the typed value")
                continue
            label, kind, order = found
            if label not in part.labels:
                part.labels.append(label)
            kinds.add(kind)
            if kind == "date":
                if order:
                    orders.add(order)
                else:
                    undecided += 1
        if part.status != "ok":
            parts.append(part)
            continue
        if kinds == {"date"} or (kinds == {"text", "date"}):
            part.kind = "date"
            if len(orders) == 1:
                part.date_format = orders.pop()
            elif len(orders) > 1:
                part.status = "inconsistent"
                part.notes.append("the samples read numeric dates in both orders")
        if codes and name in codes:
            stored = list(codes[name])
            table: dict[str, str] = {}
            for sample, value in zip(samples, stored):
                key = sample.typed.get(name, "")
                if key in table and table[key] != value:
                    part.status = "inconsistent"
                    part.notes.append(f"{key!r} was stored as two codes")
                table[key] = value
            part.kind = "code_table"
            part.table = table
        parts.append(part)
    return parts


@dataclass
class RoundTrip:
    sample: int
    name: str
    result: str  # match | mismatch | halt
    detail: str = ""


def read_value(part: InputPart, lines: Sequence[str]) -> tuple[Optional[str], str]:
    """Read ``part`` from a document: (value, problem). Ambiguity halts."""

    hits = []
    for line in lines:
        split = _split(line)
        if split is None:
            continue
        label, value = split
        if any(_norm(label) == _norm(known) for known in part.labels):
            hits.append(value)
    if not hits:
        return None, "no line with a known label"
    if len({_norm(h) for h in hits}) > 1:
        return None, "two lines with known labels disagree"
    raw = hits[0]
    if part.kind == "date":
        iso, error, _conv = _normalize_date(raw, part.date_format)
        if error or not iso:
            return None, error or "not a date"
        return iso, ""
    return raw, ""


def convert(part: InputPart, value: str) -> tuple[Optional[str], str]:
    """The stored form of ``value``; an unknown code halts."""

    if part.kind != "code_table":
        return value, ""
    for key, code in part.table.items():
        if _norm(key) == _norm(value):
            return code, ""
    return None, f"{value!r} is not in the learned table; a person maps it"


def round_trip(
    parts: Sequence[InputPart], samples: Sequence[Sample]
) -> list[RoundTrip]:
    """Read every sample back with the generated parts; compare with the typing."""

    out: list[RoundTrip] = []
    for n, sample in enumerate(samples, start=1):
        for part in parts:
            if part.status != "ok":
                out.append(RoundTrip(n, part.name, "halt", part.status))
                continue
            value, problem = read_value(part, sample.lines)
            if value is None:
                out.append(RoundTrip(n, part.name, "halt", problem))
            elif _norm(value) == _norm(sample.typed.get(part.name, "")):
                out.append(RoundTrip(n, part.name, "match"))
            else:
                out.append(
                    RoundTrip(n, part.name, "mismatch", "read a different value")
                )
    return out


def admitted(trips: Sequence[RoundTrip]) -> bool:
    return bool(trips) and all(t.result == "match" for t in trips)
