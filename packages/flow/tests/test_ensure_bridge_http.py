"""Joined legacy/Shadow/canonical proof through the existing synthetic HTTP server.

These controls exercise native HTTP collection and issued-state authority.
They do not qualify a real application, its permissions, history or cache.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import pytest

from openadapt_flow import verifier as api
from openadapt_flow.ensure_bridge import canonical_result
from openadapt_flow.ensure_record import EnsureContract, capture_baseline, verify_ensure
from openadapt_flow.fhir_reference import definition_digest
from openadapt_flow.runtime.effects.targeted import FrameTargeted
from openadapt_flow.shadow import Shadow
from tests.test_targeted_recheck import RECORD_ID
from tests.test_targeted_recheck import server as server

ACTOR = api.Actor("agent", "actor/run")
PARAMS = {
    "name": "synthetic-business-canary",
    "patient": "Patient/a",
    "display": "Task presentation",
    "phone": "+1 (555) 010-1234",
}


def plan(base: str) -> tuple[EnsureContract, FrameTargeted, api.Contract]:
    definition = {
        "resourceType": "StructureDefinition",
        "kind": "resource",
        "type": "Basic",
        "url": "http://hl7.org/fhir/StructureDefinition/Basic",
        "snapshot": {
            "element": [
                {"path": "Basic"},
                {"path": "Basic.name", "type": [{"code": "string"}]},
                {
                    "path": "Basic.subject",
                    "type": [
                        {
                            "code": "Reference",
                            "targetProfile": [
                                "http://hl7.org/fhir/StructureDefinition/Patient"
                            ],
                        }
                    ],
                },
                {"path": "Basic.valueString", "type": [{"code": "string"}]},
            ]
        },
    }
    contract = EnsureContract.model_validate(
        {
            "resource_type": "Basic",
            "base_url": base,
            "cache_bound_s": 0,
            "self": [ACTOR.id],
            "closed": True,
            "settle_s": 0,
            "stable_reads": 2,
            "fields": {
                "name": {"path": "name", "compare": "casefold"},
                "patient": {"path": "subject.reference"},
                "display": {"path": "subject.display"},
                "phone": {"path": "valueString", "compare": "digits"},
            },
            "identity": [["name"]],
            "search": {"name": "name"},
            "reference_schema": {
                "definition": definition,
                "sha256": definition_digest(definition),
            },
        }
    )
    targeted = FrameTargeted.model_validate(
        {
            "quiet_s": 0.15,
            "setup_evidence_sha256": "sha256:" + "a" * 64,
            "search": contract,
            "history_retention_qualified": True,
            "author_path": "meta.author.reference",
            "author_server_set": True,
            "self": [{"literal": ACTOR.id}],
            "not_found_qualified": True,
        }
    )
    return (
        contract,
        targeted,
        api.Contract.from_ensure(contract, PARAMS, targeted=targeted),
    )


def body(**over: Any) -> dict[str, Any]:
    return {
        "resourceType": "Basic",
        "name": PARAMS["name"],
        "subject": {"reference": "Patient/a", "display": "Store presentation"},
        "valueString": "15550101234",
        **over,
    }


def post(base: str, document: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        base + "/Basic",
        data=json.dumps(document).encode(),
        headers={"Content-Type": "application/fhir+json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        assert response.status == 201
        return json.load(response)


def prepare(store: Any) -> None:
    store.bridge_mode = True
    store.broad_identifying_search = True
    store.extra_resources = {
        "Patient/a": {"resourceType": "Patient", "id": "a"},
        "Patient/b": {"resourceType": "Patient", "id": "b"},
    }


@pytest.fixture
def begun(server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store, base = server
    prepare(store)
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    monkeypatch.setattr(api, "seconds_until", lambda due: 0)
    contract, targeted, cfg = plan(base)
    ledger = api.Ledger(api.MemoryLedger())
    baseline = capture_baseline(
        contract,
        PARAMS,
        actor=ACTOR,
        targeted=targeted,
        ledger=ledger,
        workdir=tmp_path / "writes",
    )
    return store, base, contract, cfg, baseline, ledger


def finish(begun: Any, **values: Any):
    store, base, contract, cfg, baseline, ledger = begun
    reply = post(base, body(**values))
    result = verify_ensure(contract, PARAMS, baseline, sleep=lambda _: None)
    write = canonical_result(baseline)
    return store, contract, cfg, baseline, result, write, ledger, reply


def recheck(cfg: api.Contract, write: Any, ledger: Any, **options: Any):
    options.setdefault("wait", True)
    return api.recheck(cfg, write, params=PARAMS, actor=ACTOR, ledger=ledger, **options)


def test_legacy_entrypoints_issue_same_canonical_bound_proof(
    begun: Any, tmp_path: Path
):
    store, contract, cfg, baseline, result, write, ledger, _ = finish(begun)
    assert result.outcome == "created_verified", result.reasons
    assert (
        write.outcome is api.Outcome.CONFIRMED and write.provisional and not write.final
    )
    assert write.recheck is not None and write.recheck.target_ids == (
        f"Basic/{RECORD_ID}",
    )
    initial_sha = write.receipt_sha256
    assert write.evidence["frame"]["hard_invariants"]["status"] == "held"
    before = copy.deepcopy((store.current, store.history))
    late = recheck(cfg, write, ledger)
    assert late.outcome == "CONFIRMED" and late.final, late.reasons
    assert late.receipt_sha256 == initial_sha
    assert late.evidence["hard_invariants"]["status"] == "held"
    assert late.evidence["targeted"]["phase"] == "late"
    assert before == (store.current, store.history)
    state_path = write.recheck.save(tmp_path / "state.json")
    serialized = json.dumps(write.to_dict()) + state_path.read_text() + repr(baseline)
    for private in (
        RECORD_ID,
        PARAMS["name"],
        PARAMS["phone"],
        "Patient/a",
        "_pending_authority",
    ):
        assert private not in serialized


def test_reply_id_is_not_the_independently_written_identity(begun: Any):
    begun[0].reply_id_override = "misleading-reply-only-id"
    _, _, _, _, result, write, _, reply = finish(begun)
    assert reply["id"] == "misleading-reply-only-id"
    assert result.outcome == "created_verified", result.reasons
    assert result.record_id == RECORD_ID
    assert write.recheck.target_ids == (f"Basic/{RECORD_ID}",)


@pytest.mark.parametrize(
    "changed",
    [
        {
            "subject": {
                "reference": "Patient/a",
                "display": "New nonidentity presentation",
            }
        },
        {"name": PARAMS["name"].upper(), "valueString": "+1-555-010-1234"},
    ],
)
def test_late_reference_display_and_declared_scalar_equivalence_preserve_rules(
    begun: Any, changed: Any
):
    store, _, cfg, _, result, write, ledger, _ = finish(begun)
    assert result.outcome == "created_verified", result.reasons
    document = body(**changed)
    store.save(
        name=document.pop("name"),
        fields={k: v for k, v in document.items() if k != "resourceType"},
    )
    late = recheck(cfg, write, ledger)
    assert late.outcome == "CONFIRMED" and late.final, late.reasons
    assert late.evidence["targeted"]["target_mismatches"] == 0


@pytest.mark.parametrize(
    "changed",
    [
        {"subject": {"reference": "Patient/b", "display": PARAMS["display"]}},
        {"valueString": "15550109999"},
        {"name": "contrary-business-key"},
        {"undeclared": "extra-closed-value"},
    ],
)
def test_late_known_wrong_value_or_extra_closed_field_refutes(begun: Any, changed: Any):
    store, _, cfg, _, result, write, ledger, _ = finish(begun)
    assert result.outcome == "created_verified", result.reasons
    document = body(**changed)
    store.save(
        name=document.pop("name"),
        fields={k: v for k, v in document.items() if k != "resourceType"},
    )
    late = recheck(cfg, write, ledger)
    assert late.outcome == "REFUTED" and late.final, late.reasons
    assert late.evidence["targeted"]["target_mismatches"] > 0
    assert ledger.latest(write.write_key).state == "held"


def test_unknown_reference_resolution_is_unknown_even_with_trusted_own_actor(
    begun: Any,
):
    store, _, cfg, _, result, write, ledger, _ = finish(begun)
    assert result.outcome == "created_verified", result.reasons
    store.save(
        name=PARAMS["name"],
        fields={"subject": body()["subject"], "valueString": body()["valueString"]},
    )
    del store.extra_resources["Patient/a"]
    late = recheck(cfg, write, ledger)
    assert late.outcome == "UNCERTAIN" and not late.verified and not late.final
    assert "target_value_unknown" in late.evidence["targeted"]["problems"]
    assert late.evidence["targeted"]["late_search_status"] == "UNKNOWN"


def test_same_issued_version_cannot_change_body_under_semantic_equivalence(begun: Any):
    store, _, cfg, _, result, write, ledger, _ = finish(begun)
    assert result.outcome == "created_verified", result.reasons
    changed = copy.deepcopy(store.current[RECORD_ID])
    changed["subject"]["display"] = "Tampered anchor presentation"
    store.current[RECORD_ID] = copy.deepcopy(changed)
    store.history[RECORD_ID][-1] = changed
    late = recheck(cfg, write, ledger)
    assert late.outcome == "UNCERTAIN" and not late.verified and not late.final
    assert late.evidence["targeted"]["history_status"] == "UNKNOWN/BLOCKED"


def test_changed_ensure_params_refuse_before_any_native_read_or_fence(
    begun: Any, tmp_path: Path
):
    store, _, cfg, _, result, write, ledger, _ = finish(begun)
    assert result.outcome == "created_verified", result.reasons
    before = list(store.calls)
    changed = {**PARAMS, "phone": "15550109999"}
    late = api.recheck(cfg, write, params=changed, actor=ACTOR, ledger=ledger)
    assert late.outcome == "UNCERTAIN" and not late.verified and not late.final
    assert store.calls == before
    assert not list((tmp_path / "authority").rglob("*.inflight"))


def test_targeted_refutation_stays_terminal_after_native_cleanup(begun: Any):
    store, _, cfg, _, result, write, ledger, _ = finish(begun)
    assert result.outcome == "created_verified", result.reasons
    store.save(
        name=PARAMS["name"],
        fields={"subject": body()["subject"], "valueString": "wrong-own-value"},
    )
    assert recheck(cfg, write, ledger).outcome == "REFUTED"
    store.save(
        name=PARAMS["name"],
        fields={"subject": body()["subject"], "valueString": body()["valueString"]},
    )
    late = recheck(cfg, write, ledger)
    assert late.outcome == "REFUTED" and not late.verified
    assert ledger.latest(write.write_key).state == "held"


def test_shadow_uses_same_canonical_producer_and_automatic_late_schedule(
    server: Any, tmp_path: Path
):
    store, base = server
    prepare(store)
    _, _, cfg = plan(base)
    sh = Shadow(
        upstream=base,
        mode="fhir",
        contracts=[cfg],
        actor_id=ACTOR.id,
        out=tmp_path / "shadow",
        quiet_s=0.01,
        recheck_s=0.15,
    ).start()
    try:
        post(
            sh.url,
            body(
                subject={"reference": "Patient/a", "display": PARAMS["display"]},
                valueString=PARAMS["phone"],
            ),
        )
        sh.flush()
        (episode,) = sh.episodes
        assert episode.verdict == "CONFIRMED", episode.reasons
        assert episode.recheck_at > 0
        deadline = time.monotonic() + 5
        while not episode.late_verdict and time.monotonic() < deadline:
            time.sleep(0.05)
        assert episode.late_verdict == "CONFIRMED", episode.late_reasons
        assert canonical_result(episode.base).recheck is not None
        assert episode.evidence["frame"]["hard_invariants"]["status"] == "held"
        assert not sh.errors
    finally:
        sh.stop()


def test_shadow_timeout_does_not_use_post_dispatch_baseline_as_authority(
    server: Any, tmp_path: Path
):
    store, base = server
    prepare(store)
    store.metadata_delay_s = 0.2
    _, _, cfg = plan(base)
    sh = Shadow(
        upstream=base,
        mode="fhir",
        contracts=[cfg],
        actor_id=ACTOR.id,
        out=tmp_path / "shadow",
        hold_ms=1,
        quiet_s=3600,
        recheck_s=0.15,
    ).start()
    try:
        post(sh.url, body())
        sh.flush()
        (episode,) = sh.episodes
        assert episode.verdict == "UNCERTAIN"
        assert episode.baseline_note
        assert episode.evidence["reconciliation"]["status"] == "owed"
        assert episode.evidence["reconciliation"]["executable"] is False
        sh.run_rechecks(now=True)
        assert episode.late_verdict == "UNCERTAIN"
    finally:
        sh.stop()
