"""Tests for approver identity, approver policy and the read-back ladder."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types import (
    ApprovalModeV1,
    ApprovalRecordV1,
    ApproverAuth,
    ApproverIdentityV1,
    ApproverKind,
    ApproverPolicyRefusal,
    ApproverPolicyV1,
    AutonomyEligibilityProvider,
    AutonomyEligibilityV1,
    EffectStrengthV1,
    EffectVerificationTier,
    LegacyApproverRefused,
    OracleTier,
    ReadBackOutcome,
    ReadBackTier,
    StepClass,
    approver_from_legacy,
    counts_as_clean,
    is_training_label,
    per_item_review_reason,
    read_back_tier,
    resolve_approval_mode_v1,
)

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64

PERSON = ApproverIdentityV1(
    kind=ApproverKind.PERSON,
    auth=ApproverAuth.CLOUD_USER,
    id="user_123",
    auth_method="cloud_aal2",
)
AGENT = ApproverIdentityV1(
    kind=ApproverKind.AGENT, auth=ApproverAuth.MCP_SESSION, id="agent:approver-bot"
)
RULE = ApproverIdentityV1(
    kind=ApproverKind.RULE,
    auth=ApproverAuth.POLICY,
    id="rule:amount-under-500",
    policy_sha256=DIGEST,
)
SYSTEM = ApproverIdentityV1(
    kind=ApproverKind.SYSTEM, auth=ApproverAuth.SIGNED_WEBHOOK, id="sys:workflow-tool"
)


# -- identity -----------------------------------------------------------------


def test_every_kind_has_a_valid_identity() -> None:
    assert {a.kind for a in (PERSON, AGENT, RULE, SYSTEM)} == set(ApproverKind)


def test_unauthenticated_person_names_no_one() -> None:
    ApproverIdentityV1(kind=ApproverKind.PERSON, auth=ApproverAuth.UNAUTHENTICATED)
    with pytest.raises(ValidationError):
        ApproverIdentityV1(
            kind=ApproverKind.PERSON, auth=ApproverAuth.UNAUTHENTICATED, id="user_1"
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "agent", "auth": "mcp_session"},  # no id
        {"kind": "agent", "auth": "cloud_user", "id": "a", "auth_method": "cloud_aal1"},
        {"kind": "rule", "auth": "policy", "id": "r"},  # no policy digest
        {"kind": "person", "auth": "cloud_user", "id": "u"},  # no sign-in method
        {
            "kind": "system",
            "auth": "signed_webhook",
            "id": "s",
            "policy_sha256": DIGEST,
        },
        {"kind": "person", "auth": "local_cli", "id": "u", "on_behalf_of": "org"},
        {"kind": "agent", "auth": "mcp_session", "id": "Robert Smith"},  # not opaque
    ],
)
def test_inconsistent_identities_are_refused(fields: dict) -> None:
    with pytest.raises(ValidationError):
        ApproverIdentityV1(**fields)


def test_only_a_person_is_a_training_label() -> None:
    assert is_training_label(PERSON)
    assert not any(is_training_label(a) for a in (AGENT, RULE, SYSTEM))


# -- legacy mapping -------------------------------------------------------------


def test_legacy_cloud_user_maps_exactly() -> None:
    mapped = approver_from_legacy(
        decided_by="human",
        approver_kind="cloud_user",
        approver_id="user_123",
        auth_method="cloud_aal2",
    )
    assert mapped == PERSON
    assert mapped.legacy_decided_by() == "human"
    assert mapped.legacy_approver_kind() == "cloud_user"


def test_legacy_unauthenticated_maps_exactly() -> None:
    mapped = approver_from_legacy(decided_by="human", approver_kind="unauthenticated")
    assert mapped.kind is ApproverKind.PERSON
    assert mapped.legacy_approver_kind() == "unauthenticated"


def test_legacy_automation_needs_a_named_system() -> None:
    with pytest.raises(LegacyApproverRefused):
        approver_from_legacy(decided_by="automation")
    mapped = approver_from_legacy(decided_by="automation", system_id="sys:x")
    assert mapped.kind is ApproverKind.SYSTEM
    assert mapped.legacy_decided_by() == "automation"


@pytest.mark.parametrize("value", ["unknown", "", "agent"])
def test_legacy_unknown_is_refused(value: str) -> None:
    with pytest.raises(LegacyApproverRefused):
        approver_from_legacy(decided_by=value)


def test_non_person_has_no_legacy_approver_kind() -> None:
    with pytest.raises(ValueError):
        AGENT.legacy_approver_kind()


# -- modes ------------------------------------------------------------------------


def test_existing_mode_values_keep_working() -> None:
    assert ApprovalModeV1("each_item") is ApprovalModeV1.EACH_ITEM
    assert ApprovalModeV1("problems_only") is ApprovalModeV1.PROBLEMS_ONLY
    assert ApprovalModeV1("not_gated") is ApprovalModeV1.NOT_GATED
    assert ApprovalModeV1.EXCEPTIONS_ONLY is ApprovalModeV1.PROBLEMS_ONLY


@pytest.mark.parametrize("value", [None, "", "auto", 3, "EACH_ITEM"])
def test_unknown_mode_means_each_item(value: object) -> None:
    assert resolve_approval_mode_v1(value) is ApprovalModeV1.EACH_ITEM


# -- policy -----------------------------------------------------------------------


def test_default_policy_is_person_only() -> None:
    policy = ApproverPolicyV1()
    for step_class in StepClass:
        assert policy.permits(step_class, PERSON)
        for other in (AGENT, RULE, SYSTEM):
            assert not policy.permits(step_class, other)


def test_policy_can_allow_an_agent_and_still_forbid_it_for_clinical_writes() -> None:
    allowed = {c: (ApproverKind.PERSON,) for c in StepClass}
    allowed[StepClass.CONSEQUENTIAL] = (ApproverKind.PERSON, ApproverKind.AGENT)
    policy = ApproverPolicyV1(allowed=allowed)
    assert policy.permits(StepClass.CONSEQUENTIAL, AGENT)
    assert policy.refusal(StepClass.IRREVERSIBLE, AGENT) is (
        ApproverPolicyRefusal.KIND_NOT_ALLOWED
    )
    assert policy.refusal(StepClass.CLINICAL_WRITE, AGENT) is (
        ApproverPolicyRefusal.CLINICAL_WRITE_NEEDS_OPT_IN
    )


def test_clinical_write_non_person_needs_an_opt_in() -> None:
    allowed = {c: (ApproverKind.PERSON,) for c in StepClass}
    allowed[StepClass.CLINICAL_WRITE] = (ApproverKind.PERSON, ApproverKind.AGENT)
    with pytest.raises(ValidationError):
        ApproverPolicyV1(allowed=allowed)
    policy = ApproverPolicyV1(allowed=allowed, clinical_write_opt_in_sha256=DIGEST)
    assert policy.permits(StepClass.CLINICAL_WRITE, AGENT)


def test_policy_must_name_every_step_class() -> None:
    with pytest.raises(ValidationError):
        ApproverPolicyV1(allowed={StepClass.READ_ONLY: (ApproverKind.PERSON,)})


# -- read-back ladder ---------------------------------------------------------------


def test_only_confirmed_independent_system_read_back_is_clean() -> None:
    clean = [
        (tier, outcome)
        for tier in ReadBackTier
        for outcome in ReadBackOutcome
        if counts_as_clean(tier, outcome)
    ]
    assert clean == [(ReadBackTier.INDEPENDENT_SYSTEM, ReadBackOutcome.CONFIRMED)]


@pytest.mark.parametrize(
    ("tier", "outcome", "reason"),
    [
        (
            ReadBackTier.SCREEN_READ_BACK,
            ReadBackOutcome.CONFIRMED,
            "screen_read_back_only",
        ),
        (ReadBackTier.UNCHECKED, ReadBackOutcome.NONE, "not_checked"),
        (ReadBackTier.INDEPENDENT_SYSTEM, ReadBackOutcome.REFUTED, "read_back_refuted"),
        (
            ReadBackTier.INDEPENDENT_SYSTEM,
            ReadBackOutcome.INDETERMINATE,
            "read_back_indeterminate",
        ),
    ],
)
def test_review_reasons(tier, outcome, reason) -> None:
    assert per_item_review_reason(tier, outcome) == reason


def test_screen_read_back_is_never_clean_even_when_the_minimum_allows_it() -> None:
    assert (
        per_item_review_reason(
            ReadBackTier.SCREEN_READ_BACK,
            ReadBackOutcome.CONFIRMED,
            minimum=ReadBackTier.SCREEN_READ_BACK,
        )
        == "screen_read_back_only"
    )


def test_below_minimum() -> None:
    assert (
        per_item_review_reason(
            ReadBackTier.SCREEN_READ_BACK,
            ReadBackOutcome.CONFIRMED,
            minimum=ReadBackTier.INDEPENDENT_SYSTEM,
        )
        == "below_minimum_tier"
    )


@pytest.mark.parametrize(
    ("strength", "tier"),
    [
        (
            EffectStrengthV1.INDEPENDENT_SYSTEM_OF_RECORD,
            ReadBackTier.INDEPENDENT_SYSTEM,
        ),
        (EffectStrengthV1.INDEPENDENT_SESSION, ReadBackTier.SCREEN_READ_BACK),
        (EffectStrengthV1.PERSISTED_STATE_REACQUISITION, ReadBackTier.SCREEN_READ_BACK),
        (EffectStrengthV1.IMMEDIATE_SCREEN_CONFIRMATION, ReadBackTier.SCREEN_READ_BACK),
        (
            EffectVerificationTier.INDEPENDENT_SYSTEM_INTERFACE,
            ReadBackTier.INDEPENDENT_SYSTEM,
        ),
        (EffectVerificationTier.INDEPENDENT_SESSION, ReadBackTier.SCREEN_READ_BACK),
        (
            EffectVerificationTier.IMMEDIATE_SCREEN_CONFIRMATION,
            ReadBackTier.SCREEN_READ_BACK,
        ),
        (OracleTier.SYSTEM_OF_RECORD, ReadBackTier.INDEPENDENT_SYSTEM),
        (OracleTier.VISUAL, ReadBackTier.SCREEN_READ_BACK),
        ("independent_system", ReadBackTier.INDEPENDENT_SYSTEM),
        ("immediate_screen", ReadBackTier.SCREEN_READ_BACK),
        (None, ReadBackTier.UNCHECKED),
        ("made_up", ReadBackTier.UNCHECKED),
        (4, ReadBackTier.UNCHECKED),
    ],
)
def test_read_back_tier_mapping(strength, tier) -> None:
    assert read_back_tier(strength) is tier


# -- record -------------------------------------------------------------------------


def _record(**overrides) -> ApprovalRecordV1:
    fields = dict(
        workflow_id="wf_1",
        step_id="save",
        step_class=StepClass.CONSEQUENTIAL,
        mode=ApprovalModeV1.EACH_ITEM,
        decision="approved",
        approver=PERSON,
        route="desktop_run_it",
        request_sha256=DIGEST,
        decided_at="2026-10-17T04:00:00Z",
    )
    fields.update(overrides)
    return ApprovalRecordV1(**fields)


def test_record_round_trips_and_marks_training_label() -> None:
    record = _record()
    assert ApprovalRecordV1.model_validate_json(record.model_dump_json()) == record
    assert record.training_label
    assert not _record(approver=AGENT, route="mcp").training_label


@pytest.mark.parametrize(
    "overrides",
    [
        {"decision": "declined"},
        {"decline_reason": "wrong_values"},
        {"mode": ApprovalModeV1.NOT_GATED},
        {"route": "review_batch"},
        {"batch_sha256": OTHER},
        {"approver": RULE},
        {"route": "rule"},
    ],
)
def test_inconsistent_records_are_refused(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        _record(**overrides)


def test_batch_record() -> None:
    record = _record(
        mode=ApprovalModeV1.BATCH, route="review_batch", batch_sha256=OTHER
    )
    assert record.batch_sha256 == OTHER


# -- eligibility ----------------------------------------------------------------------


def _eligibility(**overrides) -> AutonomyEligibilityV1:
    fields = dict(
        workflow_id="wf_1",
        calibration_artifact_sha256=DIGEST,
        verified_runs=400,
        audited_items=120,
        audit_misses=0,
        audit_miss_rate_upper_bound=0.025,
        target_miss_rate=0.05,
        audit_fraction=0.1,
        evaluated_at="2026-10-17T04:00:00Z",
    )
    fields.update(overrides)
    return AutonomyEligibilityV1(**fields)


def test_eligibility() -> None:
    assert _eligibility().eligible
    assert not _eligibility(audit_miss_rate_upper_bound=0.06).eligible
    assert not _eligibility(audited_items=0).eligible
    with pytest.raises(ValidationError):
        _eligibility(audit_fraction=0.005)
    with pytest.raises(ValidationError):
        _eligibility(audit_misses=121)


def test_provider_protocol() -> None:
    class Provider:
        def eligibility(self, workflow_id: str) -> AutonomyEligibilityV1:
            return _eligibility(workflow_id=workflow_id)

    assert isinstance(Provider(), AutonomyEligibilityProvider)


# -- packaged schemas -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "filename"),
    [
        (ApproverIdentityV1, "approver-identity-v1.json"),
        (ApproverPolicyV1, "approver-policy-v1.json"),
        (ApprovalRecordV1, "approval-record-v1.json"),
        (AutonomyEligibilityV1, "autonomy-eligibility-v1.json"),
    ],
)
def test_packaged_schemas_match_models(model: object, filename: str) -> None:
    packaged = json.loads(
        files("openadapt_types.schemas").joinpath(filename).read_text(encoding="utf-8")
    )
    assert packaged == model.model_json_schema()
