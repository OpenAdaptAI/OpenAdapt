"""The FHIR adapter consumes history without changing legacy field judgment."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openadapt_flow.ensure_invariants import judge_ensure_invariants
from openadapt_flow.ensure_record import Baseline, Change, EnsureContract, judge

BOT = "Practitioner/synthetic-writer"
OTHER = "Practitioner/synthetic-other"
T0 = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
PARAMS = {"key": "synthetic-1853", "phone": "5550100"}


def contract(**over):
    return EnsureContract.model_validate(
        {
            "resource_type": "Patient",
            "base_url": "http://synthetic.test/fhir/R4",
            "self": [BOT],
            "identity": [["key"]],
            "search": {"key": "identifier"},
            "fields": {
                "key": {"path": "identifier[system=urn:synthetic:key].value"},
                "phone": {"path": "telecom[system=phone].value", "compare": "digits"},
            },
            "closed": True,
            "settle_s": 1,
            **over,
        }
    )


def patient(rid, **over):
    return {
        "resourceType": "Patient",
        "id": rid,
        "identifier": [{"system": "urn:synthetic:key", "value": PARAMS["key"]}],
        "telecom": [{"system": "phone", "value": PARAMS["phone"]}],
        **over,
    }


def change(ref, *actions, bodies=(), prior=None, by=BOT, **over):
    return Change(
        ref=ref,
        actions=set(actions),
        authors={by} if by else set(),
        bodies=list(bodies),
        prior=prior,
        versions=len(bodies),
        events=[
            (T0 + timedelta(seconds=i + 1), action, frozenset({by}))
            for i, action in enumerate(actions)
        ],
        **over,
    )


def result(changes, *, c=None, before=(), after=None, **flags):
    return judge_ensure_invariants(
        c or contract(),
        PARAMS,
        Baseline(T0, {r["id"]: r for r in before}),
        changes,
        {"x": patient("x")} if after is None else after,
        history_complete=True,
        search_complete=True,
        **flags,
    )


def checks(verdict):
    return {c.name: c for c in verdict.checks}


def created(**over):
    return {"Patient/x": change("Patient/x", "C", bodies=[patient("x", **over)])}


def test_complete_audit_history_and_search_prove_the_layer():
    assert result(created()).status == "held"
    assert result({}, before=[patient("x")]).status == "held"


def test_caller_cannot_implicitly_claim_completeness():
    c = contract()
    verdict = judge_ensure_invariants(
        c, PARAMS, Baseline(T0, {}), created(), {"x": patient("x")}
    )
    assert verdict.status == "not_checked"
    assert {v.status for v in verdict.checks} == {"not_checked"}


@pytest.mark.parametrize("by, attributed", [(BOT, True), (OTHER, False), ("", True)])
def test_outside_scope_change_is_refuted_with_conservative_attribution(by, attributed):
    other = change(
        "Observation/outside",
        "U",
        bodies=[{"resourceType": "Observation", "id": "outside"}],
        by=by,
        attributed=attributed,
    )
    verdict = result({**created(), other.ref: other})
    assert checks(verdict)["declared_scope"].status == "refuted"


def test_authoritatively_attributed_other_actor_change_is_not_our_scope_violation():
    other = change(
        "Observation/outside",
        "U",
        bodies=[{"resourceType": "Observation", "id": "outside"}],
        by=OTHER,
    )
    assert result({**created(), other.ref: other}).status == "held"


def test_any_own_write_after_finding_the_record_is_outside_ensure_scope():
    verdict = result(
        {"Patient/x": change("Patient/x", "U", bodies=[patient("x")])},
        before=[patient("x")],
    )
    assert checks(verdict)["declared_scope"].status == "refuted"


def test_deleted_duplicate_is_detected_in_history_when_search_returns_one():
    gone = change("Patient/gone", "C", "D", bodies=[patient("gone")], by=OTHER)
    verdict = result({**created(), gone.ref: gone})
    assert checks(verdict)["at_most_one"].status == "refuted"
    assert checks(verdict)["at_most_one"].observed_count == 2
    assert checks(verdict)["declared_scope"].status == "held"
    assert checks(verdict)["no_undeclared_deletes"].status == "held"


def test_known_historical_duplicate_refutes_even_with_another_missing_history():
    gone = change("Patient/gone", "C", "D", bodies=[patient("gone")], by=OTHER)
    missing = Change("Patient/unknown", actions={"U"}, authors={OTHER})
    verdict = result({**created(), gone.ref: gone, missing.ref: missing})
    assert checks(verdict)["at_most_one"].status == "refuted"


def test_prior_key_match_is_not_erased_by_an_identity_edit():
    now = patient(
        "prior", identifier=[{"system": "urn:synthetic:key", "value": "other-key"}]
    )
    prior = change("Patient/prior", "U", bodies=[now], prior=patient("prior"), by=OTHER)
    assert (
        checks(result({**created(), prior.ref: prior}))["at_most_one"].status
        == "refuted"
    )


@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "count",
        "empty",
        "wrong_type",
        "wrong_id",
        "not_mapping",
        "deleted_without_body",
    ],
)
def test_partial_or_mismatched_history_is_not_a_held_history_proof(mode):
    changes = created()
    c = changes["Patient/x"]
    if mode == "missing":
        c.versions = None
    elif mode == "count":
        c.versions = 2
    elif mode == "empty":
        c.versions, c.bodies = 0, []
    elif mode == "wrong_type":
        c.bodies[0]["resourceType"] = "Observation"
    elif mode == "wrong_id":
        c.bodies[0]["id"] = "other"
    elif mode == "not_mapping":
        c.bodies[0] = None
    else:
        changes["Patient/unknown"] = change("Patient/unknown", "D", by=OTHER)
    verdict = result(changes)
    assert checks(verdict)["at_most_one"].status == "not_checked"


@pytest.mark.parametrize("which", ["audit_free", "unlogged", "history_disabled"])
def test_incomplete_feed_cannot_establish_all_three_predicates(which):
    c, changes = contract(), created()
    if which == "audit_free":
        c = contract(change_feed={"audit": None, "sweep_types": ["Patient"]})
    elif which == "unlogged":
        changes["Patient/x"].unlogged = True
    else:
        c = contract(change_feed={"history": False})
    assert result(changes, c=c).status == "not_checked"


def test_undeclared_delete_is_an_independent_refutation():
    gone = change("Observation/gone", "D")
    verdict = result({**created(), gone.ref: gone})
    assert checks(verdict)["no_undeclared_deletes"].reason == "undeclared_deletes"


def test_proven_declared_cleanup_does_not_rewrite_literal_key_history_or_legacy_admission():
    c = contract(operations={"cardinality": {"own_transient": True}})
    gone = change("Patient/gone", "C", "D", bodies=[patient("gone")])
    changes = {**created(), gone.ref: gone}
    verdict = result(changes, c=c)
    assert checks(verdict)["declared_scope"].status == "held"
    assert checks(verdict)["no_undeclared_deletes"].status == "held"
    assert checks(verdict)["at_most_one"].status == "refuted"
    # This independent component does not silently change admission policy.
    old = judge(c, PARAMS, Baseline(T0, {}), changes, {"x": patient("x")}, patient("x"))
    assert old.outcome == "created_verified"


def test_unsafe_cleanup_does_not_excuse_scope_or_deletion():
    c = contract(operations={"cardinality": {"own_transient": True}})
    gone = change("Patient/gone", "C", "D", bodies=[patient("gone")])
    gone.reads = [(T0 + timedelta(seconds=1.5), frozenset({OTHER}))]
    verdict = result({**created(), gone.ref: gone}, c=c)
    assert checks(verdict)["declared_scope"].status == "refuted"
    assert checks(verdict)["no_undeclared_deletes"].status == "refuted"


def test_layer_is_separate_from_unchanged_strict_scalar_comparison():
    bad = patient("x", telecom=[{"system": "phone", "value": "5550199"}])
    changes = {"Patient/x": change("Patient/x", "C", bodies=[bad])}
    assert result(changes).status == "held"
    old = judge(contract(), PARAMS, Baseline(T0, {}), changes, {"x": bad}, bad)
    assert old.outcome == "needs_review"
    assert any("phone" in reason for reason in old.reasons)


def test_declared_bookkeeping_uses_existing_exact_path_proof():
    prior = {
        "resourceType": "Practitioner",
        "id": "writer",
        "meta": {"versionId": "1", "lastUpdated": "2026-11-04T12:00:00Z"},
        "active": True,
    }
    after = {**prior, "meta": {"versionId": "2", "lastUpdated": "2026-11-04T12:00:01Z"}}
    c = contract(
        operations={
            "bookkeeping": [
                {
                    "resource_type": "Practitioner",
                    "paths": ["meta.versionId", "meta.lastUpdated"],
                }
            ]
        }
    )
    book = change("Practitioner/writer", "U", bodies=[after], prior=prior)
    assert result({**created(), book.ref: book}, c=c).status == "held"
    book.bodies[0] = {**after, "active": False}
    assert (
        checks(result({**created(), book.ref: book}, c=c))["declared_scope"].status
        == "refuted"
    )


def test_missing_business_key_parameters_cannot_establish_history():
    verdict = judge_ensure_invariants(
        contract(),
        {},
        Baseline(T0, {}),
        created(),
        {"x": patient("x")},
        history_complete=True,
        search_complete=True,
    )
    assert checks(verdict)["at_most_one"].status == "not_checked"


def test_correction_scope_uses_existing_proof_instead_of_only_accept_switch():
    x = patient("x")
    updated = change("Patient/x", "C", "U", bodies=[x, x])
    assert result({updated.ref: updated}).status == "held"
    updated.reads = [(T0 + timedelta(seconds=1.5), frozenset({OTHER}))]
    assert checks(result({updated.ref: updated}))["declared_scope"].status == "refuted"
    updated.reads = []
    assert (
        checks(result({updated.ref: updated}, c=contract(accept_corrections=False)))[
            "declared_scope"
        ].status
        == "refuted"
    )


def test_malformed_early_history_does_not_hide_a_later_known_duplicate():
    unreadable = change("Patient/unknown", "C", bodies=[None], by=OTHER)
    gone = change("Patient/gone", "C", "D", bodies=[patient("gone")], by=OTHER)
    verdict = result({unreadable.ref: unreadable, **created(), gone.ref: gone})
    assert checks(verdict)["at_most_one"].status == "refuted"


# Production admission is stricter than the independent field/operation judge.
# These retain that judge's original assertions and test the separate gate.


def production(changes, *, c=None, before=(), after=None, body=None):
    from openadapt_flow.ensure_invariants import enforce_ensure_invariants

    c = c or contract()
    baseline = Baseline(T0, {record["id"]: record for record in before})
    after = {"x": patient("x")} if after is None else after
    original = judge(c, PARAMS, baseline, changes, after, body or patient("x"))
    return original, enforce_ensure_invariants(
        original, c, PARAMS, baseline, changes, after
    )


def test_production_does_not_upgrade_snapshot_only_create_to_verified():
    original, guarded = production(created())
    assert original.outcome == "created_verified"
    assert guarded.outcome == "needs_review"
    assert guarded.record_id is None
    assert guarded.evidence["invariants_unknown"] is True
    assert guarded.evidence["hard_invariants"]["status"] == "not_checked"
    assert all("hard_invariant:" in reason for reason in guarded.reasons[1:])


def test_production_search_only_admission_is_unknown():
    c = contract(change_feed={"audit": None, "history": False, "sweep_types": ["Patient"]})
    original, guarded = production(created(), c=c)
    assert original.outcome == "created_verified"
    assert guarded.outcome == "needs_review"
    assert guarded.evidence["invariants_unknown"] is True


def test_production_found_existing_also_needs_authenticated_target_proof():
    original, guarded = production({}, before=[patient("x")])
    assert original.outcome == "found_existing"
    assert guarded.outcome == "needs_review"
    assert guarded.evidence["invariants_unknown"] is True


def test_production_known_scope_failure_remains_refuted_when_history_unknown():
    outside = change("Observation/outside", "U", bodies=[{"resourceType": "Observation", "id": "outside"}])
    original, guarded = production({**created(), outside.ref: outside})
    assert original.outcome == guarded.outcome == "needs_review"
    assert guarded.evidence["invariants_refuted"] is True
    assert "invariants_unknown" not in guarded.evidence


def test_production_unknown_history_does_not_excuse_wrong_scalar():
    bad = patient("x", telecom=[{"system": "phone", "value": "5550199"}])
    original, guarded = production(created(telecom=bad["telecom"]), after={"x": bad}, body=bad)
    assert original.outcome == guarded.outcome == "needs_review"
    assert any("phone" in reason for reason in guarded.reasons)
    assert "invariants_unknown" not in guarded.evidence


def test_production_actor_and_search_absence_are_not_run_receipt_binding():
    c = contract(operations={"cardinality": {"own_transient": True}})
    gone = change("Patient/gone", "C", "D", bodies=[patient("gone")])
    original, guarded = production({**created(), gone.ref: gone}, c=c)
    assert original.outcome == "created_verified"
    assert guarded.outcome == "needs_review"
    assert guarded.evidence["invariants_refuted"] is True
    assert any("own_transient_receipt_binding_missing" in reason for reason in guarded.reasons)
    assert "invariants_unknown" not in guarded.evidence
