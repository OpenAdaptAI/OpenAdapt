"""Where each written value came from, and whether the record matches it.

A record check confirms that the store holds the values it was given. If the
referral said ``02139`` and the extraction read ``02193``, the save of
``02193`` passes every check: the error happened before the save. This module
closes that gap in three steps (``docs/VERIFICATION_COVERAGE.md`` section 3):

1. **Provenance.** Every parameter can name its source: a line of a document
   (by the document's digest), a worklist cell, or the authorized instruction
   itself. Intake already finds the line for every field; :func:`from_intake`
   keeps it.
2. **A second extraction must agree before the save.** :func:`cross_check`
   reads the same source with a different method and compares value by
   value. A disagreement, a value the second method can't find, or two
   extractions by the same method hold the item for a person.
3. **A round trip after the save.** :func:`round_trip` compares the record as
   read back with the source document, not with the parameters, so a
   correctly persisted wrong entry is refuted.

Values compare by kind (text, code, date, currency) with the same rules the
screen read-back uses (:mod:`openadapt_flow.runtime.effects.readback_values`),
read exactly (no OCR leniency). Nothing here calls a model: a model extractor
is something a caller passes in.

What this can't catch: a source document that is itself wrong, and a value
both methods misread the same way. The receipt names both methods so a
reviewer can judge how independent they were.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROVENANCE_SCHEMA: Literal["openadapt.param-provenance/v1"] = (
    "openadapt.param-provenance/v1"
)

SourceKind = Literal["document", "worklist_cell", "instruction"]
SourceStatus = Literal["held", "refuted", "not_checked", "instruction"]

#: Extractor methods. Two extractions are independent only when their methods
#: differ.
METHOD_OF: dict[str, str] = {
    "intake:local": "label_parse",
    "intake:agent": "model_line",
    "ocr": "ocr",
    "worklist": "cell_read",
}


def method_of(extractor: str) -> str:
    """The method family of an extractor name (``model:<id>`` is ``model``)."""

    if extractor in METHOD_OF:
        return METHOD_OF[extractor]
    return extractor.split(":", 1)[0]


class SourceRef(BaseModel):
    """Where one value came from."""

    model_config = ConfigDict(extra="forbid")

    kind: SourceKind
    #: ``document`` / ``worklist_cell``: ``sha256:`` of the source file bytes.
    sha256: Optional[str] = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    #: ``document``: the 1-based line that carries the value.
    line: Optional[int] = Field(default=None, ge=1)
    #: ``document``: the label before the value on that line.
    label: Optional[str] = None
    #: ``worklist_cell``: 1-based row and the column name.
    row: Optional[int] = Field(default=None, ge=1)
    column: Optional[str] = None

    @model_validator(mode="after")
    def _shape(self) -> "SourceRef":
        if self.kind == "document" and (self.sha256 is None or self.line is None):
            raise ValueError("a document source needs sha256 and line")
        if self.kind == "worklist_cell" and (
            self.sha256 is None or self.row is None or not self.column
        ):
            raise ValueError("a worklist cell needs sha256, row and column")
        return self


class ParamProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    source: SourceRef
    #: Who read the value: ``intake:local``, ``intake:agent``, ``model:<id>``,
    #: ``ocr``, ``worklist``, ``person``.
    extractor: str = Field(min_length=1)
    #: ``text`` | ``code`` | ``date`` | ``currency``. Unset: inferred.
    value_kind: Optional[Literal["text", "code", "date", "currency"]] = None
    #: The text as the source wrote it, when intake converted it (``value`` is
    #: the converted form, for example a date as ISO).
    raw_value: Optional[str] = None
    #: How ``raw_value`` became ``value``, for example
    #: ``MM/DD/YYYY to ISO (25 can only be the day)``.
    conversion: Optional[str] = None


class ProvenanceFile(BaseModel):
    """``params.provenance.json``: one entry per parameter that has a source."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.param-provenance/v1"] = PROVENANCE_SCHEMA
    params: dict[str, ParamProvenance]

    def documents(self) -> set[str]:
        return {
            p.source.sha256
            for p in self.params.values()
            if p.source.kind != "instruction" and p.source.sha256
        }

    def has_documents(self) -> bool:
        return any(p.source.kind != "instruction" for p in self.params.values())

    def matches(self, params: Mapping[str, object]) -> list[str]:
        """Names whose provenance value differs from the run's value."""

        return sorted(
            name
            for name, p in self.params.items()
            if name in params and str(params[name]) != p.value
        )


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def document_sha256(path: Path | str) -> str:
    return sha256_bytes(Path(path).read_bytes())


def from_intake(
    result: Any, document_sha: str, *, extractor: Optional[str] = None
) -> ProvenanceFile:
    """Provenance for an :class:`~openadapt_flow.intake.extract.IntakeResult`.

    A value a person confirmed or typed keeps its line when it has one; its
    extractor is ``person``. A value with no line is not given a source.
    """

    name_of = extractor or f"intake:{result.mode}"
    params: dict[str, ParamProvenance] = {}
    for name, fld in result.fields.items():
        if fld.value is None or fld.source_line is None:
            continue
        params[name] = ParamProvenance(
            value=str(fld.value),
            source=SourceRef(
                kind="document",
                sha256=document_sha,
                line=fld.source_line,
                label=fld.label,
            ),
            extractor="person" if fld.confirmed and fld.confidence >= 1.0 else name_of,
            raw_value=getattr(fld, "raw", None),
            conversion=getattr(fld, "conversion", None),
        )
    return ProvenanceFile(params=params)


# -- second extraction -----------------------------------------------------------


class Extractor(Protocol):
    """A second way to read a value from a source document."""

    #: Recorded in the receipt; :func:`method_of` decides independence.
    name: str

    def read(self, document: Any, name: str, ref: SourceRef) -> Optional[str]:
        """The value of parameter ``name`` per this method, or None."""


@dataclass
class LabelExtractor:
    """Deterministic: find the parameter's label anywhere in the document.

    The intake label parser, run over the whole document (not only the cited
    line), so a citation to the wrong line is caught too. ``specs`` gives each
    parameter's type for normalization; ``aliases`` extra labels.
    """

    specs: Mapping[str, Any]
    aliases: Mapping[str, list[str]] = field(default_factory=dict)
    name: str = "intake:local"
    #: Declared order of numeric dates, per parameter (as in intake).
    date_formats: Mapping[str, str] = field(default_factory=dict)

    def read(self, document: Any, name: str, ref: SourceRef) -> Optional[str]:
        from openadapt_flow.intake.extract import extract_fields

        spec = self.specs.get(name)
        if spec is None:
            return None
        aliases = dict(self.aliases)
        if ref.label:
            aliases[name] = [*aliases.get(name, []), ref.label]
        found = extract_fields(
            document,
            {name: spec},
            aliases=aliases,
            date_formats={k: v for k, v in self.date_formats.items() if k == name},
        ).fields[name]
        if found.error or found.value is None:
            return None
        return str(found.value)


@dataclass
class CallableExtractor:
    """Wrap a caller's function (a second model, OCR) as an extractor."""

    name: str
    fn: Callable[[Any, str, SourceRef], Optional[str]]

    def read(self, document: Any, name: str, ref: SourceRef) -> Optional[str]:
        return self.fn(document, name, ref)


def _kind(prov: ParamProvenance, value: str) -> str:
    from openadapt_flow.runtime.effects.readback_values import infer_value_kind

    return prov.value_kind or infer_value_kind(value)


def same_value(a: str, b: str, kind: str) -> bool:
    from openadapt_flow.runtime.effects.readback_values import compare_value

    return compare_value(a, b, kind=kind, source="structure") == "match"  # type: ignore[arg-type]


@dataclass
class FieldCheck:
    name: str
    #: ``agree`` | ``disagree`` | ``missing`` | ``instruction`` |
    #: ``not_independent`` | ``no_document``
    result: str
    #: In memory only: never put in a receipt (values are data).
    first: Optional[str] = None
    second: Optional[str] = None


@dataclass
class CrossCheck:
    """The pre-save comparison of two extractions."""

    status: Literal["agree", "hold", "instruction"]
    second_extractor: str
    fields: list[FieldCheck] = field(default_factory=list)

    @property
    def disagreements(self) -> list[str]:
        return [f.name for f in self.fields if f.result not in ("agree", "instruction")]

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for f in self.fields:
            counts[f.result] = counts.get(f.result, 0) + 1
        return {
            "status": self.status,
            "second_extractor": self.second_extractor,
            "fields": dict(sorted(counts.items())),
            "held_fields": self.disagreements,
        }


def cross_check(
    provenance: ProvenanceFile,
    documents: Mapping[str, Any],
    second: Extractor,
) -> CrossCheck:
    """Compare every sourced value with a second, independent extraction.

    ``documents`` maps a document's ``sha256:`` to its text
    (:class:`~openadapt_flow.intake.document.IntakeDocument`). A source whose
    document isn't supplied can't be checked, so it holds.
    """

    checks: list[FieldCheck] = []
    for name, prov in sorted(provenance.params.items()):
        if prov.source.kind == "instruction":
            checks.append(FieldCheck(name, "instruction"))
            continue
        if method_of(prov.extractor) == method_of(second.name):
            checks.append(FieldCheck(name, "not_independent", prov.value))
            continue
        document = documents.get(prov.source.sha256 or "")
        if document is None:
            checks.append(FieldCheck(name, "no_document", prov.value))
            continue
        other = second.read(document, name, prov.source)
        if other is None:
            checks.append(FieldCheck(name, "missing", prov.value))
        elif same_value(prov.value, other, _kind(prov, prov.value)):
            checks.append(FieldCheck(name, "agree", prov.value, other))
        else:
            checks.append(FieldCheck(name, "disagree", prov.value, other))
    sourced = [c for c in checks if c.result != "instruction"]
    if not sourced:
        status: Literal["agree", "hold", "instruction"] = "instruction"
    elif all(c.result == "agree" for c in sourced):
        status = "agree"
    else:
        status = "hold"
    return CrossCheck(status, second.name, checks)


# -- round trip ------------------------------------------------------------------


@dataclass
class RoundTrip:
    """The read-back record against the source document."""

    status: SourceStatus
    second_extractor: str
    reason: str
    fields: list[FieldCheck] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for f in self.fields:
            counts[f.result] = counts.get(f.result, 0) + 1
        return {
            "status": self.status,
            "second_extractor": self.second_extractor,
            "fields": dict(sorted(counts.items())),
            "mismatched_fields": [
                f.name for f in self.fields if f.result == "disagree"
            ],
        }


def bindings(effects: Any) -> dict[str, tuple[str, Any]]:
    """Parameter name -> (record field, value expression), each pair taken
    from one place in one effect, so a field's transform is never another
    field's. A parameter bound two different ways is left out: its round
    trip can't be compared, so it stays unchecked."""

    out: dict[str, tuple[str, Any]] = {}
    ambiguous: set[str] = set()

    def add(param: str, record_field: str, expr: Any) -> None:
        seen = out.get(param)
        transform = (expr.map, expr.stored)
        if seen is None:
            out[param] = (record_field, expr)
        elif seen[0] != record_field or (seen[1].map, seen[1].stored) != transform:
            ambiguous.add(param)

    for effect in effects:
        for record_field, expr in effect.match.items():
            if expr.param is not None:
                add(expr.param, record_field, expr)
        if effect.field and effect.value is not None and effect.value.param:
            add(effect.value.param, effect.field, effect.value)
    return {k: v for k, v in out.items() if k not in ambiguous}


def bound_fields(effects: Any) -> dict[str, str]:
    """Parameter name -> record field (:func:`bindings`)."""

    return {param: field for param, (field, _expr) in bindings(effects).items()}


def bound_maps(effects: Any) -> dict[str, Any]:
    """Parameter name -> the value expression that says how its field stores
    what was entered: a label's code (``map``), a stored form (``stored``).
    From the same effect as the field (:func:`bindings`)."""

    return {
        param: expr
        for param, (_field, expr) in bindings(effects).items()
        if expr.map or expr.stored
    }


def round_trip(
    provenance: ProvenanceFile,
    record: Optional[Mapping[str, Any]],
    fields: Mapping[str, str],
    documents: Mapping[str, Any],
    second: Extractor,
    maps: Optional[Mapping[str, Any]] = None,
) -> RoundTrip:
    """Compare ``record`` (as read back) with the source of each value.

    ``fields`` maps a parameter to the record field it was written to
    (:func:`bound_fields`). For each sourced parameter the second method reads
    the source; the record's field must equal that reading. The comparison
    never consults the parameters, so a value misread before the save and
    saved faithfully is refuted.
    """

    sourced = {
        name: prov
        for name, prov in sorted(provenance.params.items())
        if prov.source.kind != "instruction"
    }
    if not sourced:
        return RoundTrip("instruction", second.name, "every input is the instruction")
    if record is None:
        return RoundTrip(
            "not_checked",
            second.name,
            "no single saved record was read back to compare with its source",
        )
    checks: list[FieldCheck] = []
    for name, prov in sourced.items():
        record_field = fields.get(name)
        if record_field is None or record_field not in record:
            checks.append(FieldCheck(name, "not_read_back"))
            continue
        document = documents.get(prov.source.sha256 or "")
        if document is None:
            checks.append(FieldCheck(name, "no_document"))
            continue
        source_value = second.read(document, name, prov.source)
        mapping = (maps or {}).get(name)
        if mapping is not None and source_value is not None:
            # The document shows the label, the record stores its code (a
            # state's name and "MA", a provider's name and their id), or a
            # stored form (a street in capitals).
            source_value = (
                mapping.get(source_value.strip())
                if isinstance(mapping, Mapping)
                else mapping.stored_form(source_value.strip())
            )
        stored = "" if record.get(record_field) is None else str(record[record_field])
        if source_value is None:
            checks.append(FieldCheck(name, "missing", stored))
        elif same_value(stored, source_value, _kind(prov, source_value)):
            checks.append(FieldCheck(name, "agree", stored, source_value))
        else:
            checks.append(FieldCheck(name, "disagree", stored, source_value))
    if any(c.result == "disagree" for c in checks):
        names = [c.name for c in checks if c.result == "disagree"]
        return RoundTrip(
            "refuted",
            second.name,
            "the saved record differs from its source document in " + ", ".join(names),
            checks,
        )
    if all(c.result == "agree" for c in checks):
        return RoundTrip(
            "held",
            second.name,
            f"{len(checks)} saved field(s) match their source document",
            checks,
        )
    unchecked = [c.name for c in checks if c.result != "agree"]
    return RoundTrip(
        "not_checked",
        second.name,
        "could not compare with the source: " + ", ".join(unchecked),
        checks,
    )


def default_extractor(provenance: ProvenanceFile) -> LabelExtractor:
    """The deterministic label parse, typed from the provenance itself.

    A parameter recorded as a date is read as a date; everything else as
    text. Each cited label is an alias, so the parse looks for the same label
    the first reader cited, anywhere in the document.
    """

    from openadapt_flow.ir import ParamKind, ParamSpec

    specs: dict[str, Any] = {}
    aliases: dict[str, list[str]] = {}
    for name, prov in provenance.params.items():
        kind = ParamKind.DATE if prov.value_kind == "date" else ParamKind.STRING
        specs[name] = ParamSpec(name=name, type=kind)
        if prov.source.label:
            aliases[name] = [prov.source.label]
    return LabelExtractor(specs, aliases)


def read_sources(paths: list[Path]) -> dict[str, Any]:
    """``sha256:`` of each file's bytes -> its text, read as intake reads it."""

    from openadapt_flow.intake.document import read_document

    return {document_sha256(path): read_document(path) for path in paths}


def load_provenance(path: Path | str) -> ProvenanceFile:
    import json

    return ProvenanceFile.model_validate(json.loads(Path(path).read_text()))
