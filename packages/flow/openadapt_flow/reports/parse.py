"""Read a later report into rows of ``(key, status)``.

Some saves can't be read back right away. A claim is confirmed by the
payer's acknowledgment hours later; an order shows up in tonight's export.
These parsers turn such a report into rows a pending check can be matched
against. Each row says what the report says about one item: ``accepted``,
``rejected``, or ``pending`` (the report mentions it but hasn't decided).

Formats:

- ``csv``: an export or end-of-day file with a key column and a status
  column. The deployment names the columns and which status values accept or
  reject.
- ``x12-999``: the implementation acknowledgment for an X12 transaction set.
  The key is the transaction set control number (``AK2-02``); ``IK5-01`` ``A``
  or ``E`` accepts, ``R``, ``M``, ``W`` or ``X`` rejects.
- ``x12-277ca``: the claim acknowledgment. The key is the claim's patient
  control number (``TRN-02`` at the claim level); ``STC-01-1`` ``A2`` accepts
  (accepted into adjudication), ``A3``, ``A4``, ``A6``, ``A7`` or ``A8``
  rejects, and anything else (``A1``, received) is pending.
- ``text``: a status page saved as text. A regular expression with named
  groups ``key`` and ``status`` finds the rows.

A parser never guesses. A report it can't read raises :class:`ReportError`;
a row with no key is skipped; two rows for one key that disagree make that key
``conflicting``, which never confirms anything.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Iterable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

RowStatus = Literal["accepted", "rejected", "pending", "conflicting"]

FORMATS = ("csv", "x12-999", "x12-277ca", "text")


class ReportError(ValueError):
    """The report could not be read. The message carries no report content."""


@dataclass(frozen=True)
class ReportRow:
    key: str
    status: RowStatus
    #: The raw status code as the report gives it (``A2``, ``R``, ``Paid``).
    code: str


class ReportFormat(BaseModel):
    """How to read one kind of report."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["csv", "x12-999", "x12-277ca", "text"]
    #: csv: the column holding the item's key, and the status column.
    key_column: Optional[str] = None
    status_column: Optional[str] = None
    #: csv and text: status values that accept or reject (compared after
    #: trimming and case folding). Anything else is pending.
    accept: list[str] = Field(default_factory=list)
    reject: list[str] = Field(default_factory=list)
    #: text: a regular expression with named groups ``key`` and ``status``.
    pattern: Optional[str] = None

    @model_validator(mode="after")
    def _shape(self) -> "ReportFormat":
        if self.format == "csv":
            if not self.key_column or not self.status_column:
                raise ValueError("a csv report needs key_column and status_column")
            if not self.accept:
                raise ValueError("a csv report needs the status values that accept")
        if self.format == "text":
            if not self.pattern:
                raise ValueError("a text report needs a pattern")
            try:
                compiled = re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"the text pattern does not compile: {exc}") from exc
            if not {"key", "status"} <= set(compiled.groupindex):
                raise ValueError("the text pattern needs named groups key and status")
            if not self.accept:
                raise ValueError("a text report needs the status values that accept")
        overlap = {v.casefold() for v in self.accept} & {
            v.casefold() for v in self.reject
        }
        if overlap:
            raise ValueError(
                f"a status can't both accept and reject: {sorted(overlap)}"
            )
        return self


def _classify(code: str, accept: Iterable[str], reject: Iterable[str]) -> RowStatus:
    folded = code.strip().casefold()
    if folded in {v.strip().casefold() for v in accept}:
        return "accepted"
    if folded in {v.strip().casefold() for v in reject}:
        return "rejected"
    return "pending"


def _segments(text: str) -> list[list[str]]:
    """X12 segments, split by the separators the ISA header declares."""

    body = text.lstrip("﻿").strip()
    if not body.startswith("ISA") or len(body) < 106:
        raise ReportError("not an X12 interchange (no ISA header)")
    element = body[3]
    segment_end = body[105]
    if element.isalnum() or segment_end.isalnum():
        raise ReportError("the X12 ISA header declares invalid separators")
    segments = []
    for raw in body.split(segment_end):
        raw = raw.strip()
        if raw:
            segments.append(raw.split(element))
    return segments


def _component_separator(text: str) -> str:
    body = text.lstrip("﻿").strip()
    return body[104] if len(body) > 104 else ":"


def parse_999(text: str) -> list[ReportRow]:
    segments = _segments(text)
    if not any(s[0] == "ST" and len(s) > 1 and s[1] == "999" for s in segments):
        raise ReportError("not a 999 acknowledgment (no ST*999)")
    rows: list[ReportRow] = []
    current: Optional[str] = None
    for seg in segments:
        tag = seg[0]
        if tag == "AK2":
            current = seg[2].strip() if len(seg) > 2 else None
        elif tag == "IK5" and current:
            code = seg[1].strip() if len(seg) > 1 else ""
            rows.append(
                ReportRow(
                    key=current,
                    status=_classify(code, ("A", "E"), ("R", "M", "W", "X")),
                    code=code,
                )
            )
            current = None
    return rows


#: 277CA claim status category codes (STC-01-1).
_277CA_ACCEPT = ("A2",)
_277CA_REJECT = ("A3", "A4", "A6", "A7", "A8")


def parse_277ca(text: str) -> list[ReportRow]:
    segments = _segments(text)
    if not any(s[0] == "ST" and len(s) > 1 and s[1] == "277" for s in segments):
        raise ReportError("not a 277 claim acknowledgment (no ST*277)")
    component = _component_separator(text)
    rows: list[ReportRow] = []
    level: Optional[str] = None
    key: Optional[str] = None
    for seg in segments:
        tag = seg[0]
        if tag == "HL":
            # HL*id*parent*code: code PT (patient) and the claim under it.
            level = seg[3].strip() if len(seg) > 3 else None
            key = None
        elif tag == "TRN" and len(seg) > 2 and level in ("PT", "19"):
            # The claim-level trace (TRN*2*<patient control number>).
            key = seg[2].strip() or None
        elif tag == "STC" and key and len(seg) > 1:
            code = seg[1].split(component)[0].strip()
            rows.append(
                ReportRow(
                    key=key,
                    status=_classify(code, _277CA_ACCEPT, _277CA_REJECT),
                    code=code,
                )
            )
    return rows


def parse_csv(text: str, fmt: ReportFormat) -> list[ReportRow]:
    assert fmt.key_column and fmt.status_column
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fields = reader.fieldnames or []
    for column in (fmt.key_column, fmt.status_column):
        if column not in fields:
            raise ReportError(f"the report has no column {column!r}")
    rows = []
    for record in reader:
        key = (record.get(fmt.key_column) or "").strip()
        if not key:
            continue
        code = (record.get(fmt.status_column) or "").strip()
        rows.append(
            ReportRow(
                key=key, status=_classify(code, fmt.accept, fmt.reject), code=code
            )
        )
    return rows


def parse_text(text: str, fmt: ReportFormat) -> list[ReportRow]:
    assert fmt.pattern
    rows = []
    for match in re.finditer(fmt.pattern, text, flags=re.MULTILINE):
        key = (match.group("key") or "").strip()
        if not key:
            continue
        code = (match.group("status") or "").strip()
        rows.append(
            ReportRow(
                key=key, status=_classify(code, fmt.accept, fmt.reject), code=code
            )
        )
    return rows


def parse_report(text: str, fmt: ReportFormat) -> dict[str, ReportRow]:
    """Rows by key. Rows for one key that disagree become ``conflicting``."""

    if fmt.format == "x12-999":
        rows = parse_999(text)
    elif fmt.format == "x12-277ca":
        rows = parse_277ca(text)
    elif fmt.format == "csv":
        rows = parse_csv(text, fmt)
    else:
        rows = parse_text(text, fmt)
    by_key: dict[str, ReportRow] = {}
    for row in rows:
        seen = by_key.get(row.key)
        if seen is None or seen.status == row.status:
            by_key[row.key] = row
        elif seen.status == "pending":
            by_key[row.key] = row
        elif row.status != "pending":
            by_key[row.key] = ReportRow(
                key=row.key, status="conflicting", code=f"{seen.code}|{row.code}"
            )
    return by_key


__all__ = [
    "FORMATS",
    "ReportError",
    "ReportFormat",
    "ReportRow",
    "parse_277ca",
    "parse_999",
    "parse_csv",
    "parse_report",
    "parse_text",
]
