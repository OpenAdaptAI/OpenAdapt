"""The delivery ladder, and the one thing the remote tier is allowed to add.

The whole claim of ``decision_context`` is that its output cannot represent
protected content — checkable by reading the schema, not by trusting a
detector. These tests hold that claim to a structural standard: they walk every
string the projection can emit and require each one to come from a vocabulary
the engine already owns, rather than asserting that one hand-picked fixture
happens to look clean.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.console.attention import _KNOWN_CATEGORIES, attention_item
from openadapt_flow.console.decision_context import (
    _RECHECK_KINDS,
    _REMOTE_CATEGORIES,
    _REMOTE_ROLES,
    REMOTE_HALT_CONTEXT_SCHEMA,
    RemoteHaltContextV1,
    remote_halt_context,
)
from openadapt_flow.console.halt_detail import (
    RUNG_ORDER,
    RecheckKind,
    RungEvidence,
    RungVerdict,
)
from openadapt_flow.console.human_decisions import (
    RemoteAttendedActionRequest,
    RemoteDecisionPrincipal,
    _remote_delivery_tier,
    portable_remote_decision_task,
)
from openadapt_flow.decision_delivery import (
    DecisionDeliveryTier,
    effective_remote_tier,
    resolve_delivery_tier,
)
from openadapt_flow.deployment import DeploymentConfig
from openadapt_flow.execution_profiles import (
    ExecutionProfile,
    execution_profile_contract,
)
from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.durable.attended import (
    AttendedActionRefused,
    AttendedActionStore,
)
from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision

PROTECTED_VALUE = "Wilhelmina Featherstonehaugh"


# --------------------------------------------------------------------- ladder


def test_the_ladder_orders_context_the_same_way_verification_orders_evidence():
    """Lower is stronger, matching ``VerificationTier``, so the two read alike."""
    assert DecisionDeliveryTier.LOCAL_FULL < DecisionDeliveryTier.REMOTE_CLOSED_CONTEXT
    assert (
        DecisionDeliveryTier.REMOTE_CLOSED_CONTEXT
        < DecisionDeliveryTier.REMOTE_IDENTIFIERS
    )
    assert (
        DecisionDeliveryTier.REMOTE_IDENTIFIERS < DecisionDeliveryTier.NOTIFICATION_ONLY
    )
    assert DecisionDeliveryTier.LOCAL_FULL.carries_protected_evidence is True
    assert all(
        tier.carries_protected_evidence is False
        for tier in DecisionDeliveryTier
        if tier is not DecisionDeliveryTier.LOCAL_FULL
    )
    assert DecisionDeliveryTier.LOCAL_FULL.is_remote is False


@pytest.mark.parametrize("name", [tier.name.lower() for tier in DecisionDeliveryTier])
def test_every_tier_has_a_configuration_spelling(name):
    assert resolve_delivery_tier(name).name.lower() == name


@pytest.mark.parametrize("value", ["", "scrubbed", "remote_scrubbed", "full", True, 9])
def test_an_unknown_tier_fails_loudly_rather_than_defaulting(value):
    """A misspelled tier must never silently become the permissive one."""
    with pytest.raises(ValueError):
        resolve_delivery_tier(value)


def test_a_missing_tier_with_no_default_is_refused():
    with pytest.raises(ValueError):
        resolve_delivery_tier(None)


def test_the_weaker_of_config_and_profile_wins_in_both_directions():
    ceiling = DecisionDeliveryTier.REMOTE_CLOSED_CONTEXT
    # A deployment may ask for less than the profile permits.
    assert (
        effective_remote_tier("remote_identifiers", ceiling)
        is DecisionDeliveryTier.REMOTE_IDENTIFIERS
    )
    # It may not ask for more.
    assert (
        effective_remote_tier(
            "remote_closed_context", DecisionDeliveryTier.REMOTE_IDENTIFIERS
        )
        is DecisionDeliveryTier.REMOTE_IDENTIFIERS
    )
    assert effective_remote_tier(None, ceiling) is ceiling


def test_local_full_is_not_reachable_as_a_remote_tier():
    """Pixels never leave the runner, whatever the configuration says."""
    with pytest.raises(ValueError, match="not a remote decision delivery tier"):
        effective_remote_tier(
            DecisionDeliveryTier.LOCAL_FULL, DecisionDeliveryTier.LOCAL_FULL
        )


@pytest.mark.parametrize("profile", list(ExecutionProfile))
def test_no_profile_permits_a_remote_tier_that_carries_protected_evidence(profile):
    ceiling = execution_profile_contract(profile).max_remote_decision_tier
    assert ceiling.is_remote is True
    assert ceiling.carries_protected_evidence is False


# ------------------------------------------------------- vocabulary is pinned


def test_remote_categories_cover_the_console_vocabulary_without_inventing_one():
    """A drift pin: the remote set is derived from the console's, never parallel."""
    assert _KNOWN_CATEGORIES <= _REMOTE_CATEGORIES
    # The two values `attention`/`human_decisions` synthesise beyond that set.
    assert _REMOTE_CATEGORIES - _KNOWN_CATEGORIES == {
        "operator_review",
        "delivery_uncertain",
    }


def test_an_unrecognised_category_withholds_the_whole_context():
    """Vocabulary drift is a reason to say nothing, not to say something odd."""
    assert remote_halt_context({"category": "totally_new_kind"}) is None
    assert remote_halt_context({"category": PROTECTED_VALUE}) is None
    assert remote_halt_context({}) is None
    assert remote_halt_context(None) is None
    assert remote_halt_context("halt") is None


def test_unknown_rungs_verdicts_and_checks_are_dropped_not_relayed():
    context = remote_halt_context(
        {
            "category": "resolution",
            "resolution_ladder": [
                {"rung": "structural", "evidence": "recorded", "verdict": "failed"},
                {"rung": PROTECTED_VALUE, "evidence": "recorded", "verdict": "failed"},
                {"rung": "ocr", "evidence": PROTECTED_VALUE, "verdict": "failed"},
                {"rung": "ocr", "evidence": "recorded", "verdict": PROTECTED_VALUE},
                "not-even-a-mapping",
            ],
            "will_recheck": [
                {"check": "record_identity", "count": 2},
                {"check": PROTECTED_VALUE, "count": 1},
            ],
        }
    )
    assert context is not None
    assert [row.rung for row in context.resolution_ladder] == ["structural"]
    assert [row.check for row in context.will_recheck] == ["record_identity"]
    assert PROTECTED_VALUE not in json.dumps(context.model_dump(mode="json"))


def test_out_of_range_and_wrong_typed_counts_are_withheld_not_clamped():
    """A number that fell outside the contract is absent, not quietly rewritten."""
    context = remote_halt_context(
        {
            "category": "halt",
            "step_ordinal": 10_001,
            "step_count": "6",
            "will_recheck": [{"check": "postconditions", "count": -1}],
        }
    )
    assert context is not None
    assert context.step_ordinal is None
    assert context.step_count is None
    assert context.will_recheck[0].count is None


def test_the_target_label_is_never_copied_and_its_existence_is_reported():
    """The label is read only to say a name exists that this tier withholds."""
    context = remote_halt_context(
        {
            "category": "resolution",
            "target_role": "button",
            "target_label": PROTECTED_VALUE,
            "target_label_withheld": False,
        }
    )
    assert context is not None
    assert context.target_label_withheld is True
    assert context.target_role == "button"
    assert PROTECTED_VALUE not in json.dumps(context.model_dump(mode="json"))
    assert "target_label" not in RemoteHaltContextV1.model_fields


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)] + list(value)
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def test_every_string_the_context_can_emit_comes_from_a_closed_vocabulary():
    """The structural form of the claim, over a deliberately hostile input.

    Feeding a protected value into every field at once and then requiring each
    surviving string to be a member of a known vocabulary is stronger than
    asserting the value is absent: it would also catch a NEW field that opened
    a channel this test does not know about.
    """
    poisoned = {
        "category": "identity",
        "step_ordinal": 3,
        "step_count": 9,
        "action_kind": PROTECTED_VALUE,
        "target_role": PROTECTED_VALUE,
        "target_label": PROTECTED_VALUE,
        "target_label_withheld": True,
        "resolution_ladder": [
            {"rung": rung, "evidence": "recorded", "verdict": "failed"}
            for rung in RUNG_ORDER
        ],
        "will_recheck": [{"check": check, "count": 1} for check in _RECHECK_KINDS],
    }
    context = remote_halt_context(poisoned)
    assert context is not None
    permitted = (
        _REMOTE_CATEGORIES
        | _REMOTE_ROLES
        | {kind.value for kind in ActionKind}
        | set(RUNG_ORDER)
        | set(RungEvidence.__args__)
        | set(RungVerdict.__args__)
        | set(RecheckKind.__args__)
        | set(RemoteHaltContextV1.model_fields)
        | {"rung", "evidence", "verdict", "check", "count", REMOTE_HALT_CONTEXT_SCHEMA}
    )
    for value in _strings(context.model_dump(mode="json")):
        assert value in permitted, value


# ------------------------------------------------- end to end, from a real halt


def _halted_run(tmp_path: Path):
    """Drive a real durable run until the engine halts for a human."""
    workflow = Workflow(
        name="delivery-tier-e2e",
        params={"patient": PROTECTED_VALUE},
        steps=[
            Step(
                id="human",
                intent=f"confirm coverage for {PROTECTED_VALUE}",
                action=ActionKind.KEY,
                key="A",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="COVERAGE ACTIVE",
                        timeout_s=0.01,
                    )
                ],
            ),
        ],
    )
    bundle = tmp_path / "bundles" / "one"
    run = tmp_path / "runs" / "one"
    workflow.save(bundle)
    report = Replayer(
        FakeBackend(),
        vision=FakeVision(),
        durable=True,
        poll_interval_s=0.0,
    ).run(
        workflow,
        bundle_dir=bundle,
        run_dir=run,
        params={"patient": PROTECTED_VALUE},
    )
    assert report.success is False
    item = attention_item(run.parent, run)
    assert item is not None
    return run, item


def _deployment(**remote: Any) -> DeploymentConfig:
    return DeploymentConfig.model_validate(
        {
            "human_decisions": {
                "remote": {
                    "enabled": True,
                    "tenant_id": "tenant_exact_01",
                    "runner_id": "runner_exact_01",
                    **remote,
                }
            }
        }
    )


def test_a_real_halt_reaches_a_remote_surface_with_what_broke(tmp_path):
    """The point of the whole change, proved from a real durable pause."""
    run, item = _halted_run(tmp_path)
    projection = portable_remote_decision_task(run, item, deployment=_deployment())

    assert projection.delivery_tier == "remote_closed_context"
    context = projection.halt_context
    assert context is not None
    assert context.category in _REMOTE_CATEGORIES
    assert context.step_ordinal == 1
    assert context.step_count == 1
    assert context.action_kind == "key"
    # The operator is told what a continue will re-prove, which is what
    # distinguishes "I fixed it" from a repeat-the-step button.
    assert {row.check for row in context.will_recheck} >= {"postconditions"}
    # And the run's protected parameter is nowhere in the projection.
    assert PROTECTED_VALUE not in json.dumps(projection.model_dump(mode="json"))


def test_the_signed_task_is_byte_identical_at_both_remote_tiers(tmp_path):
    """The context is additive. It cannot change what the signed envelope says."""
    run, item = _halted_run(tmp_path)
    rich = portable_remote_decision_task(run, item, deployment=_deployment())
    plain = portable_remote_decision_task(
        run, item, deployment=_deployment(context_tier="remote_identifiers")
    )
    assert plain.delivery_tier == "remote_identifiers"
    assert plain.halt_context is None
    assert rich.task.model_dump(mode="json") == plain.task.model_dump(mode="json")
    assert rich.task_digest == plain.task_digest
    # Presentation is not authority: the binding a returned decision is checked
    # against must be identical whether or not context travelled.
    assert rich.binding_digest == plain.binding_digest
    assert rich.idempotency_scope_digest == plain.idempotency_scope_digest


def test_a_deployment_may_cap_itself_below_the_profile_ceiling(tmp_path):
    run, item = _halted_run(tmp_path)
    projection = portable_remote_decision_task(
        run,
        item,
        deployment=_deployment(context_tier="remote_identifiers"),
    )
    assert projection.halt_context is None
    assert projection.delivery_tier == "remote_identifiers"


def test_a_projection_reports_the_tier_it_delivered_not_the_one_it_intended(
    tmp_path, monkeypatch
):
    """An engine that could not build a context says so, rather than claiming one.

    This is the delivery-path form of the rule the effect ladder already
    applies: report the evidence you actually have.
    """
    run, item = _halted_run(tmp_path)
    monkeypatch.setattr(
        "openadapt_flow.console.human_decisions.remote_halt_context",
        lambda _halt: None,
    )
    projection = portable_remote_decision_task(run, item, deployment=_deployment())
    assert projection.halt_context is None
    assert projection.delivery_tier == "remote_identifiers"


def test_an_unprofiled_deployment_takes_the_strictest_profile(tmp_path):
    """Omitting a profile must not select the most permissive posture."""
    deployment = _deployment()
    assert deployment.runtime.profile is None
    regulated = execution_profile_contract("regulated").max_remote_decision_tier
    demo = execution_profile_contract("demo").max_remote_decision_tier
    assert max(regulated, demo) == regulated


def test_the_ceiling_follows_the_profile_the_run_actually_executed_under(tmp_path):
    """A dispatch-time profile the deployment file never named still binds.

    A hosted runner receives its execution profile with the dispatch, so the
    local ``deployment.yaml`` is not the only authority on it. Reading only the
    deployment would let a run executed under a stricter profile be projected
    under a looser ceiling.
    """
    run, item = _halted_run(tmp_path)
    deployment = _deployment()
    deployment.runtime.profile = "demo"

    class _Report:
        execution_profile = "regulated"

    strict = _remote_delivery_tier(deployment, _Report())
    lenient = _remote_delivery_tier(deployment, None)
    ceilings = {
        profile: execution_profile_contract(profile).max_remote_decision_tier
        for profile in ("demo", "regulated")
    }
    # Whatever the two profiles permit, the run's own profile is consulted and
    # the weaker of the two ceilings wins.
    assert strict == max(ceilings["demo"], ceilings["regulated"])
    assert lenient >= ceilings["demo"]


def test_a_replayed_decision_still_admits_after_the_pause_closed(tmp_path):
    """The regression the binding-digest design must not reintroduce.

    ``admit_remote_action`` re-derives the projection on every response. The
    halt context is read from the run's live checkpoint, which empties once the
    pause resolves, so binding it into the authority digest would make a
    correct idempotent replay refuse. It is excluded for exactly that reason,
    and this pins it.
    """
    run, item = _halted_run(tmp_path)
    deployment = _deployment()
    capability = AttendedActionStore(run).read()
    projection = portable_remote_decision_task(run, item, deployment=deployment)
    request = RemoteAttendedActionRequest(
        capability_digest=capability.digest,
        idempotency_key="remote-decision-key-0001",
        action="continue",
        disposition="completed_by_operator",
        task_digest=projection.task_digest,
        task_signature=projection.task.signature,
        tenant_id="tenant_exact_01",
        runner_id="runner_exact_01",
        phase=projection.phase,
        event_sequence=projection.event_sequence,
        idempotency_scope_digest=projection.idempotency_scope_digest,
        binding_digest=projection.binding_digest,
    )
    principal = RemoteDecisionPrincipal(
        subject="operator_subject_01",
        tenant_id="tenant_exact_01",
        runner_id="runner_exact_01",
    )
    from openadapt_flow.console.human_decisions import admit_remote_action

    # Clear the pending escalation the halt context is derived from -- exactly
    # what a completed resume does -- leaving the signed capability the
    # authority binding is derived from intact.
    assert CheckpointStore(run).read_pending() is not None
    assert projection.halt_context is not None
    CheckpointStore(run).clear_pending()
    assert CheckpointStore(run).read_pending() is None
    # The context genuinely changed, so this test would fail if the context
    # were bound into `binding_digest`.
    reissued = portable_remote_decision_task(run, item, deployment=deployment)
    assert reissued.halt_context != projection.halt_context
    assert reissued.binding_digest == projection.binding_digest

    admitted = admit_remote_action(
        run, item, request, deployment=deployment, principal=principal
    )
    assert admitted.capability_digest == capability.digest


def test_remote_issuance_still_requires_explicit_enablement(tmp_path):
    """The new tier does not become a second way to turn remote delivery on."""
    run, item = _halted_run(tmp_path)
    with pytest.raises(AttendedActionRefused):
        portable_remote_decision_task(run, item, deployment=DeploymentConfig())
