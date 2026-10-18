"""Records read out of an application's screens, field by field, with optional
proof of where each value came from.

A *record schema* names the fields a workflow reads from one record (an
invoice, an account, a customer) and gives each field a kind. A *field read*
is one value read off the screen for one field: what each channel saw, whether
the channels agreed, a confidence, where on the screen it came from, the read
grade and, when sealing is on, a pointer to the Seal that covers it. A *record
read* groups the field reads of one record.

Proof is opt-in. By default a field is read through one channel, gets the
grade ``unconfirmed`` and no Seal. :class:`ReadProof` turns on a double read,
a committed-record read-back and Seals, for the whole schema or per field.

This module only defines data. It makes no screen reads, no model calls and
no network calls, so every build stream can import it.

Read grades, weakest first. A grade only ever reflects what was proven:

- ``unconfirmed``: nothing was cross-checked. This is the default for a
  single-channel read. It is also the grade when channels disagreed, which
  sends the value to the review queue.
- ``screen_readback``: two channels in the same session agreed (for example
  the clipboard text and OCR of the same region).
- ``committed_record_readback``: the record was closed, reopened by its
  identity fields and read again, and the second read agreed with the first.
- ``verified``: a channel independent of the acting session agreed: a
  read-only database, an API, or an export of committed data (a separate
  process or a scheduled export). Only an independent channel can give this
  grade; :func:`grade_for` enforces it.

An export read records its source in ``ChannelRead.source_kind``, so its
grade is derived, not asserted. An export from the same live UI session
(``live_session``) can carry that session's unsaved state; it counts like a
screen read and gives at most ``screen_readback``.

A step that writes a value into a system of record can declare a minimum
grade with :class:`GradeGate`; :meth:`GradeGate.check` raises
:class:`GradeTooLow` so the run halts instead of writing a weaker value.

The grade describes how a *read value* was checked. It is separate from
:mod:`openadapt_flow.assurance`, which grades how a *write* was checked.
"""

from __future__ import annotations

import re
from typing import Any, Final, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "INDEPENDENT_CHANNELS",
    "READ_GRADES",
    "SCHEMA_VERSION",
    "SCREEN_CHANNELS",
    "Agreement",
    "SourceKind",
    "ChannelRead",
    "FieldKind",
    "FieldRead",
    "FieldSpec",
    "GradeGate",
    "GradeTooLow",
    "ReadProof",
    "ReadChannel",
    "ReadGrade",
    "RecordRead",
    "RecordSchema",
    "SealRef",
    "SourceRegion",
    "grade_at_least",
    "grade_for",
    "weakest_grade",
]

#: Version of the wire shape of these models. Bump on a breaking change.
SCHEMA_VERSION: Final = "openadapt.records/v1"

FieldKind = Literal[
    "text", "code", "date", "currency", "integer", "decimal", "boolean", "enum"
]

ReadChannel = Literal[
    # Through the application's screens.
    "clipboard",
    "ocr",
    "structure",
    "accessibility",
    # An export from the application; see SourceKind for how it grades.
    "export",
    # Not through the screens.
    "database",
    "api",
]
SCREEN_CHANNELS: Final[frozenset[str]] = frozenset(
    {"clipboard", "ocr", "structure", "accessibility"}
)
#: Channels that are always independent of the acting session. An ``export``
#: read is independent only when its source is ``committed_store``.
INDEPENDENT_CHANNELS: Final[frozenset[str]] = frozenset({"database", "api"})

#: Where an ``export`` read came from. Required on every export read.
#:
#: - ``live_session``: exported from the same live UI session that typed or
#:   showed the value. It can reflect that session's unsaved state, so it
#:   counts as a screen-session read (at best ``screen_readback``).
#: - ``committed_store``: exported from saved data by a separate process, a
#:   database read or a scheduled export, not the session's in-memory state.
#:   It is independent and can give ``verified``.
SourceKind = Literal["live_session", "committed_store"]

ReadGrade = Literal[
    "unconfirmed", "screen_readback", "committed_record_readback", "verified"
]
#: Weakest first.
READ_GRADES: Final[tuple[ReadGrade, ...]] = (
    "unconfirmed",
    "screen_readback",
    "committed_record_readback",
    "verified",
)

Agreement = Literal["agree", "disagree", "ambiguous", "single"]

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReadProof(BaseModel):
    """Which proof to collect for a read. Everything is off by default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    double_read: bool = Field(
        default=False, description="Read through two channels and compare."
    )
    committed_readback: bool = Field(
        default=False,
        description="Close, reopen by identity fields and read again.",
    )
    seal: bool = Field(default=False, description="Cover the value with a signed Seal.")


class FieldSpec(BaseModel):
    """One field of a record: its name, kind and how strictly it is read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Lower snake case, unique in the schema.")
    kind: FieldKind = "text"
    description: str = ""
    required: bool = True
    choices: tuple[str, ...] = Field(
        default=(), description="Allowed values; required when kind is enum."
    )
    date_order: Optional[Literal["dmy", "mdy"]] = Field(
        default=None, description="How to read 03/04/2026; unset means ambiguous."
    )
    pattern: Optional[str] = Field(
        default=None, description="Regular expression the normalized value matches."
    )
    min_confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Below this the value goes to the review queue; unset means no confidence gate.",
    )
    proof: Optional[ReadProof] = Field(
        default=None, description="Overrides the schema's proof for this field."
    )

    @field_validator("name")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError(f"field name {value!r} must be lower snake case")
        return value

    @model_validator(mode="after")
    def _enum_has_choices(self) -> "FieldSpec":
        if self.kind == "enum" and not self.choices:
            raise ValueError(f"enum field {self.name!r} needs choices")
        if self.pattern is not None:
            re.compile(self.pattern)
        return self


_JSON_TYPES: Final[dict[str, dict[str, Any]]] = {
    "text": {"type": "string"},
    "code": {"type": "string"},
    "date": {"type": "string", "format": "date"},
    "currency": {"type": "string", "pattern": r"^-?\d+(\.\d+)?$"},
    "integer": {"type": "integer"},
    "decimal": {"type": "string", "pattern": r"^-?\d+(\.\d+)?$"},
    "boolean": {"type": "boolean"},
    "enum": {"type": "string"},
}


class RecordSchema(BaseModel):
    """The fields read from one kind of record, and which fields identify it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: int = Field(default=1, ge=1)
    description: str = ""
    fields: tuple[FieldSpec, ...]
    identity_fields: tuple[str, ...] = Field(
        description="Fields that together identify a record; used to reopen it."
    )
    proof: ReadProof = Field(
        default_factory=ReadProof,
        description="Proof collected for every field unless a field overrides it.",
    )

    @field_validator("name")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError(f"schema name {value!r} must be lower snake case")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "RecordSchema":
        names = [spec.name for spec in self.fields]
        if not names:
            raise ValueError("a record schema needs at least one field")
        if len(set(names)) != len(names):
            raise ValueError("field names must be unique")
        if not self.identity_fields:
            raise ValueError("a record schema needs at least one identity field")
        for name in self.identity_fields:
            if name not in names:
                raise ValueError(f"identity field {name!r} is not a field")
            if not self.field(name).required:
                raise ValueError(f"identity field {name!r} must be required")
        return self

    def field(self, name: str) -> FieldSpec:
        """The field named ``name``; ``KeyError`` if there is none."""

        for spec in self.fields:
            if spec.name == name:
                return spec
        raise KeyError(name)

    def proof_for(self, name: str) -> ReadProof:
        """The proof settings for field ``name``: its own, else the schema's."""

        return self.field(name).proof or self.proof

    def to_json_schema(self) -> dict[str, Any]:
        """A JSON Schema (draft 2020-12) for one exported row of this record."""

        properties: dict[str, Any] = {}
        for spec in self.fields:
            prop = dict(_JSON_TYPES[spec.kind])
            if spec.kind == "enum":
                prop["enum"] = list(spec.choices)
            if spec.pattern is not None and prop.get("type") == "string":
                prop["pattern"] = spec.pattern
            if spec.description:
                prop["description"] = spec.description
            if not spec.required:
                prop = {"anyOf": [prop, {"type": "null"}]}
            properties[spec.name] = prop
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"urn:openadapt:record:{self.name}:v{self.version}",
            "title": self.name,
            "type": "object",
            "properties": properties,
            "required": [spec.name for spec in self.fields if spec.required],
            "additionalProperties": False,
        }


class SourceRegion(BaseModel):
    """Where on which screen a value was read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frame_sha256: str = Field(description="sha256 of the full screenshot PNG.")
    bbox: tuple[int, int, int, int] = Field(description="x, y, width, height in px.")
    crop_sha256: Optional[str] = Field(
        default=None, description="sha256 of the cropped region PNG."
    )
    frame_size: Optional[tuple[int, int]] = None
    step_id: Optional[str] = None

    @field_validator("frame_sha256", "crop_sha256")
    @classmethod
    def _sha(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _SHA256.match(value):
            raise ValueError("expected a lowercase hex sha256")
        return value

    @field_validator("bbox")
    @classmethod
    def _bbox(cls, value: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        if value[0] < 0 or value[1] < 0 or value[2] <= 0 or value[3] <= 0:
            raise ValueError("bbox needs x, y >= 0 and width, height > 0")
        return value


class SealRef(BaseModel):
    """A pointer to the signed run receipt (Seal) that covers a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    receipt_sha256: Optional[str] = Field(
        default=None, description="sha256 of the receipt JSON; set once sealed."
    )
    receipt_path: Optional[str] = None
    bundle_digest: Optional[str] = None
    field_commitment: Optional[str] = Field(
        default=None, description="Salted per-field commitment, when sealed."
    )

    @property
    def sealed(self) -> bool:
        return self.receipt_sha256 is not None


class ChannelRead(BaseModel):
    """What one channel read for one field."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: ReadChannel
    raw: Optional[str] = Field(description="Exactly what the channel returned.")
    normalized: Optional[str] = Field(
        default=None,
        description="Canonical form for the field kind; None if it did not parse.",
    )
    engine_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    session: Literal["acting", "reopened", "independent"] = "acting"
    source_kind: Optional[SourceKind] = Field(
        default=None,
        description="Required for an export read; must be unset otherwise.",
    )

    @model_validator(mode="after")
    def _session_matches_channel(self) -> "ChannelRead":
        if self.channel == "export":
            if self.source_kind is None:
                raise ValueError(
                    "an export read needs source_kind: live_session or committed_store"
                )
            if self.source_kind == "live_session" and self.session != "acting":
                raise ValueError(
                    "a live_session export is read in the acting session ('acting')"
                )
        elif self.source_kind is not None:
            raise ValueError("source_kind applies to export reads only")
        if self.independent != (self.session == "independent"):
            raise ValueError(
                "database, api and committed_store export reads have session "
                "'independent'; screen reads and live_session exports do not"
            )
        return self

    @property
    def independent(self) -> bool:
        """True when this read does not depend on the acting session's state."""

        if self.channel == "export":
            return self.source_kind == "committed_store"
        return self.channel in INDEPENDENT_CHANNELS


class FieldRead(BaseModel):
    """One value read for one field, with its evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    value: Optional[str] = Field(
        description="The normalized value, or None when no channel parsed it."
    )
    reads: tuple[ChannelRead, ...]
    agreement: Agreement
    confidence: float = Field(ge=0.0, le=1.0)
    grade: ReadGrade
    region: Optional[SourceRegion] = None
    seal: Optional[SealRef] = None
    needs_review: bool = False
    review_reason: Optional[str] = None
    review_decision: Optional[str] = Field(
        default=None,
        description="Id of the signed review decision that settled this value.",
    )

    @model_validator(mode="after")
    def _grade_matches_reads(self) -> "FieldRead":
        if not self.reads:
            raise ValueError("a field read needs at least one channel read")
        allowed = grade_for(self.reads, self.agreement)
        if READ_GRADES.index(self.grade) > READ_GRADES.index(allowed):
            raise ValueError(
                f"grade {self.grade!r} is stronger than the reads support ({allowed!r})"
            )
        if self.needs_review and not self.review_reason:
            raise ValueError("needs_review requires a review_reason")
        return self


class RecordRead(BaseModel):
    """The field reads of one record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["openadapt.records/v1"] = SCHEMA_VERSION
    schema_name: str
    schema_version: int
    record_key: str = Field(
        description="Identity field values joined; stable per record."
    )
    run_id: str
    read_at: str = Field(description="ISO 8601 with offset.")
    fields: tuple[FieldRead, ...]

    def value(self, name: str) -> Optional[str]:
        for read in self.fields:
            if read.field == name:
                return read.value
        raise KeyError(name)

    @property
    def grade(self) -> ReadGrade:
        return weakest_grade([read.grade for read in self.fields])

    @property
    def needs_review(self) -> bool:
        return any(read.needs_review for read in self.fields)

    def check_against(self, schema: RecordSchema) -> None:
        """Raise ``ValueError`` if this read does not fit ``schema``."""

        if (self.schema_name, self.schema_version) != (schema.name, schema.version):
            raise ValueError("record read is for another schema or version")
        seen = [read.field for read in self.fields]
        if len(set(seen)) != len(seen):
            raise ValueError("a field is read twice")
        names = {spec.name for spec in schema.fields}
        unknown = set(seen) - names
        if unknown:
            raise ValueError(f"unknown fields: {sorted(unknown)}")
        missing = [s.name for s in schema.fields if s.required and s.name not in seen]
        if missing:
            raise ValueError(f"required fields not read: {missing}")


def grade_for(
    reads: tuple[ChannelRead, ...] | list[ChannelRead], agreement: Agreement
) -> ReadGrade:
    """The strongest grade a set of channel reads can support.

    ``verified`` needs an agreeing independent channel (database, API, or an
    export of committed data). A live-session export counts as a screen read. ``committed_record_readback`` needs an agreeing read from a reopened
    record. ``screen_readback`` needs two agreeing screen channels. Anything
    else, including a single read or any disagreement, is ``unconfirmed``.
    """

    if agreement != "agree" or len(reads) < 2:
        return "unconfirmed"
    if any(read.independent for read in reads):
        return "verified"
    if any(read.session == "reopened" for read in reads):
        return "committed_record_readback"
    return "screen_readback"


def grade_at_least(grade: ReadGrade, floor: ReadGrade) -> bool:
    """True when ``grade`` is ``floor`` or stronger."""

    return READ_GRADES.index(grade) >= READ_GRADES.index(floor)


def weakest_grade(grades: list[ReadGrade]) -> ReadGrade:
    """The weakest of ``grades``; ``unconfirmed`` when empty."""

    if not grades:
        return "unconfirmed"
    return min(grades, key=READ_GRADES.index)


class GradeTooLow(Exception):
    """A value is below the grade a write step requires. The run halts."""

    def __init__(self, record_key: str, below: dict[str, ReadGrade], floor: ReadGrade):
        self.record_key = record_key
        self.below = below
        self.floor = floor
        listed = ", ".join(f"{name} ({grade})" for name, grade in below.items())
        super().__init__(f"record {record_key}: below {floor}: {listed}")


class GradeGate(BaseModel):
    """The minimum read grade a write step accepts for the values it writes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_grade: ReadGrade
    fields: tuple[str, ...] = Field(
        default=(), description="Fields the step writes; empty means every field."
    )

    def check(self, record: RecordRead) -> None:
        """Raise :class:`GradeTooLow` if a gated field is below ``min_grade``,
        is missing, or still needs review."""

        reads = {read.field: read for read in record.fields}
        names = self.fields or tuple(reads)
        below: dict[str, ReadGrade] = {}
        for name in names:
            read = reads.get(name)
            if read is None or read.needs_review:
                below[name] = "unconfirmed"
            elif not grade_at_least(read.grade, self.min_grade):
                below[name] = read.grade
        if below:
            raise GradeTooLow(record.record_key, below, self.min_grade)
