"""Checks an exchange beyond its JSON Schema, and runs the fixture harness.

The JSON Schema says what a well-formed exchange looks like. These checks say
what a *safe* one looks like: references resolve, a ``verified`` grade has an
independent read behind it, decisions come from authorized people and never
from a model, nobody approves their own work, and domain data stays in
namespaced extensions. Every violation has a stable code so a producer on the
other side of the boundary can test against the same list.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from pydantic import ValidationError

from openadapt_flow.interchange.model import (
    EXTENSION_KEY,
    INDEPENDENT_CHANNELS,
    SCREEN_CHANNELS,
    Exchange,
)

CODES: dict[str, str] = {
    "IX001": "does not match the schema",
    "IX010": "checksum does not match the content",
    "IX011": "a Seal covers a different checksum than the exchange has",
    "IX012": "a Seal is present but the exchange has no checksum",
    "IX020": "a read names an artifact that is not in the exchange",
    "IX021": "an id or field name is used twice",
    "IX022": "an identity names a field that is not in the exchange",
    "IX023": "a decision choice is not one of its options",
    "IX024": "a decision output field is not in the exchange",
    "IX025": "a value does not match its declared type",
    "IX030": "verified grade without a read through an independent channel",
    "IX031": "read-back grade without any read of the target application",
    "IX033": "committed-record grade without a read of the reopened record",
    "IX034": "a value that needs review has no reason",
    "IX035": "an export read does not say where the export came from",
    "IX032": "confirmed or corrected value without a person who confirmed it",
    "IX040": "a decision was made by something other than a person",
    "IX041": "the producer approved its own work",
    "IX042": "the decider's role is not authorized for the decision",
    "IX050": "extension key is not namespaced (use reverse-DNS, e.g. org.example.x)",
    "IX060": "an exchange sent to an executor has an ambiguous identity",
    "IX061": "an existing identity match has no record reference",
    "IX062": "a halted exchange has no halt reason",
    "IX063": "a committed exchange has an identity that was not resolved",
    "IX070": "a payload file is missing or its sha256 does not match",
    "IX071": "a payload path is absolute or leaves the exchange directory",
}


@dataclass(frozen=True)
class Violation:
    code: str
    where: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code} {self.where}: {self.detail}"


def canonical_bytes(doc: dict[str, Any]) -> bytes:
    """The bytes the checksum is computed over.

    ``provenance.checksum`` and ``seals`` are removed (a Seal signs the
    checksum, so it cannot be part of it). Keys are sorted, no whitespace,
    UTF-8.
    """
    body = json.loads(json.dumps(doc))
    body.pop("seals", None)
    prov = body.get("provenance")
    if isinstance(prov, dict):
        prov.pop("checksum", None)
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_checksum(doc: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(doc)).hexdigest()


def with_checksum(doc: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``doc`` with ``provenance.checksum`` filled in."""
    out = json.loads(json.dumps(doc))
    out["provenance"]["checksum"] = compute_checksum(out)
    return out


def _dupes(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    dup: set[str] = set()
    for v in values:
        (dup if v in seen else seen).add(v)
    return dup


def _independent(read: Any) -> bool:
    """A read outside the screens. An export counts only from committed data."""
    if read.channel == "export":
        return bool(read.source_kind == "committed_store")
    return read.channel in INDEPENDENT_CHANNELS


_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _type_ok(kind: str, value: object) -> bool:
    """Decimals travel as strings so no precision is lost; dates are ISO."""
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if not isinstance(value, str):
        return False
    if kind in ("decimal", "currency"):
        return bool(_DECIMAL.match(value))
    if kind == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return bool(_DATE.match(value))
    return True


def check(doc: dict[str, Any], *, base_dir: Optional[Path] = None) -> list[Violation]:
    """Return every violation in ``doc``; an empty list means it conforms.

    ``base_dir`` is the directory payload paths are relative to. Without it,
    payload files are not opened.
    """
    try:
        ex = Exchange.model_validate(doc)
    except ValidationError as err:
        return [
            Violation("IX001", ".".join(str(p) for p in e["loc"]) or "$", e["msg"])
            for e in err.errors()
        ]

    out: list[Violation] = []
    add = lambda code, where, detail="": out.append(  # noqa: E731
        Violation(code, where, detail or CODES[code])
    )

    # Checksum and Seals.
    checksum = ex.provenance.checksum
    if checksum is not None and checksum != compute_checksum(doc):
        add("IX010", "provenance.checksum")
    for i, seal in enumerate(ex.seals):
        if checksum is None:
            add("IX012", f"seals[{i}]")
        elif seal.covers != checksum:
            add("IX011", f"seals[{i}].covers")

    # Uniqueness and references.
    artifact_ids = {a.artifact_id for a in ex.artifacts}
    field_names = {f.name for f in ex.fields}
    for label, values in (
        ("artifacts", [a.artifact_id for a in ex.artifacts]),
        ("fields", [f.name for f in ex.fields]),
        ("identities", [i.identity_id for i in ex.identities]),
        ("decisions", [d.decision_id for d in ex.decisions]),
    ):
        for dup in sorted(_dupes(values)):
            add("IX021", label, f"{dup!r} is used twice")

    for f in ex.fields:
        where = f"fields[{f.name}]"
        for j, r in enumerate(f.reads):
            if r.artifact_id is not None and r.artifact_id not in artifact_ids:
                add(
                    "IX020",
                    f"{where}.reads[{j}]",
                    f"unknown artifact {r.artifact_id!r}",
                )
        if f.value is not None and not _type_ok(f.type, f.value):
            add("IX025", where, f"{f.value!r} is not a valid {f.type}")
        for j, r in enumerate(f.reads):
            if r.channel == "export" and r.source_kind is None:
                add("IX035", f"{where}.reads[{j}]")
        independent = {r.channel for r in f.reads if _independent(r)}
        app_reads = independent | {
            r.channel
            for r in f.reads
            if r.channel in SCREEN_CHANNELS or r.channel == "export"
        }
        if f.grade == "verified" and not independent:
            add("IX030", where)
        if (
            f.grade in ("screen_readback", "committed_record_readback")
            and not app_reads
        ):
            add("IX031", where)
        elif f.grade == "committed_record_readback" and not any(
            r.session == "reopened" for r in f.reads
        ):
            add("IX033", where)
        if f.needs_review and not (f.review_reason or "").strip():
            add("IX034", where)
        if f.status in ("confirmed", "corrected") and (
            f.confirmed_by is None or f.confirmed_by.kind != "human"
        ):
            add("IX032", where)

    for ident in ex.identities:
        where = f"identities[{ident.identity_id}]"
        for name in ident.fields:
            if name not in field_names:
                add("IX022", where, f"unknown field {name!r}")
        if ident.match.status == "existing" and not ident.match.record_ref:
            add("IX061", where)
        if ex.direction == "to_executor" and ident.match.status == "ambiguous":
            add("IX060", where)
        if ex.outcome == "committed" and ident.match.status in (
            "ambiguous",
            "not_checked",
        ):
            add("IX063", where)

    producer = ex.provenance.producer
    for d in ex.decisions:
        where = f"decisions[{d.decision_id}]"
        if d.choice is not None and d.choice not in d.options:
            add("IX023", where, f"{d.choice!r} is not an option")
        if d.output_field is not None and d.output_field not in field_names:
            add("IX024", where, f"unknown field {d.output_field!r}")
        if d.decided_by is None:
            continue
        if d.decided_by.kind != "human":
            add("IX040", where, f"decided by a {d.decided_by.kind}")
        if d.decided_by.id == producer.id:
            add("IX041", where, f"{producer.id!r} produced the exchange and decided")
        if d.decided_by.role not in d.authorized_roles:
            add("IX042", where, f"role {d.decided_by.role!r} is not authorized")

    if ex.outcome == "halted" and not (ex.halt_reason or "").strip():
        add("IX062", "halt_reason")

    for key in ex.extensions:
        if not EXTENSION_KEY.match(key):
            add("IX050", f"extensions[{key}]")

    for a in ex.artifacts:
        if a.payload is None:
            continue
        where = f"artifacts[{a.artifact_id}].payload"
        rel = PurePosixPath(a.payload.path)
        if rel.is_absolute() or ".." in rel.parts:
            add("IX071", where)
            continue
        if base_dir is not None:
            path = Path(base_dir) / rel
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != a.payload.sha256
            ):
                add("IX070", where)

    return out


def check_file(path: Path | str) -> list[Violation]:
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return [Violation("IX001", str(p), f"not readable JSON: {err}")]
    return check(doc, base_dir=p.parent)


# -- fixture harness ---------------------------------------------------------


@dataclass(frozen=True)
class CaseResult:
    file: str
    ok: bool
    expected: tuple[str, ...]
    got: tuple[str, ...]
    l1_mismatch: Optional[str] = None


def run_cases(cases_dir: Path | str) -> list[CaseResult]:
    """Run every case in ``cases_dir/cases.json``.

    Each case is ``{"file": ..., "expect": [codes]}``. An empty ``expect``
    means the exchange must conform. A conforming case may also give
    ``expect_l1_rows``: the manifest rows that emitting it through
    :func:`openadapt_flow.interchange.l1.emit_exchange` must produce (only the
    listed keys are compared).
    """
    from openadapt_flow.interchange.l1 import emit_exchange

    root = Path(cases_dir)
    cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))
    results: list[CaseResult] = []
    for case in cases["cases"]:
        path = root / case["file"]
        expected = tuple(sorted(set(case.get("expect", []))))
        got = tuple(sorted({v.code for v in check_file(path)}))
        mismatch: Optional[str] = None
        rows_expected = case.get("expect_l1_rows")
        if rows_expected is not None and not got:
            with tempfile.TemporaryDirectory() as tmp:
                refs = emit_exchange(path, Path(tmp) / "extractions")
                rows = [r.row for r in refs]
            if len(rows) != len(rows_expected):
                mismatch = f"{len(rows)} rows, expected {len(rows_expected)}"
            else:
                for want, have in zip(rows_expected, rows):
                    diff = {k: have.get(k) for k, v in want.items() if have.get(k) != v}
                    if diff:
                        mismatch = f"row differs: {diff}"
                        break
        results.append(
            CaseResult(
                case["file"],
                got == expected and mismatch is None,
                expected,
                got,
                mismatch,
            )
        )
    return results
