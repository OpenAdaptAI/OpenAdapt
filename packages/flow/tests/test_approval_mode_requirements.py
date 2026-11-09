"""Requirements E1 to E8 for "Only ask a person about problems".

All evidence below is made up. ``_all_met`` builds a summary in which every
requirement holds, and each test removes one thing to show the requirement
it belongs to fails with a closed reason. The evaluator reads people's
recorded decisions; it never makes one.
"""

from __future__ import annotations

import pytest

from openadapt_flow.approval_mode.model import DESKTOP_LIST_WITH_HEADER
from openadapt_flow.approval_mode.requirements import (
    REQUIRED_DRILLS,
    ApprovalReceiptEvidence,
    DeploymentEvidence,
    PersonDecisions,
    ReadinessEvidence,
    RequirementCode,
    RequirementsEvidence,
    RunnerSetupEvidence,
    VerifierEvidence,
    evaluate_requirements,
)
from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
from openadapt_flow.qualification import (
    ActionRiskClass,
    ActionRiskClassification,
    EnvironmentBoundary,
    init_project,
)
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.effects.effect import ValueExpr
from tests._approval_mode_support import VERIFIER_IDENTITY


def _anchor(text: str) -> Anchor:
    return Anchor(
        template="t.png", region=(0, 0, 10, 10), click_point=(5, 5), ocr_text=text
    )


def _create_effects() -> list[Effect]:
    return [
        Effect(
            kind=EffectKind.EXACT_NEW_SET,
            match={"clinic": ValueExpr(literal="clinic-test")},
            new_records=[
                {
                    "patient_id": ValueExpr(param="patient_id"),
                    "note": ValueExpr(param="note"),
                }
            ],
            expected_count=1,
            idempotency_key=ValueExpr(param="__run_id__"),
            risk="irreversible",
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"patient_id": ValueExpr(param="patient_id")},
            field="note",
            value=ValueExpr(param="note"),
        ),
    ]


def _workflow(*, save_text: str = "Save referral", effects=None) -> Workflow:
    return Workflow(
        name="referral",
        steps=[
            Step(
                id="patient",
                intent="type the patient id",
                action=ActionKind.TYPE,
                param="patient_id",
                anchor=_anchor("Patient"),
            ),
            Step(
                id="note",
                intent="type the note",
                action=ActionKind.TYPE,
                param="note",
                anchor=_anchor("Note"),
            ),
            Step(
                id="save",
                intent=save_text,
                action=ActionKind.CLICK,
                risk="irreversible",
                anchor=_anchor(save_text),
                effects=_create_effects() if effects is None else effects,
            ),
        ],
    )


def _approval(index: int, approver: str, **overrides) -> ApprovalReceiptEvidence:
    fields = {
        "run_ref": f"{index:064x}",
        "transaction_outcome": "VERIFIED",
        "whole_run_clean": True,
        "read_back_by_record_check": True,
        "decided_by": "human",
        "approver_cloud_user_id": approver,
        "authentication": "aal2",
        "route": "desktop_run_it",
        "approver_is_runner_account": False,
        "after_last_switch_off": True,
        "same_version_runner_environment_verifier_user": True,
    }
    fields.update(overrides)
    return ApprovalReceiptEvidence(**fields)


def _all_met(**overrides) -> RequirementsEvidence:
    fields = {
        "deployment": DeploymentEvidence(
            certified_and_sealed=True,
            admission_active=True,
            profile="regulated",
            model_grounding_allowed=False,
            environment_observer_bound=True,
            policy_name="clinical-write",
            policy_pack="regulated",
            minimum_effect_tier=1,
        ),
        "verifiers": (
            VerifierEvidence(
                kind="rest", verifier_identity=VERIFIER_IDENTITY, connection_ok=True
            ),
        ),
        "readiness": ReadinessEvidence(
            practice_case_transaction_outcome="VERIFIED",
            practice_case_read_back_by="deployment_verifier",
            drills={drill: "as_agreed" for drill in REQUIRED_DRILLS},
            customer_stop_cases=(
                "HALTED_BEFORE_EFFECT",
                "HALTED_BEFORE_EFFECT",
                "REJECTED_POLICY",
            ),
            whole_item_snapshot_clean=True,
            no_silent_incorrect_success=True,
            no_silent_incorrect_success_judged_by="test_setup_read_back",
            production_write_authorization_sha256="sha256:" + "5" * 64,
            clinic_sign_off_recorded=True,
        ),
        "approvals": tuple(
            _approval(i, "user-dana" if i % 2 else "user-lee") for i in range(20)
        ),
        "runner_setup": RunnerSetupEvidence(
            current=True, signed_in_user_check_armed=True
        ),
        "person": PersonDecisions(
            no_sign_attest_or_order=True,
            nothing_sent_outside_clinic=True,
            no_downstream_action_before_read_back=True,
            values_source="clinic_staff",
            privacy_contact_named=True,
        ),
        "channel": DESKTOP_LIST_WITH_HEADER,
        "stop_answered_in_cloud": True,
    }
    fields.update(overrides)
    return RequirementsEvidence(**fields)


def _unmet(workflow, evidence):
    report = evaluate_requirements(workflow, evidence)
    return report.first_unmet


def test_a_qualifying_workflow_meets_every_requirement():
    report = evaluate_requirements(_workflow(), _all_met())
    assert report.all_met, report.first_unmet
    assert [r.code for r in report.requirements] == list(RequirementCode)


def test_nothing_recorded_fails_at_the_first_requirement():
    report = evaluate_requirements(_workflow(), RequirementsEvidence())
    assert not report.all_met
    assert report.first_unmet.code is RequirementCode.E1_LOCKED_VERSION
    assert not any(r.met for r in report.requirements)


# -- E1 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"certified_and_sealed": False}, "not_certified_and_sealed"),
        ({"admission_active": False}, "no_active_admission"),
        ({"profile": "standard"}, "profile_not_regulated"),
        ({"profile": "demo"}, "profile_not_regulated"),
        ({"model_grounding_allowed": True}, "model_egress_allowed"),
        ({"environment_observer_bound": False}, "no_environment_observer"),
    ],
)
def test_e1_locked_version_regulated_no_egress(change, reason):
    deployment = _all_met().deployment.model_copy(update=change)
    unmet = _unmet(_workflow(), _all_met(deployment=deployment))
    assert unmet.code is RequirementCode.E1_LOCKED_VERSION
    assert unmet.reason == reason


# -- E2 -----------------------------------------------------------------------------


def _e2(workflow=None, **overrides):
    report = evaluate_requirements(workflow or _workflow(), _all_met(**overrides))
    return report.requirements[1]


def test_e2_requires_the_regulated_pack_and_clinical_write():
    deployment = _all_met().deployment.model_copy(update={"minimum_effect_tier": 2})
    assert _e2(deployment=deployment).reason == "not_qualified_with_regulated_pack"
    deployment = _all_met().deployment.model_copy(update={"policy_name": "permissive"})
    assert _e2(deployment=deployment).reason == "not_under_clinical_write"


@pytest.mark.parametrize(
    "verifier,reason",
    [
        (
            VerifierEvidence(kind="rest", connection_ok=True),
            "verifier_identity_missing",
        ),
        (
            VerifierEvidence(
                kind="onscreen", verifier_identity=VERIFIER_IDENTITY, connection_ok=True
            ),
            "verifier_kind_not_allowed",
        ),
        (
            VerifierEvidence(
                kind="plugin", verifier_identity=VERIFIER_IDENTITY, connection_ok=True
            ),
            "plugin_not_reviewed",
        ),
        (
            VerifierEvidence(
                kind="file", verifier_identity=VERIFIER_IDENTITY, connection_ok=True
            ),
            "file_source_not_attested",
        ),
        (
            VerifierEvidence(kind="sql", verifier_identity=VERIFIER_IDENTITY),
            "record_check_unreachable",
        ),
    ],
)
def test_e2_allows_only_reviewed_system_of_record_verifiers(verifier, reason):
    assert _e2(verifiers=(verifier,)).reason == reason


def test_e2_accepts_a_reviewed_plugin_and_an_attested_file_source():
    plugin = VerifierEvidence(
        kind="plugin",
        verifier_identity=VERIFIER_IDENTITY,
        plugin_review_sha256="sha256:" + "6" * 64,
        connection_ok=True,
    )
    assert _e2(verifiers=(plugin,)).met
    document = VerifierEvidence(
        kind="document_hash", verifier_identity=VERIFIER_IDENTITY, connection_ok=True
    )
    setup = RunnerSetupEvidence(
        current=True, signed_in_user_check_armed=True, file_source_attested=True
    )
    assert _e2(verifiers=(document,), runner_setup=setup).met


def test_e2_refuses_an_input_the_record_check_does_not_cover():
    effects = _create_effects()
    effects[0] = effects[0].model_copy(
        update={"new_records": [{"patient_id": ValueExpr(param="patient_id")}]}
    )
    workflow = _workflow(effects=[effects[0]])
    result = _e2(workflow)
    assert not result.met
    assert result.reason == "input_not_read_back"
    assert result.step_ids == ("save",)


def test_e2_refuses_an_update_until_the_field_delta_check_exists():
    update_only = [
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"patient_id": ValueExpr(param="patient_id")},
            field="note",
            value=ValueExpr(param="note"),
            idempotency_key=ValueExpr(param="__run_id__"),
        )
    ]
    result = _e2(_workflow(effects=update_only))
    assert result.reason == "update_needs_field_delta_check"


def test_e2_requires_an_idempotency_key_on_every_write():
    effects = [
        effect.model_copy(update={"idempotency_key": None})
        for effect in _create_effects()
    ]
    assert _e2(_workflow(effects=effects)).reason == "write_without_idempotency_key"


def test_e2_refuses_a_save_without_any_record_check():
    assert _e2(_workflow(effects=[])).reason == "no_record_check"


# -- E3 -----------------------------------------------------------------------------


def test_e3_refuses_irreversible_writes():
    result = evaluate_requirements(
        _workflow(save_text="Delete referral"), _all_met()
    ).requirements[2]
    assert result.reason == "irreversible_write"
    result = evaluate_requirements(
        _workflow(save_text="Send to payer"), _all_met()
    ).requirements[2]
    assert result.reason == "irreversible_write"


def test_e3_refuses_a_step_without_an_effect_classified_harmless():
    workflow = _workflow()
    project = init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Made-up records system",
            application_version="1",
            environment_digest="e" * 64,
            runtime_version="1.0.0",
        ),
    )
    project.action_classifications["note"] = ActionRiskClassification(
        step_id="note",
        classification=ActionRiskClass.STATE_CHANGING,
        explanation="typing into a form field",
        operator_confirmed=True,
    )
    result = evaluate_requirements(workflow, _all_met()).requirements[2]
    assert result.reason == "unchecked_step_classified_harmless"
    assert "note" in result.step_ids


@pytest.mark.parametrize(
    "missing,reason",
    [
        ("no_sign_attest_or_order", "sign_attest_order_not_recorded"),
        ("nothing_sent_outside_clinic", "outside_sending_not_recorded"),
        ("no_downstream_action_before_read_back", "downstream_action_not_recorded"),
    ],
)
def test_e3_needs_the_recorded_person_decisions(missing, reason):
    person = _all_met().person.model_copy(update={missing: False})
    result = evaluate_requirements(_workflow(), _all_met(person=person)).requirements[2]
    assert result.reason == reason
    assert result.checked_by == "person"


# -- E4 -----------------------------------------------------------------------------


def _e4(**readiness_change):
    readiness = _all_met().readiness.model_copy(update=readiness_change)
    return evaluate_requirements(
        _workflow(), _all_met(readiness=readiness)
    ).requirements[3]


def test_e4_rejects_a_practice_read_back_by_the_test_setup():
    assert _e4(practice_case_read_back_by="test_setup").reason == (
        "practice_case_not_read_back_by_deployment"
    )


def test_e4_accepts_the_test_setup_read_back_for_silent_errors_only_labeled():
    assert _e4(no_silent_incorrect_success_judged_by="test_setup_read_back").met
    assert _e4(no_silent_incorrect_success_judged_by=None).reason == (
        "silent_incorrect_success_not_ruled_out"
    )


@pytest.mark.parametrize("drill", REQUIRED_DRILLS)
def test_e4_a_drill_that_could_not_run_has_not_passed(drill):
    drills = {name: "as_agreed" for name in REQUIRED_DRILLS if name != drill}
    assert _e4(drills=drills).reason == f"drill_not_run_{drill}"
    drills[drill] = "not_as_agreed"
    assert _e4(drills=drills).reason == f"drill_failed_{drill}"


def test_e4_a_must_stop_case_that_saves_fails():
    assert _e4(
        customer_stop_cases=("HALTED_BEFORE_EFFECT", "VERIFIED", "REJECTED_POLICY")
    ).reason == ("must_stop_case_saved")
    assert _e4(customer_stop_cases=("HALTED_BEFORE_EFFECT",)).reason == (
        "too_few_must_stop_cases"
    )


def test_e4_needs_the_clinic_sign_off_and_written_authorization():
    assert _e4(clinic_sign_off_recorded=False).reason == "clinic_sign_off_missing"
    assert _e4(production_write_authorization_sha256=None).reason == (
        "production_write_authorization_missing"
    )
    assert _e4(whole_item_snapshot_clean=False).reason == "write_outside_gated_steps"


# -- E5 -----------------------------------------------------------------------------


def _e5(approvals):
    return evaluate_requirements(
        _workflow(), _all_met(approvals=tuple(approvals))
    ).requirements[4]


def _streak(**overrides_for_last):
    items = [_approval(i, "user-dana" if i % 2 else "user-lee") for i in range(19)]
    items.append(_approval(19, "user-dana", **overrides_for_last))
    return items


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"declined": True}, "declined_item"),
        ({"transaction_outcome": "COMPLETED_UNVERIFIED"}, "approved_item_not_clean"),
        ({"whole_run_clean": False}, "approved_item_not_clean"),
        ({"read_back_by_record_check": False}, "approved_item_not_clean"),
        ({"decided_by": "automation"}, "approval_not_authenticated_person"),
        ({"decided_by": "unknown"}, "approval_not_authenticated_person"),
        ({"authentication": "aal1"}, "approval_not_authenticated_person"),
        ({"route": "phone"}, "approval_not_authenticated_person"),
        ({"route": "console"}, "approval_not_authenticated_person"),
        ({"approver_cloud_user_id": None}, "approval_not_authenticated_person"),
        ({"approver_is_runner_account": True}, "approval_not_authenticated_person"),
        (
            {"same_version_runner_environment_verifier_user": False},
            "approved_under_other_binding",
        ),
    ],
)
def test_e5_counts_only_clean_authenticated_human_approvals(change, reason):
    assert _e5(_streak(**change)).reason == reason


def test_e5_ignores_items_from_before_the_last_switch_off():
    items = _streak()
    items[0] = _approval(0, "user-lee", after_last_switch_off=False)
    assert _e5(items).reason == "too_few_approved_items"


def test_e5_needs_two_different_approvers():
    items = [_approval(i, "user-dana") for i in range(20)]
    assert _e5(items).reason == "too_few_approvers"


def test_e5_uses_the_most_recent_twenty():
    old_declined = [_approval(100, "user-lee", declined=True)]
    assert _e5(old_declined + _streak()).met


# -- E6 to E8 ---------------------------------------------------------------------


def test_e6_runner_setup_and_signed_in_user_check():
    setup = RunnerSetupEvidence(current=False, signed_in_user_check_armed=True)
    assert _unmet(_workflow(), _all_met(runner_setup=setup)).reason == (
        "runner_setup_not_current"
    )
    setup = RunnerSetupEvidence(current=True, signed_in_user_check_armed=False)
    assert _unmet(_workflow(), _all_met(runner_setup=setup)).reason == (
        "signed_in_user_check_not_armed"
    )


def test_e7_channel_and_values_source():
    assert _unmet(_workflow(), _all_met(channel="cloud_schedule")).reason == (
        "channel_not_covered"
    )
    person = _all_met().person.model_copy(update={"values_source": "partner_system"})
    assert _unmet(_workflow(), _all_met(person=person)).reason == (
        "partner_values_not_acknowledged"
    )
    person = person.model_copy(
        update={"partner_values_acknowledged_by_privacy_contact": True}
    )
    assert evaluate_requirements(_workflow(), _all_met(person=person)).all_met


def test_e8_someone_answers_problems():
    assert _unmet(_workflow(), _all_met(stop_answered_in_cloud=False)).reason == (
        "no_stop_answered_in_cloud"
    )
    person = _all_met().person.model_copy(update={"privacy_contact_named": False})
    assert _unmet(_workflow(), _all_met(person=person)).reason == (
        "privacy_contact_not_named"
    )
