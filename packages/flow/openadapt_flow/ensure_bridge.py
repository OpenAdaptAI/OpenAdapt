"""Legacy Ensure calls use the canonical verifier, with one native collector.

The adapter preserves Ensure's pure field/operation judgment. Pending authority
and native observations remain in memory; serialized snapshots are data only.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, SupportsIndex

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.ensure_record import (
    Baseline,
    EnsureContract,
    EnsureObservation,
    EnsureResult,
    FhirReader,
    Unreadable,
    _capture_ensure,
    _collect_ensure,
    extra_elements,
    is_person,
    wrong_fields,
)
from openadapt_flow.fhir_reference import ReferenceEvidence
from openadapt_flow.runtime.effects.adapter import (
    set_verifier_identity,
    verifier_identity,
)
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier
from openadapt_flow.runtime.effects.frame import FrameVerdict

DETAIL = "openadapt:ensure_record"
_DEFAULT_LEDGER = object()


class EnsureBinding(BaseModel):
    """The full original contract and the names supplied before dispatch."""

    model_config = ConfigDict(extra="forbid")
    contract: EnsureContract
    parameters: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _names(self) -> "EnsureBinding":
        if self.parameters != sorted(set(self.parameters)):
            raise ValueError("ensure parameter names must be unique and sorted")
        return self


class EnsureContext:
    """Explicit per-call native transport; never a contract or an artifact."""

    __slots__ = ("reader", "sleep", "baseline", "observation")

    def __init__(self, reader: Optional[FhirReader] = None) -> None:
        self.reader = reader
        self.sleep: Callable[[float], None] = time.sleep
        self.baseline: Optional[Baseline] = None
        self.observation: Optional[EnsureObservation] = None

    def __reduce_ex__(self, protocol: SupportsIndex) -> Any:
        raise TypeError("ensure runtime context cannot be serialized")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _snapshot(baseline: Baseline) -> str:
    return _hash(asdict(baseline))


class _EnsureAuthority:
    __slots__ = (
        "owner",
        "pending",
        "context",
        "snapshot",
        "contract",
        "params",
        "result",
    )

    def __init__(self, owner: Baseline, pending: Any, context: EnsureContext) -> None:
        self.owner, self.pending, self.context = owner, pending, context
        self.snapshot = _snapshot(owner)
        self.contract = pending.contract.digest
        self.params = _hash(dict(pending.params))
        self.result: Any = None

    def __reduce_ex__(self, protocol: SupportsIndex) -> Any:
        raise TypeError("ensure pending authority cannot be serialized")


def _unknown(reason: str) -> EnsureResult:
    from openadapt_flow.frame_invariants import judge_frame_invariants

    hard = judge_frame_invariants(FrameVerdict("not_checked", reason)).evidence()
    return EnsureResult(
        "needs_review",
        None,
        [reason],
        {
            "invariants_unknown": True,
            "reason": reason,
            "reconciliation": {"status": "owed", "reason": reason},
            "hard_invariants": hard,
        },
    )


def _invalidate(authority: _EnsureAuthority, reason: str) -> EnsureResult:
    # A failed live binding cannot later be repaired into the same attempt.
    # Releasing a canonical lease leaves its ledger attempt open for review.
    authority.pending.used = True
    authority.pending.release()
    return _unknown(reason)


def begin_ensure(
    contract: EnsureContract,
    params: Mapping[str, Any],
    *,
    reader: Optional[FhirReader] = None,
    actor: Any = None,
    ledger: Any = _DEFAULT_LEDGER,
    workdir: Optional[Path] = None,
    targeted: Any = None,
) -> Baseline:
    from openadapt_flow.verifier import Actor, Contract, _begin_write

    context = EnsureContext(reader)
    bound = Contract.from_ensure(contract, params, targeted=targeted)
    pending = _begin_write(
        bound,
        params,
        actor=actor or Actor("agent", "ensure-record"),
        workdir=workdir,
        _ensure_context=context,
        **({} if ledger is _DEFAULT_LEDGER else {"ledger": ledger}),
    )
    baseline = context.baseline
    if baseline is None:
        pending.release()
        raise Unreadable("independent ensure baseline is unavailable")
    baseline._pending_authority = _EnsureAuthority(baseline, pending, context)
    return baseline


def canonical_result(baseline: Baseline) -> Any:
    """Private in-memory handle; no serialized result can recover authority."""
    authority = getattr(baseline, "_pending_authority", None)
    return authority.result if isinstance(authority, _EnsureAuthority) else None


def canonical_contract(baseline: Baseline) -> Any:
    authority = getattr(baseline, "_pending_authority", None)
    return (
        authority.pending.contract if isinstance(authority, _EnsureAuthority) else None
    )


def canonical_observation(baseline: Baseline) -> Optional[EnsureObservation]:
    authority = getattr(baseline, "_pending_authority", None)
    return (
        authority.context.observation
        if isinstance(authority, _EnsureAuthority)
        else None
    )


def finish_ensure(
    contract: EnsureContract,
    params: Mapping[str, Any],
    baseline: Baseline,
    *,
    reader: Optional[FhirReader] = None,
    sleep: Callable[[float], None] = time.sleep,
    declared: Collection[str] = (),
) -> EnsureResult:
    from openadapt_flow.verifier import Contract, Outcome, _verify_write

    authority = getattr(baseline, "_pending_authority", None)
    if not isinstance(authority, _EnsureAuthority) or authority.owner is not baseline:
        return _unknown("ensure_baseline_authority_missing")
    if declared:
        return _invalidate(authority, "independent_declared_write_binding_unknown")
    pending = authority.pending
    rebuilt = Contract.from_ensure(
        contract,
        params,
        targeted=getattr(pending.contract.config.frame, "targeted", None),
    )
    if (
        pending.used
        or authority.snapshot != _snapshot(baseline)
        or authority.contract != rebuilt.digest
        or authority.contract != pending.contract.digest
        or authority.params != _hash(dict(params))
        or authority.params != _hash(dict(pending.params))
        or (reader is not None and reader is not authority.context.reader)
    ):
        return _invalidate(authority, "ensure_baseline_binding_invalid")
    authority.context.sleep = sleep
    result = _verify_write(pending, seal=False, _ensure_context=authority.context)
    authority.result = result
    observation = authority.context.observation
    original = (
        observation.result
        if observation is not None
        else _unknown("ensure_readback_unknown")
    )
    evidence = {
        **original.evidence,
        "canonical": result.to_dict(),
        "hard_invariants": (result.evidence.get("frame") or {}).get(
            "hard_invariants", {}
        ),
        "reconciliation": {
            "status": "owed" if not getattr(result, "final", False) else "complete"
        },
    }
    if result.outcome is Outcome.CONFIRMED and original.outcome != "needs_review":
        return EnsureResult(
            original.outcome, original.record_id, list(result.reasons), evidence
        )
    key = (
        "invariants_refuted"
        if result.outcome is Outcome.REFUTED
        else "invariants_unknown"
    )
    evidence[key] = True
    return EnsureResult(
        "needs_review", None, list(result.reasons) or list(original.reasons), evidence
    )


class EnsureEffectVerifier(FhirEffectVerifier):
    """Adapt the existing Ensure reader/judge to the canonical effect protocol."""

    def __init__(
        self,
        native: FhirEffectVerifier,
        binding: EnsureBinding,
        params: Mapping[str, object],
        context: Optional[EnsureContext] = None,
    ) -> None:
        if (
            native.base_url.rstrip("/") != binding.contract.base_url.rstrip("/")
            or native.resource_type != binding.contract.resource_type
            or native.drop_fields
            or native.cache_bound_s != binding.contract.cache_bound_s
        ):
            raise ValueError("ensure adapter and native read name different sources")
        super().__init__(
            native.base_url,
            resource_type=native.resource_type,
            field_paths=native.field_paths,
            search_params=native.search_params,
            access_token=native.access_token,
            token_source=native.token_source,
            session=native._session,
            timeout_s=native.timeout_s,
            verify_tls=native.verify_tls,
            poll_interval_s=native.poll_interval_s,
            read_headers=native._read_headers,
            cache_bound_s=native.cache_bound_s,
        )
        self._verify_per_request = native._verify_per_request
        self.binding, self.params = binding, dict(params)
        self.context = context or EnsureContext()
        from openadapt_flow.runtime.effects.targeted import _transport

        if self.context.reader is not None and (
            self.context.reader.base_url.rstrip("/") != self.base_url
            or self.context.reader._token_env != binding.contract.access_token_env
            or self.context.reader.cache_bound_s != self.cache_bound_s
        ):
            raise ValueError(
                "ensure runtime reader names another source or credential reference"
            )
        self.reader = self.context.reader or FhirReader(
            binding.contract, get=lambda url, query: _transport(self, url, query)
        )
        set_verifier_identity(
            self,
            "sha256:"
            + _hash(
                {
                    "native": verifier_identity(native),
                    "ensure_record": binding.model_dump(mode="json", by_alias=True),
                }
            ),
        )

    def _flatten(self, resource: dict[str, Any]) -> dict[str, Any]:
        # Never replace a stored value with the expected value. The native
        # body is sealed as cells and stays in memory while judging.
        return dict(resource)

    def _targeted_reference_evidence(
        self,
        resources: Collection[Mapping[str, Any]],
        params: Mapping[str, object],
    ) -> ReferenceEvidence:
        if dict(params) != self.params:
            raise Unreadable("ensure targeted parameter binding changed")
        return self.reader.reference_evidence(
            self.binding.contract, self.params, resources
        )

    def _matches_targeted_resource(
        self,
        resource: Mapping[str, Any],
        params: Mapping[str, object],
        references: ReferenceEvidence,
    ) -> bool:
        """Apply the original field predicates to already collected evidence."""
        if dict(params) != self.params:
            raise Unreadable("ensure targeted parameter binding changed")
        contract = self.binding.contract
        return (
            is_person(contract, resource, self.params, references)
            and not wrong_fields(contract, resource, self.params, references)
            and (
                not contract.closed
                or not extra_elements(contract, resource, self.params)
            )
        )

    def capture_pre_state(self, context: Any = None) -> EffectState:
        try:
            baseline = _capture_ensure(self.binding.contract, self.params, self.reader)
            if self.context.baseline is None:
                self.context.baseline = baseline
            return EffectState(
                substrate=self.substrate,
                reachable=True,
                records=list(baseline.matches.values()),
                detail={
                    DETAIL: {
                        "started": baseline.started.isoformat(),
                        "candidates": baseline.candidates,
                    }
                },
            )
        except Unreadable:
            return EffectState(substrate=self.substrate, reachable=False)

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        try:
            detail = before.detail[DETAIL]
            if not before.reachable:
                raise Unreadable("ensure baseline is unreadable")
            matches = {}
            for record in before.records:
                rid = record.get("id")
                if not isinstance(rid, str) or not rid or rid.startswith("\x00"):
                    raise Unreadable(
                        "an existing ensure identity cannot be reconstructed"
                    )
                matches[rid] = record
            baseline = Baseline(
                datetime.fromisoformat(detail["started"]), matches, detail["candidates"]
            )
            observation = _collect_ensure(
                self.binding.contract,
                self.params,
                baseline,
                reader=self.reader,
                sleep=self.context.sleep,
            )
            self.context.observation = observation
            result = observation.result
            records = [
                dict(r)
                for i, r in observation.after_matches.items()
                if i == result.record_id
            ]
            confirmed = result.outcome in {"created_verified", "found_existing"}
            return EffectVerdict(
                verdict=Verdict.CONFIRMED
                if confirmed
                else Verdict.REFUTED
                if result.evidence.get("agent_writes")
                else Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason="; ".join(result.reasons),
                observed_count=(
                    len(observation.after_matches)
                    if confirmed
                    else max(
                        len(observation.after_matches),
                        int(result.evidence.get("agent_writes") or 0),
                    )
                ),
                expected_count=1,
                matched_records=records if confirmed else [],
            )
        except (Unreadable, KeyError, TypeError, ValueError):
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason="ensure_readback_unknown",
                unavailable=True,
            )

    def capture_post_state(self, context: Any = None) -> EffectState:
        observed = self.context.observation
        return EffectState(
            substrate=self.substrate,
            reachable=observed is not None,
            records=[]
            if observed is None
            else [dict(r) for r in observed.after_matches.values()],
        )

    def declared_ids(self) -> set[str]:
        observed = self.context.observation
        return (
            {observed.result.record_id}
            if (
                observed is not None
                and observed.result.outcome in {"created_verified", "found_existing"}
                and observed.result.record_id is not None
            )
            else set()
        )

    def frame(self, generic: FrameVerdict) -> FrameVerdict:
        from openadapt_flow.ensure_invariants import enforce_ensure_invariants

        observed = self.context.observation
        if observed is None:
            return generic
        bounded = enforce_ensure_invariants(
            observed.result,
            self.binding.contract,
            self.params,
            observed.baseline,
            observed.changes,
            observed.after_matches,
        )
        if bounded.evidence.get("invariants_refuted"):
            generic.status = "refuted"
            generic.reason = "; ".join(bounded.reasons)
        # Preserve adjudicated scope/delete refutations when the actual
        # targeted observation is applied later. Missing proof stays UNKNOWN.
        generic.hard_invariants = _known_hard(bounded.evidence.get("hard_invariants"))
        return generic


def _known_hard(evidence: Any) -> Any:
    """Retain only the fixed judge's typed checks, never caller declarations."""
    from openadapt_flow.hard_invariants import HardInvariantVerdict, InvariantResult

    if not isinstance(evidence, Mapping):
        return None
    return HardInvariantVerdict(
        tuple(InvariantResult(**row) for row in evidence["checks"])
    )
