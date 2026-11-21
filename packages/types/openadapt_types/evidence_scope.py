"""What a receipt's evidence covered: the evidence-scope block.

Every receipt states, in one closed block, how its write was checked and what
that check did not see:

- ``check_level``: the level on the one ladder
  (:mod:`openadapt_types.check_level`).
- ``word``: the receipt word (``VERIFIED``, ``SHOWN``, ``CONFIRMED``,
  ``ATTESTED`` or ``RECORDED``), from :func:`~openadapt_types.check_level.outcome_word`.
- ``basis``: who produced the evidence. Required for ``VERIFIED``.
- ``wire``: the channel the run observed and the protocols it parsed.
  Watching the wire is not a read-back: it never makes a word stronger.
- ``read_back_at``: when the read-back ran. ``VERIFIED`` means "as of this
  time, the read-back showed the value", never "persisted".
- ``recheck_window_s``: how long after ``read_back_at`` a recheck reads the
  record again, when one is scheduled.
- ``not_covered``: what the evidence did not cover. It always lists
  ``server_side_async_effects`` and ``outbound_effects``: no read-back sees a
  job the server runs later, or a message it sends out. A producer that did
  not record when its read-back ran lists ``read_back_time`` instead of a
  ``read_back_at`` (a run report has no per-read time yet).

The block holds only closed values (enums, a timestamp, a count), like the
rest of a ``record/run/v1`` predicate. It is additive: a receipt without it
still verifies, and :func:`predicate_evidence_scope_problem` checks it only
when it is present.
"""

from __future__ import annotations

from typing import Any, Final, Iterable, Literal, Mapping, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from openadapt_types.check_level import (
    Basis,
    CheckLevel,
    OutcomeWord,
    VERIFIED_LEVELS,
    level_for_basis,
    outcome_word,
)

__all__ = [
    "ALWAYS_NOT_COVERED",
    "EVIDENCE_SCOPE_FIELD",
    "EVIDENCE_SCOPE_SCHEMA",
    "EvidenceScopeV1",
    "NotCovered",
    "WireChannel",
    "WireProtocol",
    "WireScope",
    "build_evidence_scope",
    "evidence_scope_problem",
    "predicate_evidence_scope_problem",
    "with_evidence_scope",
]

EVIDENCE_SCOPE_SCHEMA: Final = "openadapt.evidence-scope/v1"
#: The additive ``record/run/v1`` predicate field that carries the block.
EVIDENCE_SCOPE_FIELD: Final = "evidence_scope"

#: The channel the run watched. ``none``: it watched no wire.
WireChannel = Literal[
    "none",
    "browser_network",
    "http_proxy",
    "api_client",
    "db_connection",
]
WireProtocol = Literal["http", "graphql", "fhir", "sql", "websocket", "grpc", "smtp"]
#: What a check did not cover.
NotCovered = Literal[
    "server_side_async_effects",
    "outbound_effects",
    "changes_after_read_back",
    "fields_not_read",
    "other_records",
    "other_actors",
    "read_back_time",
]
#: Listed on every receipt: no read-back observes these.
ALWAYS_NOT_COVERED: Final[tuple[NotCovered, ...]] = (
    "server_side_async_effects",
    "outbound_effects",
)
_LEVEL_LABEL = Literal[
    "unchecked",
    "same_screen",
    "reopened",
    "separate_session",
    "system_of_record",
    "counterparty_confirmed",
]
_ISO_SECONDS = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
_MAX_RECHECK_S: Final = 30 * 24 * 3600


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class WireScope(_Closed):
    channel: WireChannel
    protocols_parsed: list[WireProtocol] = Field(default_factory=list, max_length=7)

    @model_validator(mode="after")
    def _closed(self) -> "WireScope":
        if len(set(self.protocols_parsed)) != len(self.protocols_parsed):
            raise ValueError("wire.protocols_parsed repeats a protocol")
        if self.channel == "none" and self.protocols_parsed:
            raise ValueError("a run that watched no wire parsed no protocol")
        return self


class EvidenceScopeV1(_Closed):
    """The evidence-scope block (``openadapt.evidence-scope/v1``)."""

    schema_: Literal["openadapt.evidence-scope/v1"] = Field(alias="schema")
    check_level: _LEVEL_LABEL
    word: OutcomeWord
    basis: Optional[Basis] = None
    wire: WireScope
    read_back_at: Optional[StrictStr] = Field(default=None, pattern=_ISO_SECONDS)
    recheck_window_s: Optional[StrictInt] = Field(
        default=None, ge=0, le=_MAX_RECHECK_S
    )
    not_covered: list[NotCovered] = Field(min_length=2, max_length=7)

    @model_validator(mode="after")
    def _consistent(self) -> "EvidenceScopeV1":
        level = CheckLevel.from_label(self.check_level)
        if len(set(self.not_covered)) != len(self.not_covered):
            raise ValueError("not_covered repeats a code")
        if not set(ALWAYS_NOT_COVERED) <= set(self.not_covered):
            raise ValueError(
                "not_covered must list server_side_async_effects and outbound_effects"
            )
        if self.basis is not None and level_for_basis(self.basis) != level:
            raise ValueError(f"basis {self.basis} does not reach {self.check_level}")
        if self.word == "VERIFIED":
            if level not in VERIFIED_LEVELS or self.basis is None:
                raise ValueError("VERIFIED needs level 3 or 4 and a basis")
            if self.read_back_at is None and "read_back_time" not in self.not_covered:
                raise ValueError("VERIFIED needs read_back_at, or says it has none")
        if self.word == "SHOWN" and not (
            CheckLevel.SAME_SCREEN <= level <= CheckLevel.REOPENED
        ):
            raise ValueError("SHOWN is a level 1 or 2 re-read of the acting screen")
        if self.word == "CONFIRMED" and level is not CheckLevel.COUNTERPARTY_CONFIRMED:
            raise ValueError("CONFIRMED is level 5, the counterparty's acknowledgment")
        if self.read_back_at is not None and "read_back_time" in self.not_covered:
            raise ValueError("a block with read_back_at covers the read-back time")
        if self.recheck_window_s is not None and self.read_back_at is None:
            raise ValueError("a recheck window needs read_back_at")
        return self

    def wire_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


def build_evidence_scope(
    level: CheckLevel,
    *,
    basis: Optional[str] = None,
    confirmed: bool = True,
    attested: bool = False,
    minimum: CheckLevel = CheckLevel.SEPARATE_SESSION,
    wire_channel: str = "none",
    protocols_parsed: Iterable[str] = (),
    read_back_at: Optional[str] = None,
    recheck_window_s: Optional[int] = None,
    not_covered: Iterable[str] = (),
) -> dict[str, Any]:
    """The evidence-scope block for one check, as a wire dict.

    The word comes from :func:`~openadapt_types.check_level.outcome_word`, so
    every path that hands in the same evidence gets the same word. A basis is
    dropped from the block when the word is not ``VERIFIED``, so a block never
    names a basis it did not rely on. Raises ``ValueError`` for an
    inconsistent block.
    """

    level = CheckLevel(level)
    word = outcome_word(
        level, basis, confirmed=confirmed, attested=attested, minimum=minimum
    )
    codes = list(dict.fromkeys((*ALWAYS_NOT_COVERED, *not_covered)))
    try:
        block = EvidenceScopeV1.model_validate(
            {
                "schema": EVIDENCE_SCOPE_SCHEMA,
                "check_level": level.label,
                "word": word,
                "basis": basis if word == "VERIFIED" else None,
                "wire": {
                    "channel": wire_channel,
                    "protocols_parsed": list(dict.fromkeys(protocols_parsed)),
                },
                "read_back_at": read_back_at,
                "recheck_window_s": recheck_window_s,
                "not_covered": codes,
            }
        )
    except ValidationError as error:
        raise ValueError(f"invalid evidence scope: {error}") from None
    return block.wire_dict()


def evidence_scope_problem(block: object) -> Optional[str]:
    """None when ``block`` is a valid evidence-scope block; else the reason."""

    if not isinstance(block, Mapping):
        return "evidence_scope"
    try:
        EvidenceScopeV1.model_validate(dict(block))
    except ValidationError:
        return "evidence_scope"
    return None


#: The ``record/run/v1`` outcome each word is consistent with.
_PREDICATE_OUTCOMES: Final[Mapping[str, frozenset[str]]] = {
    "VERIFIED": frozenset({"VERIFIED"}),
    "CONFIRMED": frozenset({"VERIFIED"}),
    "SHOWN": frozenset({"UNKNOWN", "STOPPED", "FAILED"}),
    "ATTESTED": frozenset({"UNKNOWN", "STOPPED", "FAILED"}),
    "RECORDED": frozenset({"UNKNOWN", "STOPPED", "FAILED"}),
}


def predicate_evidence_scope_problem(predicate: Mapping[str, Any]) -> Optional[str]:
    """Check a ``record/run/v1`` predicate's optional ``evidence_scope``.

    A predicate without the field is an older receipt and passes. With it,
    the block must be valid and its word must agree with the predicate's
    ``outcome``: a ``VERIFIED`` outcome needs a ``VERIFIED`` or ``CONFIRMED``
    word, and no other outcome may carry either.
    """

    if EVIDENCE_SCOPE_FIELD not in predicate:
        return None
    block = predicate[EVIDENCE_SCOPE_FIELD]
    problem = evidence_scope_problem(block)
    if problem is not None:
        return problem
    allowed = _PREDICATE_OUTCOMES[block["word"]]
    if predicate.get("outcome") not in allowed:
        return "evidence_scope_outcome"
    return None


def with_evidence_scope(
    predicate: Mapping[str, Any], block: Mapping[str, Any]
) -> dict[str, Any]:
    """A copy of ``predicate`` with ``block`` added as ``evidence_scope``.

    Raises ``ValueError`` when the block is invalid or disagrees with the
    predicate's outcome.
    """

    out = dict(predicate)
    out[EVIDENCE_SCOPE_FIELD] = dict(block)
    problem = predicate_evidence_scope_problem(out)
    if problem is not None:
        raise ValueError(f"evidence scope refused: {problem}")
    return out

