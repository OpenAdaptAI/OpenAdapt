"""Reconcile one prior authorization or claim across the EHR, clearinghouse
and payer.

    from openadapt_flow.reconcile import ReconcileSpec, reconcile

    spec = ReconcileSpec.load("pa.reconcile.yaml")      # openadapt.reconcile/v1
    result = reconcile(spec, {"submission_id": "PA-1042"})
    result.outcome      # CONFIRMED, REFUTED or UNCERTAIN (openadapt_flow.verifier)
    result.breaks       # not_received, mismatch, not_written_back, duplicate
    result.scope        # what was read, and what wasn't

Each leg is an ``openadapt.verify/v1`` contract built by
:meth:`openadapt_flow.verifier.Contract.from_dict` and read through
``deployment.build_effect_verifier``. Each check is an ``Effect`` judged by
the verifier's shared ``judge_records``. There is no second engine: this
module only chooses which effects to check, carries the values one leg reads
to the next, and names the break each failed check is. See
``docs/RECONCILE.md``.

The reads are of each store's current state; no baseline is taken, so a leg
may not declare ``count_new_only`` or ``exact_new_set``. Nothing here writes,
calls a model or sends values outside the stores it reads. A leg that can't
be read, or that depends on a value an earlier leg didn't give, is
``UNCERTAIN``: never a pass.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.reconcile import x12
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.effect import (
    EffectState,
    EffectVerdict,
    ValueExpr,
    Verdict,
)
from openadapt_flow.verifier import (
    READ_PATHS,
    Contract,
    Outcome,
    Scope,
    VerifierError,
    Window,
)
from openadapt_flow.verify_run import _STRENGTH_BY_TIER, _STRENGTH_RANK

__all__ = [
    "Break",
    "LegResult",
    "ReconcileSpec",
    "Reconciliation",
    "reconcile",
]

SCHEMA = "openadapt.reconcile/v1"
Role = Literal["ehr", "clearinghouse", "payer", "writeback"]
BreakKind = Literal["not_received", "mismatch", "not_written_back", "duplicate"]

x12.register()

_OUTCOME = {
    Verdict.CONFIRMED: Outcome.CONFIRMED,
    Verdict.REFUTED: Outcome.REFUTED,
    Verdict.INDETERMINATE: Outcome.UNCERTAIN,
}
_RANK = {Outcome.CONFIRMED: 0, Outcome.UNCERTAIN: 1, Outcome.REFUTED: 2}
_PATHS = {**READ_PATHS, "x12": "file"}

#: What no reconciliation covers.
NOT_COVERED = (
    "who made each record, or when: the stores' state at read_at, not a change",
    "records that change after read_at",
    "service lines and fields the spec doesn't name",
    "stores the spec doesn't read: payer portals, fax, other clearinghouses",
    "whether the request's documentation (CRD, DTR, attachments) was complete",
)


class Join(BaseModel):
    """Copy ``field`` from the record that this record's ``via`` field
    references (``Patient/123``) in the same read, such as a ``_include``d
    Patient's member id."""

    model_config = ConfigDict(extra="forbid")

    via: str
    field: str


class Due(BaseModel):
    """When the leg's record must exist: ``within_s`` after the time in
    parameter ``after`` (ISO 8601). Before then, absence is UNCERTAIN."""

    model_config = ConfigDict(extra="forbid")

    after: str
    within_s: int = Field(ge=0)


class When(BaseModel):
    """Check the leg only when parameter ``param`` is one of ``values``."""

    model_config = ConfigDict(extra="forbid")

    param: str
    values: list[str] = Field(min_length=1)


class Leg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    environment_id: str = "unnamed-environment"
    #: The same wiring as ``verify.yaml``'s ``verifier:``.
    verifier: EffectsConfig
    #: Selects this unit of work's record: exactly one must match.
    identity: dict[str, ValueExpr] = Field(min_length=1)
    #: Each field of the one matched record that must equal a value.
    compare: dict[str, ValueExpr] = Field(default_factory=dict)
    #: Selects every submission of the same work (member, code, date): more
    #: than one is a duplicate, even under different identities.
    duplicate_key: dict[str, ValueExpr] = Field(default_factory=dict)
    #: Values later legs read as ``{param: "<leg>.<name>"}``: name -> field.
    provides: dict[str, str] = Field(default_factory=dict)
    join: dict[str, Join] = Field(default_factory=dict)
    due: Optional[Due] = None
    when: Optional[When] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            data = dict(data)
            for key in ("identity", "compare", "duplicate_key"):
                body = data.get(key)
                if isinstance(body, Mapping):
                    data[key] = {
                        k: v if isinstance(v, Mapping) else {"literal": str(v)}
                        for k, v in body.items()
                    }
        return data

    def contract(self, name: str) -> Contract:
        """This leg as a verifier contract: presence, duplicates, fields."""

        quiet = {"forbid_collateral_loss": False, "timeout_s": 0.0}
        effects: list[dict[str, Any]] = [
            {
                "kind": "record_written",
                "match": self.identity,
                "expected_count": 1,
                "probe": "presence",
                **quiet,
            }
        ]
        if self.duplicate_key:
            effects.append(
                {
                    "kind": "record_written",
                    "match": self.duplicate_key,
                    "expected_count": 1,
                    "probe": "duplicates",
                    **quiet,
                }
            )
        for name_, value in self.compare.items():
            effects.append(
                {
                    "kind": "field_equals",
                    "match": self.identity,
                    "field": name_,
                    "value": value,
                    "probe": f"field:{name_}",
                    **quiet,
                }
            )
        data = {
            "schema_version": "openadapt.verify/v1",
            "environment_id": self.environment_id,
            "verifier": self.verifier.model_dump(mode="json", exclude_defaults=True),
            "effects": [{k: _dump(v) for k, v in e.items()} for e in effects],
        }
        return Contract.from_dict(data, name=name)


def _dump(value: Any) -> Any:
    if isinstance(value, ValueExpr):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {k: _dump(v) for k, v in value.items()}
    return value


class ReconcileSpec(BaseModel):
    """A parsed ``openadapt.reconcile/v1`` spec. Legs run in order; the
    anchor (the EHR's record of the request) runs first."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.reconcile/v1"] = SCHEMA
    unit: Literal["prior_authorization", "claim"]
    anchor: str
    legs: dict[str, Leg] = Field(min_length=2)

    @model_validator(mode="after")
    def _check(self) -> "ReconcileSpec":
        names = list(self.legs)
        if names[0] != self.anchor:
            raise ValueError("the anchor must be the first leg")
        if self.legs[self.anchor].role != "ehr":
            raise ValueError("the anchor is the EHR's record (role: ehr)")
        for name, leg in self.legs.items():
            if "." in name:
                raise ValueError(f"leg name {name!r} may not contain '.'")
            if leg.due is not None and name == self.anchor:
                raise ValueError("the anchor has no due time")
            for later in names[names.index(name) :]:
                for dep in _deps(self.legs[later]):
                    if dep.split(".", 1)[0] == later:
                        raise ValueError(f"leg {later!r} reads its own value {dep!r}")
        return self

    @classmethod
    def load(cls, path: Union[str, Path]) -> "ReconcileSpec":
        import yaml

        path = Path(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return cls.model_validate(data)
        except Exception as exc:  # noqa: BLE001 - one error type for callers
            raise VerifierError(f"{path.name}: {exc}") from exc

    @property
    def digest(self) -> str:
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, default=str)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _deps(leg: Leg) -> set[str]:
    """The ``<leg>.<name>`` values a leg reads."""

    out: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            name = value.get("param")
            if isinstance(name, str) and "." in name:
                out.add(name)
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(leg.model_dump(mode="json"))
    for name in (
        leg.due.after if leg.due else None,
        leg.when.param if leg.when else None,
    ):
        if name and "." in name:
            out.add(name)
    return out


@dataclass(frozen=True)
class Break:
    kind: BreakKind
    leg: str
    role: str
    #: REFUTED: the store contradicts agreement. UNCERTAIN: it may still
    #: resolve (inside the due time) or couldn't be told.
    outcome: Outcome
    #: Field names that differ (``mismatch``, ``not_written_back``).
    fields: tuple[str, ...] = ()
    #: References to the records involved (``Claim/123``, ``file#ctl.1``).
    records: tuple[str, ...] = ()
    observed_count: Optional[int] = None
    due_at: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "leg": self.leg,
            "role": self.role,
            "outcome": self.outcome.value,
            "fields": list(self.fields),
            "records": list(self.records),
            "observed_count": self.observed_count,
            "due_at": self.due_at,
            "reason": self.reason,
        }


@dataclass
class LegResult:
    name: str
    role: str
    outcome: Outcome
    #: ``presence``, ``duplicates`` and ``field:<name>``: ``held``,
    #: ``refuted`` or ``not_checked``.
    checked: dict[str, str] = field(default_factory=dict)
    records: tuple[str, ...] = ()
    reasons: list[str] = field(default_factory=list)
    skipped: bool = False
    scope: Optional[Scope] = None
    contract_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "outcome": self.outcome.value,
            "checked": dict(self.checked),
            "records": list(self.records),
            "reasons": list(self.reasons),
            "skipped": self.skipped,
            "scope": self.scope.to_dict() if self.scope else None,
            "contract_digest": self.contract_digest,
        }


@dataclass
class Reconciliation:
    unit: str
    outcome: Outcome
    breaks: list[Break]
    legs: list[LegResult]
    scope: Scope
    spec_digest: str
    reasons: list[str] = field(default_factory=list)

    @property
    def agrees(self) -> bool:
        return self.outcome is Outcome.CONFIRMED

    def to_dict(self) -> dict[str, Any]:
        """Closed fields only: no member ids, names, codes or dates."""

        return {
            "schema_version": "openadapt.reconciliation/v1",
            "unit": self.unit,
            "outcome": self.outcome.value,
            "agrees": self.agrees,
            "breaks": [b.to_dict() for b in self.breaks],
            "legs": [leg.to_dict() for leg in self.legs],
            "scope": self.scope.to_dict(),
            "reasons": list(self.reasons),
            "spec_sha256": self.spec_digest,
        }


def _now_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # a time without a zone can't be compared
    return parsed


def _refs(verifier: Any, records: list[dict[str, Any]]) -> tuple[str, ...]:
    kind = getattr(verifier, "resource_type", None)
    out = []
    for r in records:
        rid = r.get("id")
        if rid is not None:
            out.append(
                f"{kind}/{rid}" if kind and verifier.substrate == "fhir" else str(rid)
            )
    return tuple(out)


def _joined(
    records: list[dict[str, Any]], join: Mapping[str, Join]
) -> Optional[list[dict[str, Any]]]:
    """``records`` with each join applied, or None when a referenced record
    isn't in the read."""

    if not join:
        return records
    by_ref: dict[str, dict[str, Any]] = {}
    for r in records:
        kind, rid = r.get("resourceType"), r.get("id")
        if kind and rid is not None:
            by_ref[f"{kind}/{rid}"] = r
    out = []
    for r in records:
        r = dict(r)
        for name, rule in join.items():
            ref = r.get(rule.via)
            if ref is None:
                continue  # a record that references nothing (the joined one)
            target = by_ref.get(str(ref))
            if target is None:
                return None
            r[name] = target.get(rule.field)
        out.append(r)
    return out


def _scope_of(
    leg: Leg, substrate: str, tier: Any, read_at: Optional[str], unread: bool
) -> Scope:
    path = _PATHS.get(substrate, substrate)
    return Scope(
        read_paths=() if unread else (path,),
        stores=() if unread else (substrate,),
        unread=(substrate,) if unread else (),
        window=Window(
            baseline_read_at=None, read_at=read_at, max_execution_window_s=None
        ),
        strength="unchecked" if unread else _STRENGTH_BY_TIER.get(tier, "unchecked"),
        not_covered=NOT_COVERED,
    )


def _check_leg(
    name: str,
    leg: Leg,
    params: dict[str, Any],
    now: datetime,
    breaks: list[Break],
    anchor: bool,
) -> LegResult:
    result = LegResult(name=name, role=leg.role, outcome=Outcome.UNCERTAIN)
    try:
        contract = leg.contract(name)
    except VerifierError as exc:
        raise VerifierError(f"leg {name}: {exc}") from exc
    result.contract_digest = contract.digest

    if leg.when is not None:
        value = params.get(leg.when.param)
        if value is None:
            result.reasons.append(
                f"checking it depends on {leg.when.param}, which no earlier leg gave"
            )
            return result
        if str(value) not in leg.when.values:
            result.outcome = Outcome.CONFIRMED
            result.skipped = True
            result.reasons.append(f"not due: {leg.when.param} is not one of the values")
            return result

    missing = [p for p in contract.params if params.get(p) is None]
    from_legs = [p for p in missing if "." in p]
    from_caller = [p for p in missing if "." not in p]
    if from_caller:
        raise VerifierError(
            f"leg {name} needs parameter(s) the caller didn't give: "
            + ", ".join(from_caller)
        )
    if from_legs:
        result.reasons.append(
            "depends on values no earlier leg gave: " + ", ".join(sorted(from_legs))
        )
        return result

    substrate = contract.config.verifier.kind
    try:
        verifier = build_effect_verifier(contract.config.verifier, params)
        state = verifier.capture_pre_state()
    except Exception as exc:  # noqa: BLE001 - a leg that can't be read is UNCERTAIN
        result.reasons.append(f"the store could not be read: {type(exc).__name__}")
        result.scope = _scope_of(leg, substrate, None, None, unread=True)
        return result
    substrate = getattr(verifier, "substrate", substrate) or substrate
    read_at = _now_iso(datetime.now(timezone.utc))
    tier = getattr(verifier, "verification_tier", None)
    records = _joined(state.records, leg.join) if state.reachable else None
    if records is None:
        problem = state.detail.get("read_problem") or (
            "a joined record is not in the read" if state.reachable else "unreadable"
        )
        result.reasons.append(f"the store could not be read: {problem}")
        result.scope = _scope_of(leg, substrate, tier, read_at, unread=True)
        return result
    result.scope = _scope_of(leg, substrate, tier, read_at, unread=False)

    empty = EffectState(substrate=substrate, reachable=True, records=[])
    verdicts: list[tuple[str, EffectVerdict]] = []
    for effect in contract.config.effects:
        verdicts.append(
            (
                effect.probe or "",
                judge_records(
                    effect.resolve(params), empty, records, substrate=substrate
                ),
            )
        )

    due_at: Optional[datetime] = None
    overdue = False
    if leg.due is not None:
        start = _when(params.get(leg.due.after))
        if start is not None:
            due_at = start + timedelta(seconds=leg.due.within_s)
            overdue = now >= due_at
    due_text = _now_iso(due_at) if due_at else None
    lag_kind: BreakKind = (
        "not_written_back" if leg.role == "writeback" else "not_received"
    )
    outcomes: list[Outcome] = []

    presence = verdicts[0][1]
    matched = presence.matched_records
    result.records = _refs(verifier, matched)
    count = presence.observed_count
    if presence.verdict is Verdict.INDETERMINATE:
        result.checked["presence"] = "not_checked"
        result.reasons.append(presence.reason)
        outcomes.append(Outcome.UNCERTAIN)
    elif count == 0:
        result.checked["presence"] = "refuted"
        if anchor:
            result.reasons.append("the EHR has no record of this unit of work")
            outcomes.append(Outcome.UNCERTAIN)
        else:
            outcome = Outcome.REFUTED if overdue else Outcome.UNCERTAIN
            why = (
                "no record of this unit, past its due time"
                if overdue
                else "no record of this unit yet; its due time has not passed"
                if due_at
                else "no record of this unit, and no due time could be set"
            )
            breaks.append(
                Break(
                    lag_kind,
                    name,
                    leg.role,
                    outcome,
                    observed_count=0,
                    due_at=due_text,
                    reason=why,
                )
            )
            result.reasons.append(why)
            outcomes.append(outcome)
    elif count is not None and count > 1:
        result.checked["presence"] = "refuted"
        why = f"{count} records match this unit of work's identity"
        breaks.append(
            Break(
                "duplicate",
                name,
                leg.role,
                Outcome.REFUTED,
                records=result.records,
                observed_count=count,
                reason=why,
            )
        )
        result.reasons.append(why)
        outcomes.append(Outcome.REFUTED)
    else:
        result.checked["presence"] = "held"
        record = matched[0]
        for key, source in leg.provides.items():
            params[f"{name}.{key}"] = record.get(source)

    one = result.checked.get("presence") == "held"
    differing: list[str] = []
    for probe, verdict in verdicts[1:]:
        if probe == "duplicates":
            if verdict.verdict is Verdict.INDETERMINATE:
                result.checked["duplicates"] = "not_checked"
                outcomes.append(Outcome.UNCERTAIN)
            elif (verdict.observed_count or 0) > 1:
                result.checked["duplicates"] = "refuted"
                if not any(b.kind == "duplicate" and b.leg == name for b in breaks):
                    why = (
                        f"{verdict.observed_count} submissions share this unit's "
                        "member, service and date"
                    )
                    breaks.append(
                        Break(
                            "duplicate",
                            name,
                            leg.role,
                            Outcome.REFUTED,
                            records=_refs(verifier, verdict.matched_records),
                            observed_count=verdict.observed_count,
                            reason=why,
                        )
                    )
                    result.reasons.append(why)
                outcomes.append(Outcome.REFUTED)
            else:
                result.checked["duplicates"] = "held"
            continue
        if not one:
            result.checked[probe] = "not_checked"
            continue
        if verdict.verdict is Verdict.CONFIRMED:
            result.checked[probe] = "held"
        elif verdict.verdict is Verdict.REFUTED:
            result.checked[probe] = "refuted"
            differing.append(probe.split(":", 1)[1])
        else:
            result.checked[probe] = "not_checked"
            result.reasons.append(verdict.reason)
            outcomes.append(Outcome.UNCERTAIN)
    if differing:
        kind: BreakKind = "not_written_back" if leg.role == "writeback" else "mismatch"
        # A write-back can lag the decision: inside its due time a stale copy
        # may still be updated.
        outcome = (
            Outcome.UNCERTAIN
            if leg.role == "writeback" and not overdue
            else Outcome.REFUTED
        )
        why = "the record differs in " + ", ".join(differing)
        breaks.append(
            Break(
                kind,
                name,
                leg.role,
                outcome,
                fields=tuple(differing),
                records=result.records,
                observed_count=1,
                due_at=due_text if leg.role == "writeback" else None,
                reason=why,
            )
        )
        result.reasons.append(why)
        outcomes.append(outcome)
    if anchor and one:
        unset = [k for k in leg.provides if params.get(f"{name}.{k}") is None]
        if unset:
            result.reasons.append("the EHR record has no value for " + ", ".join(unset))
    result.outcome = max(outcomes or [Outcome.CONFIRMED], key=_RANK.__getitem__)
    return result


def reconcile(
    spec: ReconcileSpec,
    params: Mapping[str, object],
    *,
    now: Optional[datetime] = None,
) -> Reconciliation:
    """Read every leg and judge whether the systems agree on the unit of
    work ``params`` names. Never raises for a failed read or check; raises
    :class:`~openadapt_flow.verifier.VerifierError` when the caller left out
    a parameter the spec needs or a leg's contract is invalid."""

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise VerifierError("now must carry a time zone")
    bad = [k for k in params if "." in k]
    if bad:
        raise VerifierError("caller parameters may not contain '.': " + ", ".join(bad))
    bound: dict[str, Any] = {k: v for k, v in params.items() if v is not None}
    breaks: list[Break] = []
    legs: list[LegResult] = []
    for name, leg in spec.legs.items():
        legs.append(
            _check_leg(name, leg, bound, now, breaks, anchor=name == spec.anchor)
        )

    checked = [leg for leg in legs if not leg.skipped]
    outcome = max((leg.outcome for leg in checked), key=_RANK.__getitem__)
    reasons: list[str] = []
    if legs[0].outcome is not Outcome.CONFIRMED and outcome is Outcome.CONFIRMED:
        outcome = Outcome.UNCERTAIN  # never agree without the anchor
    if len(checked) < 2 and outcome is Outcome.CONFIRMED:
        outcome = Outcome.UNCERTAIN
        reasons.append("only one leg was checked: nothing to reconcile against")
    for leg in legs:
        reasons.extend(f"{leg.name}: {r}" for r in leg.reasons)

    scopes = [leg.scope for leg in legs if leg.scope is not None]
    paths: list[str] = []
    stores: list[str] = []
    unread: list[str] = []
    for leg in legs:
        if leg.scope is None:
            if not leg.skipped:
                unread.append(leg.name)
            continue
        for p in leg.scope.read_paths:
            if p not in paths:
                paths.append(p)
        (unread if leg.scope.unread else stores).append(leg.name)
    strengths = [s.strength for s in scopes if s.strength != "unchecked"]
    strength = (
        min(strengths, key=lambda s: _STRENGTH_RANK.get(s, 0))
        if strengths and not unread
        else "unchecked"
    )
    times = [s.window.read_at for s in scopes if s.window.read_at]
    scope = Scope(
        read_paths=tuple(paths),
        stores=tuple(stores),
        unread=tuple(unread),
        window=Window(
            baseline_read_at=None,
            read_at=max(times) if times else None,
            max_execution_window_s=None,
        ),
        strength=strength,
        not_covered=NOT_COVERED,
    )
    return Reconciliation(
        unit=spec.unit,
        outcome=outcome,
        breaks=breaks,
        legs=legs,
        scope=scope,
        spec_digest=spec.digest,
        reasons=reasons,
    )
