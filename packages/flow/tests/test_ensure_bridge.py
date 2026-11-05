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
