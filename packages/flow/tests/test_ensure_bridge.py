"""Canonical Ensure authority, unchanged field rules and data-only snapshots.

Native producer interaction tests use the shared targeted HTTP fixture after
its qualified dependency is integrated. These controls do not qualify an app.
"""

from __future__ import annotations

import copy
import json
import pickle
from dataclasses import asdict, replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow import verifier as api
from openadapt_flow.ensure_bridge import EnsureContext, _EnsureAuthority, finish_ensure
from openadapt_flow.ensure_record import Baseline, EnsureContract, verify_ensure
from openadapt_flow.shadow import Episode, Shadow, UNCERTAIN

PARAMS = {"name": "synthetic-bound-value"}
T0 = datetime(2026, 11, 5, tzinfo=timezone.utc)


def ensure(**over: Any) -> EnsureContract:
    return EnsureContract.model_validate({
        "base_url": "http://fhir.example.test/r4", "resource_type": "Basic",
        "self": ["Practitioner/synthetic-run"], "fields": {"name": {"path": "name"}},
        "identity": [["name"]], "search": {"name": "name"}, **over,
    })


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("refusal must precede canonical verification or native I/O")


class PrivateMarker:
    def __repr__(self) -> str:
        return "private-pending-canary"

    def __reduce_ex__(self, protocol: int) -> Any:
        raise AssertionError("a data snapshot must never serialize pending authority")


def test_pending_attachment_is_absent_from_all_data_fields_and_repr() -> None:
    baseline = Baseline(T0, {})
    baseline._pending_authority = PrivateMarker()
    assert set(asdict(baseline)) == {"started", "matches", "candidates"}
    assert set(vars(baseline)) == {"started", "matches", "candidates"}
    assert "private-pending-canary" not in repr(baseline)
    assert "private-pending-canary" not in json.dumps(asdict(baseline), default=str)


@pytest.mark.parametrize("copy_data", [copy.copy, copy.deepcopy,
    lambda value: pickle.loads(pickle.dumps(value)), lambda value: replace(value)])
def test_serialized_or_copied_snapshot_loses_authority(copy_data: Any, monkeypatch: Any) -> None:
    baseline = Baseline(T0, {})
    baseline._pending_authority = PrivateMarker()
    restored = copy_data(baseline)
    monkeypatch.setattr(api, "verify_write", forbidden)
    result = verify_ensure(ensure(), PARAMS, restored)
    assert result.outcome == "needs_review" and result.record_id is None
    assert result.reasons == ["ensure_baseline_authority_missing"]
    assert result.evidence["reconciliation"]["status"] == "owed"


def test_handbuilt_baseline_never_authorizes_readback(monkeypatch: Any) -> None:
    monkeypatch.setattr(api, "verify_write", forbidden)
    result = verify_ensure(ensure(), PARAMS, Baseline(T0, {}))
    assert result.outcome == "needs_review"
    assert result.evidence["invariants_unknown"] is True


@pytest.mark.parametrize("mutation", ["contract", "params", "snapshot", "used", "reader", "declared"])
def test_live_baseline_binding_refuses_changes_before_canonical_read(mutation: str, monkeypatch: Any) -> None:
    contract = ensure()
    baseline = Baseline(T0, {})
    pending = SimpleNamespace(contract=api.Contract.from_ensure(contract, PARAMS),
                              params=dict(PARAMS), used=False, release=lambda: None)
    context = EnsureContext()
    baseline._pending_authority = _EnsureAuthority(baseline, pending, context)
    monkeypatch.setattr(api, "verify_write", forbidden)
    params, reader, declared = dict(PARAMS), None, ()
    if mutation == "contract":
        contract = ensure(closed=True)
    elif mutation == "params":
        params["name"] = "different-synthetic-value"
    elif mutation == "snapshot":
        baseline.matches["fabricated"] = {"id": "fabricated"}
    elif mutation == "used":
        pending.used = True
    elif mutation == "reader":
        reader = object()
    else:
        declared = ("Basic/reply-only-id",)
    result = finish_ensure(contract, params, baseline, reader=reader, declared=declared)
    assert result.outcome == "needs_review" and result.record_id is None
    assert pending.used is True
    assert result.evidence["reconciliation"]["status"] == "owed"
    assert result.reasons == ["independent_declared_write_binding_unknown"
                              if mutation == "declared" else "ensure_baseline_binding_invalid"]


@pytest.mark.parametrize("update", [
    {"closed": True}, {"allow": ["active=true"]},
    {"fields": {"name": {"path": "name", "compare": "casefold"}}},
    {"operations": {"cardinality": {"own_transient": True}}},
])
def test_full_ensure_rules_bind_contract_and_ledger_identity(update: Any) -> None:
    original = api.Contract.from_ensure(ensure(), PARAMS)
    changed = api.Contract.from_ensure(ensure(**update), PARAMS)
    assert original.digest != changed.digest
    assert original.identity != changed.identity
    assert original.shape != changed.shape


def test_structured_reference_input_stays_typed_and_is_not_a_fake_scalar_effect() -> None:
    contract = api.Contract.from_ensure(ensure(), {"name": {"reference": "Basic/synthetic"}})
    assert contract.params == ["name"]
    assert contract.config.effects[0].match == {}
    assert contract.config.ensure_record.contract == ensure()


def test_old_contract_keeps_fixed_digest_and_omits_new_metadata() -> None:
    contract = api.Contract.from_dict({
        "verifier": {"kind": "fhir", "base_url": "https://fhir.local/r4",
                     "resource_type": "Observation", "field_paths": {
                         "id": "id", "email": "email", "lname": "lname", "fname": "fname"}},
        "record": {"name": "patient", "match": ["email", "lname"],
                   "fields": {"fname": "fname", "lname": "lname", "email": "email"}},
        "frame": {"identity_field": "id"},
    })
    assert contract.digest == "91c4a3ebe007fb29ff109ab420e004c69d18344776d91b833275cafa0764fd20"
    dumped = contract.config.model_dump(mode="json")
    assert "ensure_record" not in dumped
    assert api.Contract.from_dict(dumped).digest == contract.digest


def test_runtime_context_itself_cannot_be_pickled() -> None:
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(EnsureContext())


def test_unknown_shadow_episode_retains_non_executable_owed_state() -> None:
    shadow = object.__new__(Shadow)
    shadow.recheck_s = 1.0
    ep = Episode(1, "none", "unknown", checked=True, checked_at=10.0, verdict=UNCERTAIN)
    shadow._schedule_recheck(ep)
    assert ep.recheck_at == 11.0
    obligation = ep.evidence["reconciliation"]
    assert obligation == {"status": "owed", "purpose": "read_only_reconciliation",
                          "reason": "independent_target_identity_unknown", "executable": False}
    shadow._recheck(ep)
    assert ep.late_verdict == UNCERTAIN
    assert ep.late_reasons == ["independent_target_identity_unknown"]
    assert obligation["status"] == "owed"


def test_initial_refuted_shadow_attempt_never_enters_native_recheck(monkeypatch: Any) -> None:
    shadow = object.__new__(Shadow)
    shadow.recheck_s = 1.0
    ep = Episode(1, "none", "refuted", checked=True, checked_at=10.0, verdict="REFUTED")
    monkeypatch.setattr(api, "recheck", forbidden)
    shadow._schedule_recheck(ep)
    assert ep.recheck_at == 0
    shadow._recheck(ep)
    assert ep.late_verdict == "REFUTED"


def test_known_wrong_write_is_conflicting_even_when_identity_search_is_empty(monkeypatch: Any) -> None:
    from openadapt_flow import ensure_bridge
    from openadapt_flow.ensure_bridge import EnsureBinding, EnsureEffectVerifier
    from openadapt_flow.ensure_record import EnsureObservation, EnsureResult
    from openadapt_flow.runtime.effects.effect import Effect, EffectKind, EffectState

    baseline = Baseline(T0, {})
    wrong = {"id": "independent-wrong-record", "name": "wrong"}
    observation = EnsureObservation(EnsureResult("needs_review", None,
        ["wrong declared value"], {"agent_writes": 1}), baseline, {}, {}, wrong, None)
    monkeypatch.setattr(ensure_bridge, "_collect_ensure", lambda *a, **kw: observation)
    adapter = object.__new__(EnsureEffectVerifier)
    adapter.binding = EnsureBinding(contract=ensure(), parameters=["name"])
    adapter.params, adapter.reader, adapter.context = dict(PARAMS), None, EnsureContext()
    state = EffectState(substrate="fhir", reachable=True,
                        detail={ensure_bridge.DETAIL: {"started": T0.isoformat(), "candidates": 0}})
    result = adapter.verify(Effect(kind=EffectKind.RECORD_WRITTEN), state)
    assert result.observed_count == 1
    assert result.observed_effect == "conflicting"
    assert not result.confirmed


@pytest.mark.parametrize("explicit_none", [False, True])
def test_direct_ensure_uses_canonical_ledger_default_unless_explicitly_disabled(monkeypatch: Any, explicit_none: bool) -> None:
    from openadapt_flow.ensure_bridge import begin_ensure

    seen = {}
    def begin(contract: Any, params: Any, **options: Any) -> Any:
        seen.update(options)
        options["_ensure_context"].baseline = Baseline(T0, {})
        return SimpleNamespace(contract=contract, params=dict(params))
    monkeypatch.setattr(api, "begin_write", begin)
    begin_ensure(ensure(), PARAMS, **({"ledger": None} if explicit_none else {}))
    assert ("ledger" in seen) is explicit_none
    if explicit_none:
        assert seen["ledger"] is None


def owed_document() -> tuple[dict[str, Any], dict[str, Any]]:
    from openadapt_flow.verify_run import SALT_SCHEME, sha256_tagged
    from openadapt_flow.runtime.effects import late_write

    at = "2026-11-05T00:00:00Z"
    mac = "hmac-sha256:" + "a" * 64
    evidence = {
        "effect_verdict": "INDETERMINATE", "checked": {
            "delta": "not_checked", "frame": "not_checked", "source": "instruction"},
        "executor": {"kind": "agent", "run_record_sha256": mac},
        "readback": {"verification_tier": "unchecked", "checked_how": "unchecked",
                     "baseline_sha256": None, "baseline_read_at": None, "read_at": at},
        "authorization": {"reference_sha256": mac, "self_approved": None},
        "salting": {"scheme": SALT_SCHEME, "salt_commitment": "sha256:" + "b" * 64},
        "frame": {"status": "not_checked", "recheck": late_write.schedule(
            {"read_at": at}, 1.0, mac, due=True)},
        "reconciliation": {"status": "owed", "purpose": "read_only_reconciliation",
                           "reason": "independent_target_identity_unknown", "executable": False},
    }
    receipt = {"outcome": "reconciliation_required", "delivery_uncertain": True,
               "effect_verdict": "INDETERMINATE", "executor_kind": "agent",
               "verification_tier": "unchecked", "checked_how": "unchecked",
               "checked": dict(evidence["checked"]), "issued_at": at,
               "evidence_digest": sha256_tagged(evidence)}
    return evidence, receipt


def test_nonverified_owed_deadline_is_evidence_only_not_a_positive_receipt() -> None:
    from openadapt_flow.verify_run import evidence_problem

    evidence, receipt = owed_document()
    assert "recheck_due_at" not in receipt
    assert evidence_problem(evidence, receipt) is None
    assert api.classify(receipt["outcome"], receipt["checked"], evidence)[0] is api.Outcome.UNCERTAIN


@pytest.mark.parametrize("fault", ["verified", "refuted", "extra", "reason", "executable", "delivery", "deadline", "schedule_extra"])
def test_owed_shape_cannot_bypass_positive_or_binding_checks(fault: str) -> None:
    from openadapt_flow.verify_run import evidence_problem, sha256_tagged

    evidence, receipt = owed_document()
    if fault == "verified":
        receipt["outcome"] = "verified"
    elif fault == "refuted":
        receipt["checked"]["frame"] = "refuted"
        evidence["checked"]["frame"] = "refuted"
        evidence["frame"]["status"] = "refuted"
    elif fault == "extra":
        evidence["reconciliation"]["target_id"] = "reply-only-id"
    elif fault == "reason":
        evidence["reconciliation"]["reason"] = "a caller supplied explanation"
    elif fault == "executable":
        evidence["reconciliation"]["executable"] = True
    elif fault == "delivery":
        receipt["delivery_uncertain"] = False
    elif fault == "deadline":
        evidence["frame"]["recheck"]["due_at"] = "2026-11-05T00:00:00Z"
    else:
        evidence["frame"]["recheck"]["caller_complete"] = True
    receipt["evidence_digest"] = sha256_tagged(evidence)
    assert evidence_problem(evidence, receipt) == "evidence_mismatch"


def test_schema_mapping_named_param_does_not_declare_an_extra_runtime_input() -> None:
    contract = api.Contract.from_ensure(ensure(fields={
        "name": {"path": "name", "map": {"param": "a-stored-code"}}}), PARAMS)
    assert contract.params == ["name"]
    assert contract.null_unsafe_params() == {}
