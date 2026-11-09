"""openadapt.verify/v1 parsing and openadapt.qualification-result/v1."""

from __future__ import annotations

import json
from base64 import b64decode
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.qualification_admission import (
    SIGNATURE_DOMAIN,
    QualificationAdmissionEnvelope,
    QualificationAdmissionError,
    expected_from_payload,
    verify_qualification_admission,
)
from openadapt_flow.qualification_dev_signer import (
    LOCAL_DEV_PRIVATE_KEY,
    local_dev_signer_trust,
)
from openadapt_flow.qualify import QualifyConfigError, parse_verify_config
from openadapt_flow.qualify.result import (
    build_result,
    case_entry,
    local_admission,
    result_sha256,
    verify_config_sha256,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr
from openadapt_flow.verification import VerificationTier

H = "b" * 64
BASE = {
    "schema_version": "openadapt.verify/v1",
    "app": {"name": "Demo", "version": "1", "url": "http://127.0.0.1:1/"},
    "verifier": {
        "kind": "rest",
        "base_url": "http://127.0.0.1:2",
        "records_path": "/r",
    },
}


def test_pre_rfc_shape_gets_a_migration_hint():
    with pytest.raises(QualifyConfigError, match="schema_version: openadapt.verify/v1"):
        parse_verify_config(
            {"version": 1, "app": BASE["app"], "system_of_record": BASE["verifier"]},
            env={},
        )


def test_effects_list_is_the_effect_contract():
    config = parse_verify_config(
        {
            **BASE,
            "minimum_effect_strength": "independent_session",
            "effects": [
                {
                    "kind": "record_written",
                    "match": {"note": {"param": "note"}},
                    "expected_count": 1,
                    "count_new_only": True,
                }
            ],
        },
        env={},
    )
    assert config.effects[0].kind is EffectKind.RECORD_WRITTEN
    assert config.minimum_effect_strength == "independent_session"


def test_effects_and_record_are_exclusive():
    with pytest.raises(QualifyConfigError, match="either effects or record"):
        parse_verify_config(
            {
                **BASE,
                "effects": [{"kind": "record_written", "match": {"a": {"param": "a"}}}],
                "record": {"match": ["a"], "fields": {"a": "a"}},
            },
            env={},
        )


def test_verify_config_sha256_binds_identity_and_effects():
    effect = Effect(kind=EffectKind.RECORD_WRITTEN, match={"a": ValueExpr(param="a")})
    one = verify_config_sha256("sha256:" + H, [effect])
    assert one.startswith("sha256:") and len(one) == 71
    assert one != verify_config_sha256("sha256:" + "c" * 64, [effect])
    other = effect.model_copy(update={"expected_count": 2})
    assert one != verify_config_sha256("sha256:" + H, [other])


def _report(outcome: str, success: bool) -> SimpleNamespace:
    return SimpleNamespace(execution_outcome=outcome, success=success, model_calls=0)


def _entry(
    tmp_path: Path,
    case_id: str,
    outcome: str,
    oracle: dict,
    trial=1,
    tier=VerificationTier.INDEPENDENT_SYSTEM,
    substrate="rest",
):
    path = tmp_path / f"{case_id}-{trial}.json"
    path.write_text("{}")
    return case_entry(
        case_id=case_id,
        trial=trial,
        report=_report(outcome, outcome == "VERIFIED"),
        oracle=oracle,
        verifier_tier=tier,
        report_path=path,
        verifier_substrate=substrate,
    )


EXACT = {
    "observed": "exact_record",
    "independent": True,
    "effect_verdict": "CONFIRMED",
    "landed": "exact",
}
NONE = {
    "observed": "no_mutation",
    "independent": True,
    "effect_verdict": "REFUTED",
    "landed": "none",
}


def test_case_entries_map_read_back_to_verdicts(tmp_path):
    good = _entry(tmp_path, "representative", "VERIFIED", EXACT)
    assert good["observed_verdict"] == "CONFIRMED" and good["outcome"] == "verified"
    assert good["passed"] and not good["silent_incorrect_success"]

    halted = _entry(tmp_path, "fault-ambiguity", "HALTED", NONE)
    assert halted["expected_verdict"] == halted["observed_verdict"] == "REFUTED"
    assert halted["outcome"] == "halted_before_effect" and halted["passed"]

    # A fault the engine let through: the record landed.
    leaked = _entry(tmp_path, "fault-wrong-identity", "VERIFIED", EXACT)
    assert leaked["silent_incorrect_success"] and not leaked["passed"]

    over = _entry(tmp_path, "representative", "HALTED", NONE)
    assert over["over_halt"] and not over["passed"]

    screen = _entry(
        tmp_path, "representative", "VERIFIED", {**EXACT, "independent": False}
    )
    assert screen["verification_tier"] == "unchecked" and not screen["passed"]


def _qreport(passed=True):
    return SimpleNamespace(
        model_dump=lambda **_: {
            "schema_version": "openadapt.qualification-report/v1",
            "workflow_name": "Register Jane Doe",
            "workflow_contract_sha256": H,
            "environment_contract_sha256": H,
            "project_id": "p",
            "project_revision": 2,
            "policy_name": "clinical-write",
            "passed": passed,
            "case_count": 6,
            "passed_case_count": 6,
            "refusals": [
                {"code": "case_not_passed", "path": "cases", "message": "free text"}
            ],
            "generated_at": "2026-10-17T05:00:00Z",
        }
    )


def _result(tmp_path, trials=3):
    cases = []
    for trial in range(1, trials + 1):
        cases.append(_entry(tmp_path, "representative", "VERIFIED", EXACT, trial))
        for case_id in (
            "fault-ambiguity",
            "fault-wrong-identity",
            "fault-stale-identity",
            "fault-weak-effect",
            "fault-missing-effect",
        ):
            cases.append(_entry(tmp_path, case_id, "HALTED", NONE, trial))
    return build_result(
        report=_qreport(),
        verify_sha256="sha256:" + H,
        verifier_identity="sha256:" + H,
        fault_driver_version="abc",
        cases=cases,
    )


def test_result_drops_free_text_and_counts(tmp_path):
    result = _result(tmp_path)
    assert result["schema_version"] == "openadapt.qualification-result/v1"
    assert "workflow_name" not in result
    assert result["refusals"] == [{"code": "case_not_passed", "path": "cases"}]
    assert result["passed"]
    assert result["silent_incorrect_success_count"] == 0
    assert result["over_halt_count"] == 0
    assert result["executor_model_calls"] == 0
    assert result["verifier_model_calls"] == 0
    assert "model_calls" not in result
    assert len(result["cases"]) == 18
    changed = {**result, "generated_at": "2030-01-01T00:00:00Z"}
    assert result_sha256(changed) == result_sha256(result)


def test_admission_binds_the_result_digest(tmp_path):
    result = _result(tmp_path)
    envelope = local_admission(
        result=result,
        result_bytes=b"{}",
        oracle_id="rest",
        campaign_id="demo-campaign-v1",
        trials_required=3,
        bundle_content_digest=H,
        effect_contract_sha256=H,
        identity_contract_sha256=None,
    )
    assert isinstance(envelope, QualificationAdmissionEnvelope)
    campaign = envelope.payload.campaign
    assert campaign.outcomes_sha256 == result_sha256(result)
    assert campaign.oracle_contract_sha256 == H
    assert campaign.oracle_id == "rest"
    assert {"over_halt", "silent_incorrect_success"} <= set(campaign.failure_taxonomy)
    # Signed with the local-dev key over the existing admission domain ...
    LOCAL_DEV_PRIVATE_KEY.public_key().verify(
        b64decode(envelope.signature),
        SIGNATURE_DOMAIN + envelope.payload.canonical_bytes(),
    )
    # ... which no production trust map accepts.
    with pytest.raises(QualificationAdmissionError, match="local-dev"):
        verify_qualification_admission(
            envelope,
            trusted_signers=local_dev_signer_trust(),
            expected=expected_from_payload(envelope.payload),
        )


def test_admission_refuses_fewer_than_three_trials(tmp_path):
    with pytest.raises(ValueError, match="at least 3 trials per case"):
        local_admission(
            result=_result(tmp_path, trials=1),
            result_bytes=b"{}",
            oracle_id="rest",
            campaign_id="demo-campaign-v1",
            trials_required=1,
            bundle_content_digest=H,
            effect_contract_sha256=H,
            identity_contract_sha256=None,
        )


@pytest.mark.parametrize("substrate", ["rest", "sql", "fhir", "graphql", "document"])
def test_system_of_record_reads_are_independent(tmp_path, substrate):
    entry = _entry(tmp_path, "representative", "VERIFIED", EXACT, substrate=substrate)
    assert entry["verification_tier"] == "independent_readback"
    assert entry["outcome"] == "verified" and entry["passed"]


def test_a_separate_ui_session_is_screen_readback(tmp_path):
    """Flow #492 ui_readback reads through the UI: never independent_readback."""

    entry = _entry(
        tmp_path,
        "representative",
        "VERIFIED",
        EXACT,
        tier=VerificationTier.INDEPENDENT_SESSION,
        substrate="ui-readback",
    )
    assert entry["verification_tier"] == "screen_readback"
    assert entry["outcome"] == "reconciliation_required"
    assert not entry["passed"]
    # A plugin that claims tier 1 but reads through the UI is still the screen.
    plugin = _entry(
        tmp_path, "representative", "VERIFIED", EXACT, substrate="ui_readback"
    )
    assert plugin["verification_tier"] == "screen_readback" and not plugin["passed"]


def test_certification_needs_the_normal_case_verified_at_independent_readback(tmp_path):
    cases = [
        _entry(
            tmp_path,
            "representative",
            "VERIFIED",
            EXACT,
            tier=VerificationTier.INDEPENDENT_SESSION,
            substrate="ui-readback",
        )
    ] + [
        _entry(tmp_path, case_id, "HALTED", NONE)
        for case_id in (
            "fault-ambiguity",
            "fault-wrong-identity",
            "fault-stale-identity",
            "fault-weak-effect",
            "fault-missing-effect",
        )
    ]
    result = build_result(
        report=_qreport(passed=True),
        verify_sha256="sha256:" + H,
        verifier_identity="sha256:" + H,
        fault_driver_version="abc",
        cases=cases,
    )
    assert not result["passed"]


def test_digest_is_over_the_effective_effects_not_the_file():
    """A record shorthand and its expanded effects list are one contract."""

    from openadapt_flow.qualify.campaign import record_effects

    record = parse_verify_config(
        {
            **BASE,
            "record": {
                "match": ["email"],
                "duplicate_key": "email",
                "fields": {"email": "email", "lname": "lname"},
            },
        },
        env={},
    )
    expanded = record_effects(record)
    listed = parse_verify_config(
        {**BASE, "effects": [e.model_dump(mode="json") for e in expanded]}, env={}
    )
    identity = "sha256:" + H
    assert verify_config_sha256(identity, expanded) == verify_config_sha256(
        identity, listed.effects
    )


def test_a_fault_passes_only_when_nothing_landed_and_the_engine_halted(tmp_path):
    ok = _entry(tmp_path, "fault-ambiguity", "HALTED", NONE)
    assert ok["passed"]
    # The store shows nothing, but the engine did not halt (it reported success).
    not_halted = _entry(tmp_path, "fault-ambiguity", "VERIFIED", NONE)
    assert not not_halted["passed"] and not_halted["silent_incorrect_success"]
    # Not halted and not verified either (the run failed some other way).
    failed = _entry(tmp_path, "fault-weak-effect", "FAILED", NONE)
    assert not failed["passed"]
    # The engine halted, but part of a write landed: REFUTED, not "nothing".
    partial = {**NONE, "observed": "unexpected_mutation", "landed": "some"}
    landed = _entry(tmp_path, "fault-stale-identity", "HALTED", partial)
    assert landed["observed_verdict"] == "REFUTED"
    assert landed["outcome"] == "reconciliation_required"
    assert not landed["passed"]


def test_refusal_paths_carry_only_ids(tmp_path):
    report = _qreport()
    dumped = report.model_dump()
    dumped["refusals"] = [
        {"code": "step_identity_unarmed", "path": "steps.step_040.identity_armed",
         "step_id": "step_040", "message": "free text"},
        {"code": "case_not_passed", "path": "qualification.cases.fault-ambiguity.results",
         "case_id": "fault-ambiguity", "message": "free text"},
        {"code": "policy_violation", "path": "steps.jane.doe@example.invalid",
         "step_id": "jane.doe@example.invalid", "message": "free text"},
        {"code": "policy_violation", "path": "workflow note: Jane Doe, DOB 1971",
         "message": "free text"},
    ]  # fmt: skip
    report = SimpleNamespace(model_dump=lambda **_: dumped)
    result = build_result(
        report=report,
        verify_sha256="sha256:" + H,
        verifier_identity="sha256:" + H,
        fault_driver_version="abc",
        cases=[],
    )
    paths = {r["path"] for r in result["refusals"]}
    assert "steps.step_040.identity_armed" in paths
    assert "qualification.cases.fault-ambiguity.results" in paths
    assert "steps.redacted" in paths and "redacted" in paths
    text = json.dumps(result["refusals"])
    assert (
        "example.invalid" not in text and "Jane" not in text and "free text" not in text
    )
