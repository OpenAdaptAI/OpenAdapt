"""Checks an exchange beyond its JSON Schema, and runs the fixture harness.

The JSON Schema says what a well-formed exchange looks like. These checks say
what a *safe* one looks like: references resolve, a read-back grade has the
reads behind it (``system_of_record`` an independent read, ``reopened`` an
acting read and a read of the reopened record) and those reads agree with the
value, a committed result carries evidence, decisions come from authorized
people and never from a model, nobody approves or confirms their own work,
payload files stay inside the exchange directory, and domain data stays in
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
    READ_GRADES,
    SCREEN_CHANNELS,
    Exchange,
)

CODES: dict[str, str] = {
    "IX001": "does not match the schema",
    "IX010": "checksum does not match the content",
    "IX011": "a Seal covers a different checksum than the exchange has",
    "IX020": "a read names an artifact that is not in the exchange",
    "IX021": "an id or field name is used twice",
    "IX022": "an identity names a field that is not in the exchange",
    "IX023": "a decision choice is not one of its options",
    "IX024": "a decision output field is not in the exchange",
    "IX025": "a value does not match its declared type",
    "IX026": "an identity's record reference is not the value of any of its fields",
    "IX030": "system_of_record grade without a read through an independent channel",
    "IX031": "read-back grade without two reads of the target application "
    "from different channels or sessions",
    "IX032": "confirmed or corrected value without a person who confirmed it",
    "IX033": "reopened grade without a read in the acting session and a screen "
    "read of the reopened record",
    "IX034": "a value that needs review has no reason",
    "IX035": "an export read does not say where the export came from",
    "IX036": "a read of the target application differs from the value it grades",
    "IX040": "a decision was made by something other than a person",
    "IX041": "the producer approved its own work",
    "IX042": "the decider's role is not authorized for the decision",
    "IX043": "a decision has a choice but no person who decided it",
    "IX044": "a decision choice differs from the value of its output field",
    "IX045": "the producer confirmed its own value",
    "IX050": "extension key is not namespaced (use reverse-DNS, e.g. org.example.x)",
    "IX060": "an exchange sent to an executor has an ambiguous identity",
    "IX061": "an existing identity match has no record reference",
    "IX062": "a halted exchange has no halt reason",
    "IX063": "a committed exchange has an identity that was not resolved",
    "IX064": "a committed exchange has no read of the target application",
    "IX065": "an exchange from an executor does not say which exchange it answers",
    "IX066": "an exchange sent to an executor claims a committed outcome",
    "IX070": "a payload file is missing or its sha256 does not match",
    "IX071": "a payload path is absolute or leaves the exchange directory",
}

#: Codes used by earlier drafts of v0 and no longer emitted. They are not
#: reused within v0.
RETIRED_CODES: dict[str, str] = {
    "IX012": "a Seal is present but the exchange has no checksum (the checksum "
    "is now required by the schema, so a missing one is IX001)",
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


#: Channels that read the target application (its screens, an export, a
#: database or an API). ``document_parse`` and ``human`` are the document side.
APPLICATION_CHANNELS: frozenset[str] = SCREEN_CHANNELS | INDEPENDENT_CHANNELS


def _same(a: object, b: object) -> bool:
    """Whether a read saw the field's value. Values of different JSON types
    compare by their text, so ``"5"`` read off a screen matches the integer 5."""
    if type(a) is type(b):
        return a == b
    if a is None or b is None or isinstance(a, bool) or isinstance(b, bool):
        return False
    return str(a).strip() == str(b).strip()


def supported_grade(reads: Iterable[Any]) -> str:
    """The strongest grade a set of agreeing reads supports.

    This is the reading step's rule (``openadapt_flow.records.schema.
    grade_for``) applied to interchange reads. Only application reads with a
    value count, and they must come from at least two different (channel,
    session) pairs. ``system_of_record`` needs an independent read
    (``database``, ``api``, or an ``export`` of committed data);
    ``reopened`` needs a read in the ``acting`` session and a screen read in
    the ``reopened`` session; ``same_screen`` needs two different channels.
    Agreement with the field value is checked separately (IX036).
    """
    parsed = [
        r for r in reads if r.channel in APPLICATION_CHANNELS and r.value is not None
    ]
    if len({(r.channel, r.session) for r in parsed}) < 2:
        return "unchecked"
    if any(_independent(r) for r in parsed):
        return "system_of_record"
    acting = any(r.session == "acting" for r in parsed)
    reopened = any(
        r.session == "reopened" and r.channel in SCREEN_CHANNELS for r in parsed
    )
    if acting and reopened:
        return "reopened"
    if len({r.channel for r in parsed}) >= 2:
        return "same_screen"
    return "unchecked"


def _rank(grade: str) -> int:
    return READ_GRADES.index(grade)


_DRIVE = re.compile(r"^[A-Za-z]:")


def _payload_path_ok(raw: str) -> bool:
    """A payload path is relative, POSIX, and stays inside its directory."""
    if "\\" in raw or _DRIVE.match(raw):
        return False
    rel = PurePosixPath(raw)
    return not rel.is_absolute() and ".." not in rel.parts


def resolve_payload(base_dir: Path | str, raw: str) -> Optional[Path]:
    """The payload file ``raw`` under ``base_dir``, with symlinks followed, or
    None when the path is not allowed or resolves outside ``base_dir``."""
    if not _payload_path_ok(raw):
        return None
    base = Path(base_dir).resolve()
    path = (base / raw).resolve()
    if path != base and base not in path.parents:
        return None
    return path


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
    if checksum != compute_checksum(doc):
        add("IX010", "provenance.checksum")
    for i, seal in enumerate(ex.seals):
        if seal.covers != checksum:
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

    producer = ex.provenance.producer
    values = {f.name: f.value for f in ex.fields}
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
        app_reads = [
            (j, r)
            for j, r in enumerate(f.reads)
            if r.channel in APPLICATION_CHANNELS and r.value is not None
        ]
        differs = [j for j, r in app_reads if not _same(r.value, f.value)]
        if f.grade != "unchecked" and differs:
            for j in differs:
                add(
                    "IX036",
                    f"{where}.reads[{j}]",
                    f"read {f.reads[j].value!r} but the {f.grade} value is {f.value!r}",
                )
        elif _rank(f.grade) > _rank(supported_grade(f.reads)):
            if f.grade == "system_of_record":
                code = (
                    "IX031" if any(_independent(r) for _, r in app_reads) else "IX030"
                )
            elif f.grade == "reopened":
                code = "IX033" if app_reads else "IX031"
            else:
                code = "IX031"
            add(code, where)
        if f.needs_review and not (f.review_reason or "").strip():
            add("IX034", where)
        if f.status in ("confirmed", "corrected") and (
            f.confirmed_by is None or f.confirmed_by.kind != "human"
        ):
            add("IX032", where)
        if f.confirmed_by is not None and f.confirmed_by.id == producer.id:
            add(
                "IX045",
                where,
                f"{producer.id!r} produced the exchange and confirmed the value",
            )

    for ident in ex.identities:
        where = f"identities[{ident.identity_id}]"
        for name in ident.fields:
            if name not in field_names:
                add("IX022", where, f"unknown field {name!r}")
        if ident.match.status == "existing" and not ident.match.record_ref:
            add("IX061", where)
        ref = ident.match.record_ref
        known = [name for name in ident.fields if name in values]
        if ref and known and not any(_same(values[name], ref) for name in known):
            add(
                "IX026", where, f"record_ref {ref!r} is not the value of {ident.fields}"
            )
        if ex.direction == "to_executor" and ident.match.status == "ambiguous":
            add("IX060", where)
        if ex.outcome == "committed" and ident.match.status in (
            "ambiguous",
            "not_checked",
        ):
            add("IX063", where)

    for d in ex.decisions:
        where = f"decisions[{d.decision_id}]"
        if d.choice is not None and d.choice not in d.options:
            add("IX023", where, f"{d.choice!r} is not an option")
        if d.output_field is not None and d.output_field not in field_names:
            add("IX024", where, f"unknown field {d.output_field!r}")
        if (
            d.choice is not None
            and d.output_field in values
            and not _same(values[d.output_field], d.choice)
        ):
            add(
                "IX044",
                where,
                f"chose {d.choice!r} but {d.output_field} is "
                f"{values[d.output_field]!r}",
            )
        if d.decided_by is None:
            if d.choice is not None:
                add("IX043", where)
            continue
        if d.decided_by.kind != "human":
            add("IX040", where, f"decided by a {d.decided_by.kind}")
        if d.decided_by.id == producer.id:
            add("IX041", where, f"{producer.id!r} produced the exchange and decided")
        if d.decided_by.role not in d.authorized_roles:
            add("IX042", where, f"role {d.decided_by.role!r} is not authorized")

    if ex.outcome == "halted" and not (ex.halt_reason or "").strip():
        add("IX062", "halt_reason")
    if ex.outcome == "committed" and all(
        f.grade == "unchecked"
        and not any(r.channel in APPLICATION_CHANNELS for r in f.reads)
        for f in ex.fields
    ):
        add("IX064", "outcome")
    if ex.direction == "from_executor" and not (ex.in_reply_to or "").strip():
        add("IX065", "in_reply_to")
    if ex.direction == "to_executor" and ex.outcome == "committed":
        add("IX066", "outcome")

    for key in ex.extensions:
        if not EXTENSION_KEY.match(key):
            add("IX050", f"extensions[{key}]")

    for a in ex.artifacts:
        if a.payload is None:
            continue
        where = f"artifacts[{a.artifact_id}].payload"
        if not _payload_path_ok(a.payload.path):
            add("IX071", where)
            continue
        if base_dir is not None:
            path = resolve_payload(base_dir, a.payload.path)
            if path is None:
                add("IX071", where, "the path resolves outside the exchange directory")
                continue
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
