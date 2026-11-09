"""Records interchange format, version 0: the wire models.

One :class:`Exchange` carries one unit of work between a document-side system
(a parser, a review tool, a records platform) and an executor that reads and
writes records in an application with no API. It holds:

- the source artifacts, in the shape of the ``L1Artifact`` envelope that
  layered records platforms already use (``source_document`` and
  ``structured_observation``), so an exchange maps onto
  :func:`openadapt_flow.emit.l1_artifact.emit_l1_artifact`;
- typed field values, each with its origin, confidence and read grade;
- identities and how each one matched a record in the target application;
- human decisions, with any model suggestion kept separate and advisory;
- provenance (who produced the exchange, a checksum) and Seal references.

Nothing here names a domain. Domain fields go in ``extensions`` under a
namespaced key such as ``org.example.referral``.

The field record (:class:`FieldValue`) and the read grades are provisional:
the legacy-records reading step owns the canonical record and field schema,
and this module will import it once that lands. The wire names are chosen to
match it so the swap does not change documents.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Literal["openadapt.records-interchange/v0"] = (
    "openadapt.records-interchange/v0"
)

# The grade, channel and kind vocabularies below match
# ``openadapt_flow.records.schema`` (the record and field schema of the
# reading step) value for value. They are spelled out here until that module
# is on main; then they are imported from it.

#: Read grades, weakest first. They are names from the check-level ladder
#: (``openadapt_flow.check_level``), so a read grade and the check level of a
#: write mean the same thing. ``system_of_record`` needs a read through a
#: channel independent of the application's screens (see
#: INDEPENDENT_CHANNELS); ``reopened`` needs a read in the acting session and
#: a screen read of the reopened record; ``same_screen`` needs two channels.
ReadGrade = Literal["unchecked", "same_screen", "reopened", "system_of_record"]
READ_GRADES: tuple[str, ...] = (
    "unchecked",
    "same_screen",
    "reopened",
    "system_of_record",
)

#: How a value was obtained. The first group reads the target application
#: through its screens, the second does not, the third is the document side.
Channel = Literal[
    "clipboard",
    "ocr",
    "structure",
    "accessibility",
    "export",
    "database",
    "api",
    "document_parse",
    "human",
]
SCREEN_CHANNELS: frozenset[str] = frozenset(
    {"clipboard", "ocr", "structure", "accessibility"}
)
#: Channels that do not go through the application's screens. A
#: ``system_of_record`` grade requires at least one read through one of them. An ``export`` read
#: counts only when its ``source_kind`` is ``committed_store``.
INDEPENDENT_CHANNELS: frozenset[str] = frozenset({"export", "database", "api"})

#: Where an export came from. ``live_session``: the export was triggered from
#: the same signed-in UI session that did the work, so it can share that
#: session's view (graded like a screen read). ``committed_store``: the export
#: was produced from saved data by a separate process, a database read or a
#: scheduled job (an independent read).
ExportSource = Literal["live_session", "committed_store"]

ValueType = Literal[
    "text", "code", "date", "currency", "integer", "decimal", "boolean", "enum"
]

_NAME = r"^[a-z][a-z0-9_]{0,63}$"
_SHA256 = r"^[0-9a-f]{64}$"
#: Extension keys are reverse-DNS style: at least two dot-separated labels.
EXTENSION_KEY = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z0-9][a-z0-9_-]*)+$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Region(_Strict):
    """Where on which screen a value was read (the reading step's
    ``SourceRegion``). ``bbox`` is x, y, width, height in frame pixels."""

    frame_sha256: str = Field(pattern=_SHA256)
    bbox: tuple[int, int, int, int]
    crop_sha256: Optional[str] = Field(default=None, pattern=_SHA256)
    frame_size: Optional[tuple[int, int]] = None
    step_id: Optional[str] = Field(default=None, max_length=200)


class Locator(_Strict):
    """Where in a source a value came from. Every member is optional; use the
    ones that apply to the channel."""

    page: Optional[int] = Field(default=None, ge=1)
    line: Optional[int] = Field(default=None, ge=1)
    span: Optional[tuple[int, int]] = Field(
        default=None, description="UTF-8 byte offsets [start, end) in the artifact"
    )
    screen: Optional[str] = Field(default=None, max_length=200)
    region: Optional[Region] = None


class Read(_Strict):
    """One observation of a value through one channel."""

    channel: Channel
    value: Union[str, int, float, bool, None]
    artifact_id: Optional[str] = Field(
        default=None, description="Artifact the read came from, if any"
    )
    locator: Optional[Locator] = None
    session: Optional[Literal["acting", "reopened", "independent"]] = Field(
        default=None,
        description="For application reads: the acting session, the record "
        "reopened by id, or an independent channel",
    )
    source_kind: Optional[ExportSource] = Field(
        default=None,
        description="Required on export reads: live_session or committed_store",
    )
    observed_at: Optional[str] = None
    run_id: Optional[str] = None


class Principal(_Strict):
    """Who did something. ``kind`` separates people, the executor and models."""

    kind: Literal["human", "executor", "model", "system"]
    id: str = Field(min_length=1, max_length=200)
    role: Optional[str] = Field(default=None, max_length=100)


class FieldValue(_Strict):
    """A typed value, with where it came from and how well it was checked."""

    name: str = Field(pattern=_NAME)
    type: ValueType
    value: Union[str, int, float, bool, None]
    unit: Optional[str] = Field(default=None, max_length=50)
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["proposed", "confirmed", "corrected", "rejected"] = "proposed"
    confirmed_by: Optional[Principal] = None
    grade: ReadGrade = "unchecked"
    reads: list[Read] = Field(default_factory=list)
    needs_review: bool = False
    review_reason: Optional[str] = Field(default=None, max_length=2000)
    seal: Optional["FieldSeal"] = None


class FieldSeal(_Strict):
    """The reading step's per-value Seal pointer."""

    run_id: str = Field(min_length=1, max_length=200)
    receipt_sha256: Optional[str] = Field(default=None, pattern=_SHA256)
    field_commitment: Optional[str] = Field(default=None, max_length=200)


class IdentityMatch(_Strict):
    """How an identity resolved against records in the target application."""

    status: Literal["new", "existing", "ambiguous", "not_checked"]
    record_ref: Optional[str] = Field(
        default=None, description="Target-application record id when existing"
    )
    candidates: int = Field(default=0, ge=0)
    checked_by: Optional[Principal] = None


class Identity(_Strict):
    """A party or record the exchange is about (a customer, an account, a
    supplier, a case). ``fields`` name the FieldValue entries that identify it."""

    identity_id: str = Field(min_length=1, max_length=100)
    role: str = Field(pattern=_NAME)
    fields: list[str] = Field(min_length=1)
    match: IdentityMatch


class Suggestion(_Strict):
    """A non-binding suggestion (for example from a local model)."""

    source: Principal
    option: str
    rationale: Optional[str] = Field(default=None, max_length=2000)


class Decision(_Strict):
    """A finite choice made by an authorized person.

    ``decided_by`` must be a human. Model output goes in ``advisory`` only and
    never authorizes anything.
    """

    decision_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(min_length=2)
    choice: Optional[str] = None
    output_field: Optional[str] = Field(default=None, pattern=_NAME)
    authorized_roles: list[str] = Field(min_length=1)
    decided_by: Optional[Principal] = None
    decided_at: Optional[str] = None
    receipt_sha256: Optional[str] = Field(default=None, pattern=_SHA256)
    advisory: list[Suggestion] = Field(default_factory=list)


class NoAction(_Strict):
    """Why an executor wrote nothing, who decided it, and the read-back that
    shows nothing was written.

    A ``no_action`` outcome is never a save and never billable.
    """

    decided_by: Principal
    reason_code: str = Field(
        pattern=_NAME, description="Why nothing was written, e.g. declined, held"
    )
    decision_id: Optional[str] = Field(
        default=None,
        max_length=100,
        description="The decision this follows, in this exchange or the one it answers",
    )
    decided_at: Optional[str] = None
    required_grade: ReadGrade = Field(
        description="The check level a commit of this workflow needs. Every "
        "evidence field must be graded at least this, and it cannot be unchecked"
    )
    evidence: list[str] = Field(
        default_factory=list,
        description="Fields whose read-back shows no new or changed record",
    )


class PayloadRef(_Strict):
    """A file next to the exchange, addressed relative to it."""

    path: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=_SHA256)
    mime: str = Field(min_length=1, max_length=200)
    bytes: Optional[int] = Field(default=None, ge=0)


class Artifact(_Strict):
    """A source artifact, in the ``L1Artifact`` envelope shape.

    ``file_number``, ``date`` and ``doctype`` are the L1 manifest naming keys
    (``{file_number}_{date}_{doctype}``).
    """

    artifact_id: str = Field(min_length=1, max_length=100)
    kind: Literal["source_document", "structured_observation"]
    source_type: str = Field(min_length=1, max_length=100)
    file_number: str = Field(min_length=1, max_length=200)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    doctype: str = Field(min_length=1, max_length=100)
    payload: Optional[PayloadRef] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Provenance(_Strict):
    """Who produced the exchange, and a checksum over its content. The
    checksum is required, so every exchange can be checked for tampering and
    a Seal always has something to sign."""

    tool_name: str = Field(min_length=1, max_length=100)
    tool_version: str = Field(min_length=1, max_length=100)
    producer: Principal
    captured_at: str
    checksum: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="Required. sha256 of the canonical JSON without checksum and seals",
    )


class SealRef(_Strict):
    """A pointer to a Seal (a signed receipt) that covers this exchange."""

    kind: str = Field(min_length=1, max_length=100)
    covers: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$", description="The checksum the Seal signs"
    )
    key_id: Optional[str] = Field(default=None, max_length=200)
    signature: Optional[str] = Field(default=None, max_length=1000)
    uri: Optional[str] = Field(default=None, max_length=1000)


class Exchange(_Strict):
    """One unit of work between a document side and an executor."""

    schema_version: Literal["openadapt.records-interchange/v0"]
    exchange_id: str = Field(min_length=1, max_length=100)
    direction: Literal["to_executor", "from_executor"]
    workflow: str = Field(min_length=1, max_length=200)
    session_id: Optional[str] = Field(default=None, max_length=100)
    in_reply_to: Optional[str] = Field(
        default=None, description="exchange_id this exchange answers"
    )
    artifacts: list[Artifact] = Field(default_factory=list)
    fields: list[FieldValue] = Field(default_factory=list)
    identities: list[Identity] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    outcome: Optional[Literal["committed", "halted", "pending", "no_action"]] = None
    halt_reason: Optional[str] = Field(default=None, max_length=2000)
    no_action: Optional[NoAction] = Field(
        default=None, description="Required when outcome is no_action"
    )
    provenance: Provenance
    seals: list[SealRef] = Field(default_factory=list)
    extensions: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Domain data under reverse-DNS keys, e.g. org.example.referral",
    )
