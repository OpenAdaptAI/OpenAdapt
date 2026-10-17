"""Requirements E1 to E8 for "Only ask a person about problems" (design 6).

:func:`evaluate_requirements` is a pure function from the workflow and
PHI-free evidence summaries to the list of requirements, in order, each met
or not with a closed reason code. Cloud's API and both apps read this one
list; the customer sees the first unmet requirement.

Some requirements are decisions people make (readiness sign-off, runner setup
attestations, the privacy contact, the source of the values). This module
only reads those decisions as recorded booleans; it never makes them, and
every such field defaults to "not recorded", which fails its requirement.

Two requirements can't be met yet by any workflow, on purpose:

- E2 needs a field-delta check for updates. That effect kind isn't built,
  so a save without an ``exact_new_set`` (a create) is reported as
  ``update_needs_field_delta_check``.
- E4 needs three new drills (typed into the wrong field, another field
  changed, nothing written at the first gate). Until they exist they can't
  run, and a drill that couldn't run hasn't passed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import Enum
from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.approval_mode.model import (
    CODE_PATTERN,
    DESKTOP_LIST_WITH_HEADER,
    HEX_SHA256_PATTERN,
    OPAQUE_ID_PATTERN,
    PREFIXED_SHA256_PATTERN,
)

if TYPE_CHECKING:
    from openadapt_flow.ir import Step, Workflow


class RequirementCode(str, Enum):
    E1_LOCKED_VERSION = "E1"
    E2_RECORD_CHECK = "E2"
    E3_NO_IRREVERSIBLE_WRITES = "E3"
    E4_READINESS_TEST = "E4"
    E5_APPROVAL_STREAK = "E5"
    E6_RUNNER_SETUP = "E6"
    E7_VALUES_SOURCE = "E7"
    E8_SOMEONE_ANSWERS = "E8"


CheckedBy = Literal["software", "person", "software_and_person"]


class RequirementResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: RequirementCode
    met: bool
    #: Closed reason code for the first thing that's missing, or ``ok``.
    reason: str = Field(pattern=CODE_PATTERN)
    checked_by: CheckedBy
    #: Step ids the reason applies to, when it is about specific steps.
    step_ids: tuple[str, ...] = ()


class RequirementsReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirements: tuple[RequirementResult, ...]

    @property
    def all_met(self) -> bool:
        return all(item.met for item in self.requirements)

    @property
    def first_unmet(self) -> Optional[RequirementResult]:
        return next((item for item in self.requirements if not item.met), None)


# -- evidence summaries (PHI-free) ---------------------------------------------

#: Built-in system-of-record verifier kinds that qualify without more review.
BUILT_IN_VERIFIER_KINDS = frozenset({"rest", "graphql", "fhir", "sql"})
FILE_VERIFIER_KINDS = frozenset({"file", "document_hash"})


class VerifierEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "rest",
        "graphql",
        "fhir",
        "sql",
        "file",
        "document_hash",
        "plugin",
        "onscreen",
        "other",
    ]
    verifier_identity: Optional[str] = Field(
        default=None, pattern=PREFIXED_SHA256_PATTERN
    )
    #: Digest of the recorded code review of a plugin verifier.
    plugin_review_sha256: Optional[str] = Field(
        default=None, pattern=PREFIXED_SHA256_PATTERN
    )
    #: The latest read-only ``test_connection()`` result.
    connection_ok: bool = False


class DeploymentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Certified and sealed ("Check it" approved) for this exact version.
    certified_and_sealed: bool = False
    admission_active: bool = False
    profile: Optional[Literal["demo", "standard", "regulated"]] = None
    model_grounding_allowed: bool = True
    environment_observer_bound: bool = False
    policy_name: Optional[str] = None
    policy_pack: Optional[str] = None
    minimum_effect_tier: Optional[int] = Field(default=None, ge=1, le=4)


DrillName = Literal[
    "wrong_record",
    "two_matches",
    "record_changed_mid_task",
    "save_did_not_land",
    "weak_record_check",
    "typed_into_wrong_field",
    "another_field_changed",
    "nothing_written_at_first_gate",
]
REQUIRED_DRILLS: tuple[str, ...] = (
    "wrong_record",
    "two_matches",
    "record_changed_mid_task",
    "save_did_not_land",
    "weak_record_check",
    "typed_into_wrong_field",
    "another_field_changed",
    "nothing_written_at_first_gate",
)
#: Proposed minimum number of clinic-supplied must-stop cases.
MIN_CUSTOMER_STOP_CASES = 3
_STOP_OUTCOMES = frozenset({"HALTED_BEFORE_EFFECT", "REJECTED_POLICY"})


class ReadinessEvidence(BaseModel):
    """The readiness test on this version, runner, environment and verifier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    practice_case_transaction_outcome: Optional[str] = None
    #: Who performed the practice case's read-back. Only the deployment's own
    #: verifier counts for E4(a); a test-setup read-back never does.
    practice_case_read_back_by: Optional[
        Literal["deployment_verifier", "test_setup"]
    ] = None
    drills: dict[DrillName, Literal["as_agreed", "not_as_agreed", "not_run"]] = Field(
        default_factory=dict
    )
    #: Transaction outcome of each clinic-supplied must-stop case.
    customer_stop_cases: tuple[Optional[str], ...] = ()
    whole_item_snapshot_clean: Optional[bool] = None
    #: E4(c), judged by a second source and labeled with it.
    no_silent_incorrect_success: Optional[bool] = None
    no_silent_incorrect_success_judged_by: Optional[
        Literal["test_setup_read_back", "person_comparison"]
    ] = None
    production_write_authorization_sha256: Optional[str] = Field(
        default=None, pattern=PREFIXED_SHA256_PATTERN
    )
    #: Recorded sign-off by a person at the clinic in Check it at AAL2.
    #: Software reads it; it never sets it.
    clinic_sign_off_recorded: bool = False


class ApprovalReceiptEvidence(BaseModel):
    """One item under per-item approval, for the approval streak (E5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_ref: str = Field(pattern=HEX_SHA256_PATTERN)
    transaction_outcome: Optional[str] = None
    whole_run_clean: bool = False
    #: The item's saves were read back by the E2 record check.
    read_back_by_record_check: bool = False
    declined: bool = False
    decided_by: Literal["human", "automation", "unknown"] = "unknown"
    approver_cloud_user_id: Optional[str] = Field(
        default=None, pattern=OPAQUE_ID_PATTERN
    )
    authentication: Literal["aal2", "aal1", "none"] = "none"
    route: Literal["desktop_run_it", "console", "phone", "cloud", "other"] = "other"
    approver_is_runner_account: bool = True
    after_last_switch_off: bool = False
    same_version_runner_environment_verifier_user: bool = False


#: Proposed streak length and minimum number of different approvers.
STREAK_LENGTH = 20
MIN_APPROVERS = 2


class RunnerSetupEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    current: bool = False
    signed_in_user_check_armed: bool = False
    #: Attestation 7: the records system, not the runner, produces the file.
    file_source_attested: bool = False


class PersonDecisions(BaseModel):
    """Decisions people record. Software reads them; it never makes them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    no_sign_attest_or_order: bool = False
    nothing_sent_outside_clinic: bool = False
    no_downstream_action_before_read_back: bool = False
    values_source: Optional[
        Literal["clinic_staff", "clinic_system", "partner_system"]
    ] = None
    partner_values_acknowledged_by_privacy_contact: bool = False
    privacy_contact_named: bool = False


class RequirementsEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment: DeploymentEvidence = Field(default_factory=DeploymentEvidence)
    verifiers: tuple[VerifierEvidence, ...] = ()
    readiness: ReadinessEvidence = Field(default_factory=ReadinessEvidence)
    #: Items under per-item approval, oldest first.
    approvals: tuple[ApprovalReceiptEvidence, ...] = ()
    runner_setup: RunnerSetupEvidence = Field(default_factory=RunnerSetupEvidence)
    person: PersonDecisions = Field(default_factory=PersonDecisions)
    channel: Optional[str] = None
    #: A person signed in to Cloud has answered at least one stop in Needs you
    #: in this environment.
    stop_answered_in_cloud: bool = False


# -- the evaluator ---------------------------------------------------------------

_FORBIDDEN_WRITE_RE = re.compile(
    r"\b(?:delet(?:e|es|ing)|remov(?:e|es|ing)|send(?:s|ing)?|sent|"
    r"submit(?:s|ted|ting)?|sign(?:s|ed|ing)?|attest(?:s|ed|ing)?|"
    r"order(?:s|ed|ing)?|transmit(?:s|ted|ting)?|fax(?:es|ed|ing)?|"
    r"e-?mail(?:s|ed|ing)?|publish(?:es|ed|ing)?|pay(?:s|ing)?)\b",
    re.IGNORECASE,
)


def _forbidden_write(step: "Step") -> bool:
    """Deleting, sending, submitting, signing, attesting or ordering."""

    from openadapt_flow.risk import step_text

    return bool(_FORBIDDEN_WRITE_RE.search(step_text(step)))


def _result(
    code: RequirementCode,
    reason: Optional[str],
    checked_by: CheckedBy,
    step_ids: Sequence[str] = (),
) -> RequirementResult:
    return RequirementResult(
        code=code,
        met=reason is None,
        reason=reason or "ok",
        checked_by=checked_by,
        step_ids=tuple(step_ids),
    )


def _e1(evidence: RequirementsEvidence) -> RequirementResult:
    deployment = evidence.deployment
    reason: Optional[str] = None
    if not deployment.certified_and_sealed:
        reason = "not_certified_and_sealed"
    elif not deployment.admission_active:
        reason = "no_active_admission"
    elif deployment.profile != "regulated":
        reason = "profile_not_regulated"
    elif deployment.model_grounding_allowed:
        reason = "model_egress_allowed"
    elif not deployment.environment_observer_bound:
        reason = "no_environment_observer"
    return _result(RequirementCode.E1_LOCKED_VERSION, reason, "software")


def _e2(workflow: "Workflow", evidence: RequirementsEvidence) -> RequirementResult:
    from openadapt_flow.approval_mode.coverage import workflow_coverage
    from openadapt_flow.policy import effect_has_idempotency_key, iter_effect_paths
    from openadapt_flow.run_gate import is_consequential
    from openadapt_flow.runtime.effects.effect import EffectKind

    code = RequirementCode.E2_RECORD_CHECK
    deployment = evidence.deployment
    if deployment.policy_pack != "regulated" or deployment.minimum_effect_tier != 1:
        return _result(code, "not_qualified_with_regulated_pack", "software")
    if deployment.policy_name != "clinical-write":
        return _result(code, "not_under_clinical_write", "software")
    if not evidence.verifiers:
        return _result(code, "no_record_check", "software")
    for verifier in evidence.verifiers:
        if verifier.verifier_identity is None:
            return _result(code, "verifier_identity_missing", "software")
        if verifier.kind in BUILT_IN_VERIFIER_KINDS:
            pass
        elif verifier.kind in FILE_VERIFIER_KINDS:
            if not evidence.runner_setup.file_source_attested:
                return _result(code, "file_source_not_attested", "software_and_person")
        elif verifier.kind == "plugin":
            if verifier.plugin_review_sha256 is None:
                return _result(code, "plugin_not_reviewed", "software")
        else:
            return _result(code, "verifier_kind_not_allowed", "software")
        if not verifier.connection_ok:
            return _result(code, "record_check_unreachable", "software")
    rows = workflow_coverage(workflow)
    if not rows:
        return _result(code, "no_save_to_check", "software")
    uncovered = [row for row in rows if not row.covered]
    if uncovered:
        return _result(
            code,
            uncovered[0].problems[0],
            "software",
            [row.step_id for row in uncovered],
        )
    saves = [step for step in workflow.steps if is_consequential(step, workflow)]
    missing_key = [step.id for step in saves if not effect_has_idempotency_key(step)]
    if missing_key:
        return _result(code, "write_without_idempotency_key", "software", missing_key)
    updates = [
        step.id
        for step in saves
        if not all(
            any(effect.kind is EffectKind.EXACT_NEW_SET for effect in effects)
            for _path, effects in iter_effect_paths(step)
        )
    ]
    if updates:
        return _result(code, "update_needs_field_delta_check", "software", updates)
    return _result(code, None, "software")


_ACTUATING = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "drag",
        "type",
        "select_option",
        "key",
        "hotkey",
    }
)


def _e3(workflow: "Workflow", evidence: RequirementsEvidence) -> RequirementResult:
    from openadapt_flow.policy import (
        all_effect_paths_covered,
        has_operator_risk_override,
    )
    from openadapt_flow.qualification import ActionRiskClass
    from openadapt_flow.risk import classify_step_risk
    from openadapt_flow.run_gate import is_consequential

    code = RequirementCode.E3_NO_IRREVERSIBLE_WRITES
    if workflow.program is not None:
        return _result(code, "program_graph", "software")
    project = workflow.qualification
    lowered_by_class: list[str] = []
    lowered_override: list[str] = []
    forbidden: list[str] = []
    for step in workflow.steps:
        if step.action.value not in _ACTUATING:
            continue
        classification = (
            project.action_classifications.get(step.id) if project is not None else None
        )
        if (
            classification is not None
            and classification.classification
            in (ActionRiskClass.READ_ONLY, ActionRiskClass.STATE_CHANGING)
            and not all_effect_paths_covered(step)
        ):
            lowered_by_class.append(step.id)
        if (
            classify_step_risk(step) == "irreversible"
            and step.risk != "irreversible"
            and has_operator_risk_override(step, workflow)
        ):
            lowered_override.append(step.id)
        if is_consequential(step, workflow) and _forbidden_write(step):
            forbidden.append(step.id)
    if lowered_by_class:
        return _result(
            code, "unchecked_step_classified_harmless", "software", lowered_by_class
        )
    if lowered_override:
        return _result(
            code, "write_classification_lowered", "software", lowered_override
        )
    if forbidden:
        return _result(code, "irreversible_write", "software", forbidden)
    person = evidence.person
    if not person.no_sign_attest_or_order:
        return _result(code, "sign_attest_order_not_recorded", "person")
    if not person.nothing_sent_outside_clinic:
        return _result(code, "outside_sending_not_recorded", "person")
    if not person.no_downstream_action_before_read_back:
        return _result(code, "downstream_action_not_recorded", "person")
    return _result(code, None, "software_and_person")


def _e4(evidence: RequirementsEvidence) -> RequirementResult:
    code = RequirementCode.E4_READINESS_TEST
    readiness = evidence.readiness
    if readiness.production_write_authorization_sha256 is None:
        return _result(code, "production_write_authorization_missing", "person")
    if readiness.practice_case_transaction_outcome != "VERIFIED":
        return _result(code, "practice_case_not_verified", "software")
    if readiness.practice_case_read_back_by != "deployment_verifier":
        # The test setup's read-back counts for E4(c) only.
        return _result(code, "practice_case_not_read_back_by_deployment", "software")
    for drill in REQUIRED_DRILLS:
        verdict = readiness.drills.get(drill, "not_run")  # type: ignore[call-overload]
        if verdict == "not_run":
            return _result(code, f"drill_not_run_{drill}", "software")
        if verdict != "as_agreed":
            return _result(code, f"drill_failed_{drill}", "software")
    if readiness.whole_item_snapshot_clean is not True:
        return _result(code, "write_outside_gated_steps", "software")
    stops = readiness.customer_stop_cases
    if len(stops) < MIN_CUSTOMER_STOP_CASES:
        return _result(code, "too_few_must_stop_cases", "software")
    if any(outcome not in _STOP_OUTCOMES for outcome in stops):
        return _result(code, "must_stop_case_saved", "software")
    if (
        readiness.no_silent_incorrect_success is not True
        or readiness.no_silent_incorrect_success_judged_by is None
    ):
        return _result(code, "silent_incorrect_success_not_ruled_out", "software")
    if not readiness.clinic_sign_off_recorded:
        return _result(code, "clinic_sign_off_missing", "person")
    return _result(code, None, "software_and_person")


def _e5(evidence: RequirementsEvidence) -> RequirementResult:
    code = RequirementCode.E5_APPROVAL_STREAK
    eligible = [item for item in evidence.approvals if item.after_last_switch_off]
    recent = eligible[-STREAK_LENGTH:]
    if len(recent) < STREAK_LENGTH:
        return _result(code, "too_few_approved_items", "software")
    for item in recent:
        if item.declined:
            return _result(code, "declined_item", "software")
        if (
            item.transaction_outcome != "VERIFIED"
            or not item.read_back_by_record_check
            or not item.whole_run_clean
        ):
            return _result(code, "approved_item_not_clean", "software")
        if not item.same_version_runner_environment_verifier_user:
            return _result(code, "approved_under_other_binding", "software")
        if (
            item.decided_by != "human"
            or item.authentication != "aal2"
            or item.route != "desktop_run_it"
            or item.approver_cloud_user_id is None
            or item.approver_is_runner_account
        ):
            return _result(code, "approval_not_authenticated_person", "software")
    approvers = {item.approver_cloud_user_id for item in recent}
    if len(approvers) < MIN_APPROVERS:
        return _result(code, "too_few_approvers", "software")
    return _result(code, None, "software")


def _e6(evidence: RequirementsEvidence) -> RequirementResult:
    code = RequirementCode.E6_RUNNER_SETUP
    if not evidence.runner_setup.current:
        return _result(code, "runner_setup_not_current", "software_and_person")
    if not evidence.runner_setup.signed_in_user_check_armed:
        return _result(code, "signed_in_user_check_not_armed", "software")
    return _result(code, None, "software_and_person")


def _e7(evidence: RequirementsEvidence) -> RequirementResult:
    code = RequirementCode.E7_VALUES_SOURCE
    if evidence.channel != DESKTOP_LIST_WITH_HEADER:
        return _result(code, "channel_not_covered", "software")
    source = evidence.person.values_source
    if source is None:
        return _result(code, "values_source_not_recorded", "person")
    if (
        source == "partner_system"
        and not evidence.person.partner_values_acknowledged_by_privacy_contact
    ):
        return _result(code, "partner_values_not_acknowledged", "person")
    return _result(code, None, "software_and_person")


def _e8(evidence: RequirementsEvidence) -> RequirementResult:
    code = RequirementCode.E8_SOMEONE_ANSWERS
    if not evidence.stop_answered_in_cloud:
        return _result(code, "no_stop_answered_in_cloud", "software")
    if not evidence.person.privacy_contact_named:
        return _result(code, "privacy_contact_not_named", "person")
    return _result(code, None, "software_and_person")


def evaluate_requirements(
    workflow: "Workflow", evidence: RequirementsEvidence
) -> RequirementsReport:
    """Return E1 to E8, in order, for one workflow version on one runner."""

    return RequirementsReport(
        requirements=(
            _e1(evidence),
            _e2(workflow, evidence),
            _e3(workflow, evidence),
            _e4(evidence),
            _e5(evidence),
            _e6(evidence),
            _e7(evidence),
            _e8(evidence),
        )
    )
