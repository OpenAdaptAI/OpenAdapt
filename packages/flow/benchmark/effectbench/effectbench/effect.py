"""Typed system-of-record effects and the ``EffectVerifier`` protocol.

This is the substrate-neutral effect **mechanism** the benchmark scores with:
an :class:`Effect` is a machine-checkable contract for what must be true of a
system of record for a step to have actually succeeded, and an
:class:`EffectVerifier` reads that record independently of the screen. It is
vendored here verbatim (mechanism only) from the OpenAdapt engine so EffectBench
installs and runs with **pydantic as its only dependency** -- no OpenAdapt
codebase required.

Why not the screen? A vision postcondition answers *"do the pixels look like a
save happened?"* -- which a partial save, an optimistic-UI-then-reject, a
duplicate submission, a lost update, or a double-delivered click all satisfy
while the record is wrong or missing. An ``EffectVerifier`` answers the only
question a record system may trust: *"is the intended record actually in the
system of record, exactly once, with the right field values?"*

Fail-safe posture: a verdict is CONFIRMED (proceed), REFUTED (the record
contradicts the effect -> halt), or INDETERMINATE (the record is unreachable ->
halt, never assume success).
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
    """A single effect-contract value: a static ``literal`` or a run ``param``.

    Lets a parameterized task verify the record it actually wrote THIS run
    (``{"param": "patient_id"}`` resolved against the run's params) rather than
    a value baked in at authoring time. A bare string is coerced to
    ``ValueExpr(literal=...)`` and compares/stringifies exactly as that string,
    so authored effects can pass plain strings.
    """

    literal: Optional[str] = None
    param: Optional[str] = None

    def resolve(self, params: Mapping[str, str]) -> Optional[str]:
        """Resolve to a concrete string against ``params`` (fail-safe: an
        unresolved param yields ``None``, which matches nothing -> REFUTE)."""
        if self.param is not None:
            return params.get(self.param)
        return self.literal

    def resolved(self, params: Mapping[str, str]) -> "ValueExpr":
        return ValueExpr(literal=self.resolve(params))

    def __str__(self) -> str:
        if self.literal is not None:
            return self.literal
        if self.param is not None:
            return "{" + self.param + "}"
        return ""

    def __repr__(self) -> str:
        if self.param is None:
            return repr(self.literal)
        return repr("{" + self.param + "}")

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ValueExpr):
            return self.literal == other.literal and self.param == other.param
        if isinstance(other, str):
            return self.param is None and self.literal == other
        return NotImplemented

    def __hash__(self) -> int:
        if self.param is None:
            return hash(self.literal)
        return hash(("__param__", self.param))


class EffectKind(str, Enum):
    """System-of-record effect kinds."""

    #: A record matching :attr:`Effect.match` must exist exactly
    #: :attr:`Effect.expected_count` times (default once -- at-most-once for a
    #: consequential write; catches missing, phantom, duplicate writes).
    RECORD_WRITTEN = "record_written"
    #: The matched record must carry ``field == value`` (read-back; catches a
    #: partial save that persists the row but drops a field).
    FIELD_EQUALS = "field_equals"


class Effect(BaseModel):
    """A typed system-of-record effect contract (substrate-neutral)."""

    model_config = ConfigDict(validate_assignment=True)

    kind: EffectKind
    #: Field -> value selector identifying the INTENDED record. A verifier
    #: matches a record when every selector pair is satisfied (string compare).
    match: dict[str, ValueExpr] = Field(default_factory=dict)
    #: ``field_equals`` only: the record field that must equal :attr:`value`.
    field: Optional[str] = None
    #: ``field_equals`` only: the required value of :attr:`field`.
    value: Optional[ValueExpr] = None
    #: ``record_written`` only: how many matching records must exist.
    expected_count: int = 1
    #: Optional at-most-once / idempotency key.
    idempotency_key: Optional[ValueExpr] = None
    #: Which record field carries the idempotency key.
    key_field: str = "key"
    #: Count only records NEW relative to the pre-state (requires a readable
    #: baseline; else INDETERMINATE). ``record_written`` only.
    count_new_only: bool = False
    #: Also REFUTE when a pre-existing non-matching record vanished (collateral
    #: loss -- the stale / lost-update fault).
    forbid_collateral_loss: bool = True
    #: Consequential-write flag (audit only here).
    risk: str = "reversible"
    #: Human-readable probe description (audit/logging only).
    probe: Optional[str] = None
    #: How long a verifier may poll before ruling INDETERMINATE.
    timeout_s: float = 5.0

    @staticmethod
    def _coerce_expr(v: Any) -> Any:
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
        if self.count_new_only and self.kind is not EffectKind.RECORD_WRITTEN:
            raise ValueError(
                "count_new_only applies only to record_written effects "
                "(a field_equals read-back has no newness delta)"
            )
        return self

    def resolve(self, params: Mapping[str, str]) -> "Effect":
        """Return a copy with every :class:`ValueExpr` bound to ``params``."""
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
        """A stable, non-secret-bearing SHA-256 digest of the resolved contract.

        Byte-compatible with the OpenAdapt engine's ``Effect.contract_hash`` so a
        submission's effect hash cross-checks against the reference engine.
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
        if self.count_new_only:
            payload["count_new_only"] = True
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"sha256:{digest}"


class EffectState(BaseModel):
    """A snapshot of the system of record captured BEFORE the action."""

    substrate: str
    reachable: bool
    #: The records present at capture time (each a plain dict). Empty when
    #: unreachable.
    records: list[dict[str, Any]] = Field(default_factory=list)
    captured_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    detail: dict[str, Any] = Field(default_factory=dict)


class Verdict(str, Enum):
    """The three-valued effect verdict (fail-safe to HALT)."""

    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    INDETERMINATE = "indeterminate"


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
    matched_records: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def should_halt(self) -> bool:
        return self.verdict is not Verdict.CONFIRMED

    @property
    def confirmed(self) -> bool:
        return self.verdict is Verdict.CONFIRMED


@runtime_checkable
class EffectVerifier(Protocol):
    """Independent verification of an :class:`Effect` against a real system of
    record (an API / DB / filesystem), never the screen.

    Implementers must return INDETERMINATE (never guess CONFIRMED) when the
    record is unreachable, and REFUTED only on affirmative contradiction.
    """

    substrate: str

    def capture_pre_state(self, context: Any = None) -> EffectState:
        """Snapshot the system of record before the action runs."""
        ...

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        """Decide whether ``expected`` actually landed, given ``before``."""
        ...


def record_matches(record: dict[str, Any], selector: dict[str, str]) -> bool:
    """Whether ``record`` satisfies every ``field == value`` in ``selector``.

    Comparison is on ``str()`` of both sides so a numeric id matches a string
    selector. An empty selector matches every record (absence checks).
    """
    for key, want in selector.items():
        if str(record.get(key, None)) != str(want):
            return False
    return True


def stable_id(record: dict[str, Any]) -> Any:
    """A record's identity for pre/post delta accounting."""
    if "id" in record:
        return ("id", record["id"])
    return tuple(sorted((k, str(v)) for k, v in record.items()))
