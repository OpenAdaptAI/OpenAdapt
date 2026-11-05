"""Named failures and the targeted guarantee, independent of field matching."""

from openadapt_flow.frame_invariants import judge_frame_invariants
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, EffectState
from openadapt_flow.runtime.effects.frame import FrameChange, FrameSpec, FrameVerdict, judge_frame


def checks(verdict):
    return {check.name: check for check in verdict.checks}


def test_existing_frame_scope_refutation_gets_its_own_named_reason():
    before = EffectState(substrate="rest", reachable=True, records=[{"id": "other", "key": "another", "value": "original"}])
    after = [{"id": "other", "key": "another", "value": "changed"}, {"id": "target", "key": "task", "value": "expected"}]
    frame = judge_frame(FrameSpec(), [Effect(kind=EffectKind.RECORD_WRITTEN, match={"key": "task", "value": "expected"})], {"": (before, after)})
    assert frame.status == "refuted"
    result = checks(judge_frame_invariants(frame))
    assert result["declared_scope"].reason == "outside_scope_changes"
    assert result["declared_scope"].status == "refuted"
    assert result["at_most_one"].status == "not_checked"


def test_actual_removed_row_is_an_observed_delete():
    before = EffectState(substrate="rest", reachable=True, records=[{"id": "other", "key": "another"}])
    after = [{"id": "target", "key": "task"}]
    frame = judge_frame(FrameSpec(), [Effect(kind=EffectKind.RECORD_WRITTEN, match={"key": "task"})], {"": (before, after)})
    assert frame.status == "refuted"
    assert frame.collateral[0].observed_delete is True
    assert checks(judge_frame_invariants(frame))["no_undeclared_deletes"].status == "refuted"


def test_missing_expected_side_row_is_not_a_claimed_actual_delete():
    frame = FrameVerdict("refuted", "missing expected side row", collateral=[FrameChange("side", "removed", "unattributed")])
    result = checks(judge_frame_invariants(frame))
    assert result["declared_scope"].status == "refuted"
    assert result["no_undeclared_deletes"].status == "not_checked"
    assert result["no_undeclared_deletes"].observed_count == 0


def test_authoritative_other_actor_never_becomes_this_runs_scope_or_delete():
    frame = FrameVerdict("held", "other actor", concurrent=[FrameChange("records", "removed", "other", observed_delete=True)])
    result = checks(judge_frame_invariants(frame))
    assert result["declared_scope"].status == "not_checked"
    assert result["no_undeclared_deletes"].status == "not_checked"
    assert all(check.status != "refuted" for check in result.values())


def test_snapshot_or_targeted_domain_cannot_claim_global_history_held():
    result = judge_frame_invariants(FrameVerdict("held", "snapshot held"))
    assert result.status == "not_checked"
    assert all(check.status == "not_checked" for check in result.checks)
    assert result.evidence()["model_calls"] == 0


# These controls use the shared producer's DTOs, not an alternative reader.
# Production authority is tested through that producer after integration.
from dataclasses import replace
import json

import pytest

from openadapt_flow.runtime.effects.targeted import (
    SearchObservation, TargetedObservation, TargetObservation, VersionObservation,
)

CONTRACT = "sha256:" + "a" * 64
SCHEMA = "sha256:" + "b" * 64
MAC = "hmac-sha256:" + "c" * 64
RECEIPT = "d" * 64
WRITE_AT = "2026-11-05T10:00:00Z"
READ_AT = "2026-11-05T10:00:30Z"


def search(*identities, **over):
    values = dict(contract_sha256=CONTRACT, schema_sha256=SCHEMA,
                  matched_ids=frozenset(identities), candidate_count=len(identities),
                  complete=True, observed_at=WRITE_AT)
    return SearchObservation(**(values | over))


def target(**over):
    values = dict(identity="Patient/synthetic-target", target_id_mac=MAC,
                  receipt_bound=True, current_status="present", matches_receipt=True,
                  current={"resourceType": "Patient", "id": "synthetic-target"},
                  history_status="complete", history_anchor_bound=True, versions=())
    return TargetObservation(**(values | over))


def observation(**over):
    values = dict(receipt_sha256=RECEIPT, targets=(target(),),
                  write_search=search("Patient/synthetic-target"),
                  late_search=search("Patient/synthetic-target", observed_at=READ_AT),
                  read_at=READ_AT, history_required=True, phase="late")
    return TargetedObservation(**(values | over))


def version(**over):
    values = dict(version_id="synthetic-post-anchor-version", operation="delete",
                  body=None, author="synthetic-actor", author_trusted=True,
                  by_run=True, occurred_at=READ_AT, matches_receipt=False)
    return VersionObservation(**(values | over))


def judged(proof=None, **over):
    return judge_frame_invariants(FrameVerdict("held", "independent frame held"),
                                  proof or observation(**over))


def test_complete_target_domain_holds_without_global_historical_absence_claim():
    result = judged()
    assert result.status == "held"
    assert all(check.status == "held" for check in result.checks)
    evidence = result.evidence()
    assert evidence["schema_version"] == "openadapt.targeted-hard-invariants/v1"
    assert evidence["outside_guarantee"] == [{
        "name": "outside_receipt_intermediate_history", "status": "UNKNOWN",
        "reason": "outside_declared_guarantee",
    }]
    assert evidence["guarantee"]["receipt_sha256"] == RECEIPT
    assert evidence["guarantee"]["target_id_macs"] == [MAC]
    assert evidence["guarantee"]["read_at"] == READ_AT
    assert evidence["model_calls"] == 0


def test_identifying_search_candidates_are_not_matched_business_records():
    # The qualified reader may filter a broad candidate that is another person.
    result = judged(late_search=search("Patient/synthetic-target", candidate_count=2))
    assert checks(result)["at_most_one"].status == "held"
    assert checks(result)["at_most_one"].observed_count == 1


@pytest.mark.parametrize("which", ["write_search", "late_search"])
def test_duplicate_in_either_declared_search_refutes_despite_missing_history(which):
    result = judged(**{
        which: search("Patient/synthetic-target", "Patient/synthetic-duplicate", complete=False),
        "targets": (target(history_status="incomplete"),),
    })
    assert result.status == "refuted"
    assert checks(result)["at_most_one"].reason == "duplicate_business_key"


@pytest.mark.parametrize("which", ["write_search", "late_search"])
def test_partial_identifying_search_cannot_hold(which):
    result = judged(**{which: search("Patient/synthetic-target", complete=False)})
    assert result.status == "not_checked"
    assert checks(result)["at_most_one"].status == "not_checked"


@pytest.mark.parametrize("override", [
    {"receipt_sha256": "sha256:" + RECEIPT},
    {"receipt_sha256": ""},
    {"phase": "write"},
    {"phase": "unknown"},
    {"receipt_sha256": "untrusted-raw-identity"},
    {"targets": ()},
    {"targets": (target(receipt_bound=False),)},
    {"targets": (target(target_id_mac="sha256:" + "c" * 64),)},
    {"targets": (target(identity="Patient/a/_history/1"),)},
    {"targets": (target(), target())},
    {"late_search": search("Patient/synthetic-target", contract_sha256="sha256:" + "e" * 64)},
    {"late_search": search("Patient/synthetic-target", schema_sha256="sha256:" + "e" * 64)},
    {"late_search": search("Patient/synthetic-target", candidate_count=0)},
    {"late_search": search("Patient/synthetic-target", candidate_count=True)},
    {"late_search": search("Patient/synthetic-target", observed_at="2026-11-05T09:59:59Z")},
    {"read_at": "2026-11-05T10:00:00"},
])
def test_unbound_or_contradictory_facts_never_hold(override):
    assert judged(**override).status == "not_checked"


@pytest.mark.parametrize("status", ["incomplete", "unavailable"])
def test_missing_required_record_history_is_unknown(status):
    result = judged(targets=(target(history_status=status),))
    assert result.status == "not_checked"
    assert checks(result)["no_undeclared_deletes"].status == "not_checked"


def test_missing_anchor_cannot_turn_post_anchor_tuple_into_complete_proof():
    assert judged(targets=(target(history_anchor_bound=False),)).status == "not_checked"


def test_explicit_final_state_only_domain_names_unclaimed_record_history():
    result = judged(history_required=False, targets=(target(history_status="unavailable"),))
    assert result.status == "held"
    assert result.evidence()["outside_guarantee"][1] == {
        "name": "receipt_record_intermediate_history", "status": "UNKNOWN",
        "reason": "final_state_only_contract",
    }
    assert result.evidence()["guarantee"]["history_required"] is False


def test_authenticated_own_delete_refutes_even_with_incomplete_history():
    result = judged(targets=(target(history_status="incomplete", versions=(version(),)),))
    assert result.status == "refuted"
    assert checks(result)["no_undeclared_deletes"].reason == "undeclared_deletes"


def test_other_actors_delete_is_not_agent_harm():
    result = judged(targets=(target(versions=(version(by_run=False),)),))
    assert result.status == "held"


@pytest.mark.parametrize("changes", [
    {"author_trusted": False}, {"by_run": None}, {"by_run": 0},
])
def test_unknown_delete_actor_does_not_become_other_user_evidence(changes):
    result = judged(targets=(target(versions=(version(**changes),)),))
    assert result.status == "not_checked"
    assert checks(result)["no_undeclared_deletes"].status == "not_checked"


def test_unreadable_receipt_target_prevents_complete_final_state_claim():
    assert judged(targets=(target(current_status="unreadable", current=None),)).status == "not_checked"


def test_known_frame_refutations_survive_targeted_positive():
    frame = FrameVerdict("refuted", "existing refusal", collateral=[
        FrameChange("records", "removed", "self", observed_delete=True),
    ])
    result = judge_frame_invariants(frame, observation())
    assert result.status == "refuted"
    assert checks(result)["declared_scope"].status == "refuted"
    assert checks(result)["no_undeclared_deletes"].status == "refuted"


def test_target_evidence_keeps_raw_identifiers_bodies_and_authors_in_memory():
    proof = observation(targets=(target(versions=(version(by_run=False),)),))
    encoded = json.dumps(judged(proof).evidence())
    for raw in ("synthetic-target", "synthetic-actor", "synthetic-post-anchor-version"):
        assert raw not in encoded
    assert MAC in encoded
    assert "synthetic-target" not in repr(proof)


def test_targeted_boundary_never_upgrades_existing_frame_refutation():
    from openadapt_flow.verify_run import _apply_hard_invariants

    frame = FrameVerdict("refuted", "independent version binding refused")
    result = _apply_hard_invariants(frame, observation())
    assert result.status == "refuted"
    assert result.reason == "independent version binding refused"


def test_complete_targeted_positive_does_not_erase_independent_unknown_frame():
    from openadapt_flow.verify_run import _apply_hard_invariants

    frame = FrameVerdict("not_checked", "existing attribution gap")
    assert _apply_hard_invariants(frame, observation()).status == "not_checked"



def test_write_phase_is_bound_by_outer_receipt_without_fabricated_receipt_hash():
    result = judged(phase="write", receipt_sha256="")
    assert result.status == "held"
    guarantee = result.evidence()["guarantee"]
    assert guarantee["phase"] == "write"
    assert guarantee["receipt_sha256"] is None
    assert guarantee["receipt_binding"] == "outer_issued_receipt"
    assert guarantee["future_late_search"] == "owed"
    assert guarantee["late_search_count"] is None
    assert guarantee["late_search_observed_at"] is None
    assert guarantee["write_confirmation_count"] == 1
    assert guarantee["write_confirmation_observed_at"] == READ_AT


def test_late_phase_names_actual_completed_search_and_original_receipt():
    guarantee = judged().evidence()["guarantee"]
    assert guarantee["phase"] == "late"
    assert guarantee["receipt_sha256"] == RECEIPT
    assert guarantee["receipt_binding"] == "original_issued_receipt"
    assert guarantee["late_search_count"] == 1
    assert guarantee["late_search_observed_at"] == READ_AT
    assert guarantee["future_late_search"] is None
    assert guarantee["write_confirmation_count"] is None
