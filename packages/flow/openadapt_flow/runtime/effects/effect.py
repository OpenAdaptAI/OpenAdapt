"""Typed effects and the ``EffectVerifier`` protocol -- verify REAL effects
against a system of record, not the screen.

This is the concrete runtime for the ``Effect`` type proposed in the
Workflow-Program IR RFC (``docs/design/WORKFLOW_PROGRAM_IR.md``, PR #61):
that RFC promotes today's vision-only :class:`openadapt_flow.ir.Postcondition`
into a *typed* ``Effect`` with **system-of-record** kinds
(``record_written`` / ``field_equals``) whose probe is backend-specific (an
API / DB read), closing the transactional gap the fault-model study measured
(``docs/LIMITS.md`` "5 of 7 write faults silent"; ``benchmark/fault_model/``).

Why not the screen? A vision postcondition answers *"do the pixels look like a
save happened?"* -- which a partial save, an optimistic-UI-then-reject, a
duplicate submission, a lost update, or a double-delivered click all satisfy
while the **record** is wrong or missing. An ``EffectVerifier`` answers the
only question a record system may trust: *"is the intended record actually in
the system of record, exactly once, with the right field values?"*

Design posture -- mirror the identity gate (``runtime.identity``): **fail safe
to HALT, refuse rather than guess.** A verdict is one of

- :attr:`Verdict.CONFIRMED`     -- the effect is present and correct -> proceed;
- :attr:`Verdict.REFUTED`       -- the system of record affirmatively
  contradicts the effect (missing / duplicated / wrong value / collateral
  loss) -> HALT (never accept as success);
- :attr:`Verdict.INDETERMINATE` -- the system of record is unreachable or
  unreadable, so the effect *cannot be certified* -> HALT (never assume
  success). Consistent with the identity ladder's ``unreadable`` -> refuse.

Both non-confirmed verdicts set :attr:`EffectVerdict.should_halt`. There is no
"probably fine": an unverifiable consequential write halts, exactly as an
unreadable identity band does.

Everything here is import-light (pydantic only); concrete substrates
(``rest`` / ``fhir`` / ``document_hash``) bring their own I/O deps lazily.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ValueExpr(BaseModel):
    """A single effect-contract value: either a static ``literal`` or a
    reference to a run ``param`` (P0-3 fix).

    Before this type an :class:`Effect` carried plain static strings, so a
    PARAMETERIZED workflow verified its effects against the values baked in at
    DEMONSTRATION time (write patient "Susan" via the GUI, then verify the
    recorded demo patient "Phil"). ``ValueExpr`` lets a compiled effect declare
    ``{"param": "patient_id"}`` and have the runtime resolve it against the
    RUN's params before verification, so the record actually written is the
    record checked.

    Exactly one of :attr:`literal` / :attr:`param` is meaningful. Old bundles
    (and hand-authored effects) that pass a bare string are coerced to
    ``ValueExpr(literal=...)`` by :class:`Effect`'s validators, and this type's
    ``__eq__`` / ``__str__`` compare/render as that bare string so every
    existing reader (learning gate signatures, codegen review comments) and the
    substrate matchers behave BYTE-FOR-BYTE identically for a literal.
    """

    #: A static value baked into the contract (the v1 form). ``None`` when the
    #: value comes from a run param instead.
    literal: Optional[str] = None
    #: Name of a run parameter to resolve against at run time (``Workflow.params``
    #: overlaid by the caller's values). ``None`` for a literal.
    param: Optional[str] = None

    def resolve(self, params: Mapping[str, str]) -> Optional[str]:
        """Resolve to a concrete string against ``params``.

        A ``param`` reference reads ``params[param]`` (``None`` when the run did
        not supply it -- fail-safe: an unresolved selector matches nothing, so
        the effect REFUTEs / HALTs rather than silently confirming the wrong
        record). A pure literal returns its literal unchanged.
        """
        if self.param is not None:
            return params.get(self.param)
        return self.literal

    def resolved(self, params: Mapping[str, str]) -> "ValueExpr":
        """Return a pure-literal copy of this expression bound to ``params``."""
        return ValueExpr(literal=self.resolve(params))

    # -- transparent str-compatibility (back-compat with the v1 plain-string
    #    form): a literal ValueExpr compares, hashes, stringifies, and reprs
    #    exactly as the bare string it replaced, so existing readers and tests
    #    that treat ``effect.value`` / ``effect.match[k]`` as a string are
    #    unaffected. ---------------------------------------------------------
    def __str__(self) -> str:
        if self.literal is not None:
            return self.literal
        if self.param is not None:
            return "{" + self.param + "}"
        return ""

    def __repr__(self) -> str:
        # Mirror ``repr(str)`` for a literal so codegen review comments
        # (``idempotency_key={eff.idempotency_key!r}``) and ``dict`` reprs of a
        # ``match`` selector render as they did when values were plain strings.
        if self.param is None:
            return repr(self.literal)
        return repr("{" + self.param + "}")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ValueExpr):
            return self.literal == other.literal and self.param == other.param
        if isinstance(other, str):
            # A literal expression equals the bare string it stands for.
            return self.param is None and self.literal == other
        return NotImplemented

    def __hash__(self) -> int:
        # A literal expression must hash IDENTICALLY to the bare string it
        # stands for, so it collides with that string in sets/dicts (the
        # hash/eq invariant given ``__eq__`` treats them as equal). Existing
        # code that keys a set/dict on ``match`` values mixing plain strings
        # and literal expressions then behaves unchanged.
        if self.param is None:
            return hash(self.literal)
        return hash(("__param__", self.param))


class EffectKind(str, Enum):
    """System-of-record effect kinds (RFC section 2.2).

    These are the NEW kinds the RFC adds beyond the vision-checkable
    postcondition kinds (``text_present`` / ``region_stable`` / ...): their
    probe is a read against the system of record, not the frame.
    """

    #: A record matching :attr:`Effect.match` must exist in the system of
    #: record EXACTLY :attr:`Effect.expected_count` times (default once --
    #: at-most-once for a consequential write; catches missing, phantom,
    #: duplicate, and double-click writes).
    RECORD_WRITTEN = "record_written"
    #: The record matching :attr:`Effect.match` must carry
    #: ``field == value`` (read-back of a specific field; catches partial
    #: saves that persist the row but drop a field).
    FIELD_EQUALS = "field_equals"


#: A screen region ``(x, y, w, h)`` in the recorded/live frame's pixel space.
#: Mirrors :data:`openadapt_flow.ir.Region`; defined locally to keep this
#: module import-light (``ir`` imports :class:`Effect`, not the reverse).
Region = tuple[int, int, int, int]


class ReadbackNav(BaseModel):
    """One deterministic navigation action to RE-OPEN the written record by a
    path DIFFERENT from the write flow, before an on-screen read-back.

    Auto-derived from the demonstration (the steps the person took to view the
    saved record again — e.g. clear the form, search the patient, open the
    encounter). Deliberately tiny and model-free: a re-navigation is replayed
    by coordinate/keyboard exactly as recorded; anything a backend cannot
    perform makes the read-back INDETERMINATE (HALT), never a guessed CONFIRM.
    """

    #: ``click`` (at :attr:`point`) | ``type`` (:attr:`text`) | ``key``
    #: (:attr:`key`). An unknown action is refused by the navigator (HALT).
    action: str
    #: Click target in frame pixel space (``click`` only).
    point: Optional[tuple[int, int]] = None
    #: Literal text to type (``type`` only).
    text: Optional[str] = None
    #: Key name to press, e.g. ``"Enter"`` (``key`` only).
    key: Optional[str] = None


class ReadbackSpec(BaseModel):
    """The on-screen read-back oracle mined from the demonstration.

    Carries WHERE to re-read the saved value (:attr:`region`) and, for the
    stronger DIFFERENT-PATH variant, the re-navigation that re-opens the record
    (:attr:`renavigation`) so the read is forced through a real fetch rather
    than the form the write left on screen. Consumed by
    :class:`~openadapt_flow.runtime.effects.onscreen.OnScreenReadbackVerifier`;
    every non-onscreen substrate ignores it.

    SAFETY — the ``different_path`` flag is the gate. A ``different_path=True``
    read-back re-opens the record by an independent path (defeating the
    "the form still shows what I typed but nothing persisted" phantom-save
    class) and is safe enough to be the DEFAULT out-of-the-box oracle. A
    ``different_path=False`` (SAME-SURFACE) read-back re-reads the very region
    the write drove; it CANNOT see a phantom/optimistic save (the measured
    false-CONFIRM rate is > 0), so it is wired but NON-default — a
    halt-inducing consistency signal an operator opts into with eyes open, not
    a pass. See ``docs/LIMITS.md`` and ``benchmark/effect_readback/``.
    """

    #: Region to OCR for the saved value. None => the verifier's constructed
    #: region (a hand-configured deployment).
    region: Optional[Region] = None
    #: True when :attr:`renavigation` re-opens the record by an independent
    #: path (the strong, default-eligible oracle); False for a same-surface
    #: re-read (weak, non-default).
    different_path: bool = False
    #: The re-navigation to perform BEFORE reading (different-path only). Empty
    #: for a same-surface read-back.
    renavigation: list[ReadbackNav] = Field(default_factory=list)


class Effect(BaseModel):
    """A typed system-of-record effect (RFC ``Effect``, concrete runtime form).

    An ``Effect`` is a *contract*: what must be true of the system of record
    for a step to have actually succeeded. It is deliberately substrate-neutral
    -- the SAME ``Effect`` is checked by the REST verifier, the FHIR verifier,
    or the filesystem verifier (see :class:`EffectVerifier`). The verifier maps
    :attr:`match` / :attr:`field` onto its substrate's query language.

    Binds to the RFC (``docs/design/WORKFLOW_PROGRAM_IR.md`` section 2.2): the
    RFC's ``Effect(kind="record_written", probe="encounter exists for
    patient")`` is realized here with a machine-checkable :attr:`match`
    selector and an :attr:`expected_count`; the RFC's
    ``Effect(kind="field_equals", field="note", value=params.note)`` maps to
    :attr:`field` / :attr:`value`.
    """

    model_config = ConfigDict(validate_assignment=True)

    kind: EffectKind
    #: Field -> value selector identifying the INTENDED record in the system
    #: of record (e.g. ``{"patient_id": "p1", "type": "Triage"}``). A verifier
    #: matches a record when every selector pair is satisfied. Values compare
    #: as strings after ``str()`` so numeric ids match across JSON/DB types.
    #: Each value is a :class:`ValueExpr` -- a literal OR a run-``param``
    #: reference -- so a PARAMETERIZED workflow verifies the record it actually
    #: wrote this run, not the demonstration's record (P0-3). A bare string in
    #: v1 bundles is coerced to ``ValueExpr(literal=...)`` and behaves
    #: identically.
    match: dict[str, ValueExpr] = Field(default_factory=dict)
    #: ``field_equals`` only: the record field that must equal :attr:`value`.
    field: Optional[str] = None
    #: ``field_equals`` only: the required value of :attr:`field` (literal or a
    #: run-``param`` reference; see :attr:`match`).
    value: Optional[ValueExpr] = None
    #: ``record_written`` only: how many matching records must exist. 1 is the
    #: at-most-once contract for a consequential write; 0 asserts absence.
    expected_count: int = 1
    #: Optional idempotency / at-most-once key. When set, ``record_written``
    #: counts records bearing THIS key (via :attr:`key_field`) and requires
    #: exactly :attr:`expected_count` -- so a duplicate submission that reused
    #: the key collapses to one record and a non-idempotent duplicate (no key,
    #: or a second distinct write) is caught as ``observed_count > expected``.
    #: A :class:`ValueExpr`: bind it to a run ``param`` (or a stable run
    #: identity) so the key is PER-RUN, not the frozen demonstration literal
    #: that would collide across unrelated runs (P0-3).
    idempotency_key: Optional[ValueExpr] = None
    #: Which record field carries the idempotency key (substrate-specific;
    #: the MockMed system-of-record uses ``"key"``).
    key_field: str = "key"
    #: Cross-cutting duplicate-write / idempotency guard (kit). When True, a
    #: ``record_written`` count considers ONLY records that are NEW relative to
    #: the pre-action snapshot (by :func:`stable_id`): with the default
    #: ``expected_count=1`` the contract reads "exactly ONE NEW matching record
    #: was created by this action" -- so a selector that legitimately matches
    #: pre-existing rows (e.g. "an encounter for this patient") still catches a
    #: double-submit (2 new rows) and a phantom write (0 new rows). Requires a
    #: REACHABLE pre-state baseline: when the pre-state could not be read the
    #: verdict is INDETERMINATE -> HALT (a delta against an unknown baseline is
    #: never guessed). Works identically on every substrate (REST / SQL / FHIR
    #: / filesystem) because the delta is computed in the shared judge.
    #: ``record_written`` ONLY (validated -- a ``field_equals`` contract
    #: refuses it rather than silently ignoring it). Caveat for substrates
    #: whose records carry no ``id``: newness falls back to whole-record
    #: identity (:func:`stable_id`), so a NEW row byte-identical to a
    #: pre-existing one is not counted as new -- prefer substrates/queries
    #: that expose a stable id when using this guard.
    #: Default False -- absolute counting, byte-for-byte the v1 behavior.
    count_new_only: bool = False
    #: ``record_written`` only: also REFUTE when a record that existed in the
    #: pre-state (``before``) and does NOT match :attr:`match` has since
    #: vanished -- collateral loss. This is what catches a stale / lost-update
    #: (last-write-wins) fault: our row lands (count 1, looks fine) while a
    #: concurrent actor's row was silently destroyed.
    forbid_collateral_loss: bool = True
    #: Consequential-write flag (mirrors ``Step.risk`` / RFC ``State.risk``).
    #: Compensation (``effects.compensation``) only fires for irreversible
    #: effects; a reversible effect just halts.
    risk: str = "reversible"
    #: Human-readable probe description (mirrors the RFC's illustrative
    #: ``probe="encounter exists for patient"``); for audit/logging only.
    probe: Optional[str] = None
    #: How long a verifier may poll the system of record before ruling
    #: INDETERMINATE (the SoR write may lag the GUI paint).
    timeout_s: float = 5.0
    #: Set by the compiler's effect miner
    #: (``compiler.effect_mining``) on a PLACEHOLDER effect: the step is a
    #: consequential write, but its system-of-record binding (which API /
    #: record / idempotency key) was NOT derivable from the demonstration —
    #: it is "irreducibly app-specific" (RFC ``WORKFLOW_PROGRAM_IR.md`` §7).
    #: The miner refuses to INVENT an endpoint, so :attr:`match` here is a
    #: sentinel, not a real selector. Such an effect must never be silently
    #: trusted: a run treats it as fail-safe (the replayer HALTs rather than
    #: verify a fabricated binding — see ``runtime.replayer._verify_effects``)
    #: until an operator completes the binding and clears this flag.
    needs_operator_confirmation: bool = False
    #: On-screen read-back oracle (the no-API default for GUI-only recordings).
    #: Set by the compiler's effect miner (``compiler.effect_mining``) when the
    #: demonstration shows the person VIEWING the saved value: it names the
    #: region to re-OCR and, for the DIFFERENT-PATH variant, the re-navigation
    #: that re-opens the record. Consumed ONLY by the ``onscreen`` substrate
    #: (:class:`~openadapt_flow.runtime.effects.onscreen.OnScreenReadbackVerifier`);
    #: every system-of-record substrate ignores it. None => not an on-screen
    #: read-back effect (the structured/placeholder paths are unchanged).
    readback: Optional[ReadbackSpec] = None

    # -- back-compat coercion: accept the v1 plain-string JSON form ----------
    @staticmethod
    def _coerce_expr(v: Any) -> Any:
        """Coerce a bare string / None into a :class:`ValueExpr` shape.

        v1 bundles serialize effect values as plain strings
        (``"value": "Phil"``); this lets them load into the parameterized
        ``ValueExpr`` fields unchanged. A ``dict`` (the new serialized form
        ``{"literal": ...}`` / ``{"param": ...}``) and an existing ``ValueExpr``
        pass straight through to pydantic.
        """
        if isinstance(v, str):
            return ValueExpr(literal=v)
        return v

    @field_validator("match", mode="before")
    @classmethod
    def _coerce_match(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {k: cls._coerce_expr(val) for k, val in v.items()}
        return v

    @field_validator("value", "idempotency_key", mode="before")
    @classmethod
    def _coerce_value(cls, v: Any) -> Any:
        return cls._coerce_expr(v)

    @model_validator(mode="after")
    def _count_new_only_scope(self) -> "Effect":
        """``count_new_only`` is a ``record_written`` guard; refuse (fail
        loud) rather than silently ignore it on a ``field_equals`` contract."""
        if self.count_new_only and self.kind is not EffectKind.RECORD_WRITTEN:
            raise ValueError(
                "count_new_only applies only to record_written effects "
                "(a field_equals read-back has no newness delta)"
            )
        return self

    # -- run-time parameter binding (P0-3) -----------------------------------
    def resolve(self, params: Mapping[str, str]) -> "Effect":
        """Return a copy with every :class:`ValueExpr` bound to ``params``.

        The runtime calls this BEFORE snapshotting the pre-state and verifying,
        so ``match`` / ``value`` / ``idempotency_key`` reflect the RECORD THIS
        RUN WROTE, not the demonstration's. A pure-literal effect (a v1 bundle)
        is returned unchanged in value -- ``resolve`` is a no-op for it, which
        is why an old bundle behaves identically.
        """
        return self.model_copy(
            update={
                "match": {k: v.resolved(params) for k, v in self.match.items()},
                "value": None if self.value is None else self.value.resolved(params),
                "idempotency_key": (
                    None
                    if self.idempotency_key is None
                    else self.idempotency_key.resolved(params)
                ),
            }
        )

    def contract_hash(self) -> str:
        """A stable, NON-secret-bearing digest of the (resolved) contract.

        Persisted in the RunReport for auditability: two runs whose effect
        contracts resolved to different records/values (or idempotency keys)
        have different hashes, and a duplicated run has the same hash. Being a
        one-way SHA-256 digest it records THAT the contract differed without
        exposing the underlying value (e.g. a patient identifier).
        """
        payload = {
            "kind": self.kind.value,
            "match": {k: str(v) for k, v in sorted(self.match.items())},
            "field": self.field,
            "value": None if self.value is None else str(self.value),
            "expected_count": self.expected_count,
            "idempotency_key": (
                None if self.idempotency_key is None else str(self.idempotency_key)
            ),
            "key_field": self.key_field,
            "forbid_collateral_loss": self.forbid_collateral_loss,
        }
        # Included ONLY when set, so every pre-existing contract (and every
        # governed-run authorization / completed-effect ledger entry that binds
        # its hash — PR #129) keeps its exact digest.
        if self.count_new_only:
            payload["count_new_only"] = True
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"sha256:{digest}"


class EffectState(BaseModel):
    """A snapshot of the system of record captured BEFORE the action.

    Two jobs: (1) establish a baseline so ``record_written`` can count only
    records that appeared *because of this action* (delta / at-most-once),
    and (2) let :attr:`Effect.forbid_collateral_loss` detect a pre-existing
    record that the action destroyed (lost update). :attr:`reachable` is
    False when the system of record could not be read at capture time -- the
    verifier factors that into an INDETERMINATE verdict rather than assuming
    an empty baseline.
    """

    substrate: str
    reachable: bool
    #: The system-of-record records present at capture time. Each is a plain
    #: dict in the substrate's native shape (a FaultDB row, a FHIR resource, a
    #: filesystem descriptor). Empty when unreachable.
    records: list[dict[str, Any]] = Field(default_factory=list)
    captured_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    detail: dict[str, Any] = Field(default_factory=dict)


class Verdict(str, Enum):
    """The three-valued effect verdict (fail-safe to HALT)."""

    CONFIRMED = "confirmed"  # effect present and correct -> proceed
    REFUTED = "refuted"  # SoR contradicts the effect -> HALT
    INDETERMINATE = "indeterminate"  # SoR unreachable/unreadable -> HALT


class EffectVerdict(BaseModel):
    """Outcome of verifying one :class:`Effect` against a system of record."""

    verdict: Verdict
    kind: EffectKind
    substrate: str = ""
    reason: str = ""
    observed_count: Optional[int] = None
    expected_count: Optional[int] = None
    observed_value: Optional[str] = None
    expected_value: Optional[str] = None
    #: Records the verifier judged to MATCH :attr:`Effect.match` (native
    #: shape), so a caller / compensator can act on them (e.g. delete the
    #: extras of a duplicate).
    matched_records: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def should_halt(self) -> bool:
        """True unless the effect was CONFIRMED -- a REFUTED or INDETERMINATE
        consequential effect must halt the run (never proceed on an
        unverified or contradicted write)."""
        return self.verdict is not Verdict.CONFIRMED

    @property
    def confirmed(self) -> bool:
        return self.verdict is Verdict.CONFIRMED


@runtime_checkable
class EffectVerifier(Protocol):
    """Independent verification of a typed :class:`Effect` against a REAL
    system of record (an API / DB / filesystem), never the screen.

    A verifier is bound to one substrate (an OpenEMR FHIR endpoint, a REST
    system of record, a document store) and knows how to (1) snapshot the
    system of record before the action and (2) decide whether the intended
    effect actually landed. The same :class:`Effect` contract is honored by
    every substrate -- that substrate-agnosticism is the point (an
    OpenEMR-shaped protocol would just overfit MockMed's replacement).

    Contract for implementers:

    - :meth:`verify` must return :attr:`Verdict.INDETERMINATE` (never raise,
      never guess CONFIRMED) when the system of record is unreachable or its
      response is unusable -- the run then HALTs, consistent with the identity
      gate's refuse-rather-than-guess posture.
    - :meth:`verify` returns :attr:`Verdict.REFUTED` only on AFFIRMATIVE
      contradiction (wrong count, wrong value, collateral loss) -- a positive
      signal the effect is bad, distinct from "could not tell".
    """

    #: Stable substrate name for audit (``"rest"`` / ``"fhir"`` / ``"fs"``).
    substrate: str

    def capture_pre_state(self, context: Any = None) -> EffectState:
        """Snapshot the system of record before the action runs."""
        ...

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        """Decide whether ``expected`` actually landed in the system of
        record, given the pre-action snapshot ``before``."""
        ...


# -- shared matching helpers (used by every substrate) -----------------------


def record_matches(record: dict[str, Any], selector: dict[str, str]) -> bool:
    """Whether ``record`` satisfies every ``field == value`` in ``selector``.

    Comparison is on ``str()`` of both sides so a numeric id in JSON (``1``)
    matches a string selector (``"1"``) without the caller tracking types.
    An empty selector matches every record (used by absence checks).
    """
    for key, want in selector.items():
        if str(record.get(key, None)) != str(want):
            return False
    return True


def stable_id(record: dict[str, Any]) -> Any:
    """A record's identity for pre/post delta accounting.

    Prefers an ``id`` field; falls back to the whole record's sorted-items
    tuple so substrates without an explicit id still support collateral-loss
    detection.
    """
    if "id" in record:
        return ("id", record["id"])
    return tuple(sorted((k, str(v)) for k, v in record.items()))
