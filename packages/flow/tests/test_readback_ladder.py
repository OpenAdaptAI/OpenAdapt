"""How each save was checked: check levels, the VERIFIED floor, report.json
and the agent result (``openadapt_flow.readback_ladder``)."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from openadapt_flow.agent.contract import build_result, reason_for_run
from openadapt_flow.check_level import CheckLevel
from openadapt_flow.execution_profiles import (
    ExecutionProfile,
    execution_profile_contract,
    required_effect_tier,
    verified_tier_floor,
)
from openadapt_flow.ir import Workflow
from openadapt_flow.qualify.config import VerifyConfig
from openadapt_flow.readback_ladder import (
    CHECKED_HOW_LABELS,
    checked_how_from_report,
    level_for_tier,
    parse_minimum_readback,
    summarize_report,
    tier_floor,
)
from openadapt_flow.verification import VERIFIED_EFFECT_TIER, VerificationTier

H = "sha256:" + "0" * 64
SEPARATE = VerificationTier.INDEPENDENT_SESSION


def _evidence(tier, verdict="confirmed", observed="present"):
    return SimpleNamespace(
        verification_tier=tier, final_verdict=verdict, observed_effect=observed
    )


def _result(step_id, *evidence, declared=None):
    return SimpleNamespace(
        step_id=step_id,
        effect_evidence=list(evidence),
        effect_contract_hashes=[H] * (len(evidence) if declared is None else declared),
    )


def _report(*results):
    return SimpleNamespace(results=list(results))


def _workflow(minimum=None):
    return SimpleNamespace(minimum_readback=minimum, qualification=None)


@pytest.mark.parametrize(
    ("tier", "confirmed", "level"),
    [
        (1, True, "system_of_record"),
        (2, True, "separate_session"),
        (3, True, "reopened"),
        (4, True, "same_screen"),
        (1, False, "unchecked"),
        (None, True, "unchecked"),
        (9, True, "unchecked"),
    ],
)
def test_each_tier_maps_to_one_check_level(tier, confirmed, level):
    assert level_for_tier(tier, confirmed=confirmed).label == level


def test_a_same_session_reopen_and_a_separate_session_have_their_own_labels():
    reopened = level_for_tier(3, confirmed=True)
    separate = level_for_tier(2, confirmed=True)
    assert reopened is CheckLevel.REOPENED
    assert separate is CheckLevel.SEPARATE_SESSION
    assert CHECKED_HOW_LABELS["reopened"] == "Reopened in the same session"
    assert CHECKED_HOW_LABELS["separate_session"] == "Checked in a separate session"
    assert len(set(CHECKED_HOW_LABELS.values())) == len(CHECKED_HOW_LABELS)
    assert set(CHECKED_HOW_LABELS) == {level.label for level in CheckLevel}


def test_a_workflow_minimum_can_only_raise_the_floor():
    assert parse_minimum_readback(" Separate-Session ") == "separate_session"
    assert parse_minimum_readback("system_of_record") == "system_of_record"
    for weaker in ("reopened", "same_screen", "unchecked", "R1", "R2", "R3"):
        with pytest.raises(ValueError, match="never makes a run VERIFIED"):
            parse_minimum_readback(weaker)
    assert tier_floor("separate_session") is SEPARATE
    assert tier_floor("system_of_record") is VerificationTier.INDEPENDENT_SYSTEM
    with pytest.raises(ValueError):
        Workflow(name="w", minimum_readback="reopened")


@pytest.mark.parametrize(
    ("profile", "minimum", "floor"),
    [
        (ExecutionProfile.STANDARD, None, 2),
        (ExecutionProfile.STANDARD, "separate_session", 2),
        (ExecutionProfile.STANDARD, "system_of_record", 1),
        (ExecutionProfile.REGULATED, None, 2),
        (ExecutionProfile.REGULATED, "system_of_record", 1),
    ],
)
def test_verified_needs_a_separate_session_or_the_system_of_record(
    profile, minimum, floor
):
    assert VERIFIED_EFFECT_TIER is SEPARATE
    assert int(verified_tier_floor(_workflow(minimum), profile)) == floor


def test_no_built_in_profile_lets_a_reopen_count_as_verified():
    for profile in ExecutionProfile:
        assert execution_profile_contract(profile).reopened_counts_as_verified is False


def test_a_profile_that_opts_in_lets_a_reopen_count():
    opted_in = dataclasses.replace(
        execution_profile_contract(ExecutionProfile.STANDARD),
        reopened_counts_as_verified=True,
    )
    assert verified_tier_floor(_workflow(), opted_in) is (
        VerificationTier.PERSISTED_STATE_REACQUISITION
    )
    # The workflow still raises it.
    assert verified_tier_floor(_workflow("separate_session"), opted_in) is SEPARATE


@pytest.mark.parametrize(
    ("profile", "minimum", "tier"),
    [
        # The gate is unchanged without a workflow minimum: Standard still
        # lets a pixel-only run execute with halt-on-doubt.
        (ExecutionProfile.STANDARD, None, 3),
        (ExecutionProfile.STANDARD, "separate_session", 2),
        (ExecutionProfile.STANDARD, "system_of_record", 1),
        (ExecutionProfile.REGULATED, None, 2),
        (ExecutionProfile.REGULATED, "system_of_record", 1),
        (ExecutionProfile.DEMO, "system_of_record", None),
    ],
)
def test_a_workflow_minimum_raises_the_gate(profile, minimum, tier):
    got = required_effect_tier(_workflow(minimum), profile)
    assert (None if got is None else int(got)) == tier


def test_summary_levels_per_save_and_for_the_run():
    summary = summarize_report(
        _report(
            _result("step_001", _evidence(1), _evidence(2)),
            _result("step_004", _evidence(1)),
            SimpleNamespace(
                step_id="step_005", effect_evidence=[], effect_contract_hashes=[]
            ),
        ),
        SEPARATE,
    )
    assert summary["minimum"] == "separate_session"
    assert [s["checked_how"] for s in summary["saves"]] == [
        "separate_session",
        "system_of_record",
    ]
    assert summary["saves"][0]["flow_tier"] == 2
    assert summary["checked_how"] == "separate_session"
    assert summary["label"] == "Checked in a separate session"
    assert summary["below_minimum"] is False


def test_a_same_session_reopen_is_below_the_default_minimum():
    summary = summarize_report(_report(_result("step_001", _evidence(3))), SEPARATE)
    assert summary["checked_how"] == "reopened"
    assert summary["label"] == "Reopened in the same session"
    assert summary["below_minimum"] is True
    screen = summarize_report(_report(_result("step_001", _evidence(4))), SEPARATE)
    assert screen["checked_how"] == "same_screen"
    assert screen["below_minimum"] is True
    # Only a profile that opts in moves the floor to a reopen.
    opted_in = summarize_report(
        _report(_result("step_001", _evidence(3))),
        VerificationTier.PERSISTED_STATE_REACQUISITION,
    )
    assert opted_in["minimum"] == "reopened"
    assert opted_in["below_minimum"] is False


def test_an_unconfirmed_or_missing_check_is_not_below_minimum_but_unchecked():
    refuted = summarize_report(
        _report(_result("step_001", _evidence(4), _evidence(3, verdict="refuted"))),
        SEPARATE,
    )
    assert refuted["checked_how"] == "unchecked"
    assert refuted["below_minimum"] is False
    missing = summarize_report(_report(_result("step_001", declared=1)), SEPARATE)
    assert missing["saves"] == [
        {"step_id": "step_001", "checked_how": "unchecked", "flow_tier": None}
    ]
    assert missing["label"] == "Not checked"


def test_no_save_means_no_readback_block():
    assert summarize_report(_report(), SEPARATE) is None


def test_workflow_bytes_are_unchanged_without_a_minimum():
    plain = Workflow(name="w")
    assert "minimum_readback" not in plain.model_dump(mode="json")
    strict = Workflow(name="w", minimum_readback="system_of_record")
    again = Workflow.model_validate(strict.model_dump(mode="json"))
    assert again.minimum_readback == "system_of_record"


def _agent_report(tx, *, below=False, checked_how="reopened"):
    execution = "VERIFIED" if tx == "VERIFIED" else "COMPLETED_UNVERIFIED"
    return {
        "execution_outcome": execution,
        "transaction_outcome": tx,
        "success": tx == "VERIFIED",
        "results": [],
        "readback": {
            "minimum": "separate_session",
            "checked_how": checked_how,
            "label": CHECKED_HOW_LABELS[checked_how],
            "below_minimum": below,
            "saves": [],
        },
    }


def test_agent_reports_checked_below_minimum(monkeypatch):
    import openadapt_flow.agent.contract as contract

    monkeypatch.setattr(
        contract,
        "report_consistency",
        lambda r: (True, r["execution_outcome"], r["transaction_outcome"]),
    )
    report = _agent_report("COMPLETED_UNVERIFIED", below=True)
    reason = reason_for_run(exit_code=0, report=report)
    assert reason == "checked_below_minimum"
    result = build_result(
        reason,
        workflow="w",
        run_id="r",
        request_id="q",
        mode="production",
        checked_how=checked_how_from_report(report),
    )
    assert result["outcome"] == "not_sure_if_saved"
    assert result["safe_to_retry"] is False
    assert result["checked_how"] == "reopened"
    assert result["checked_how_label"] == "Reopened in the same session"
    plain = _agent_report("COMPLETED_UNVERIFIED", below=False)
    assert reason_for_run(exit_code=0, report=plain) == "not_checked"


def test_checked_how_rides_on_done_and_never_on_did_not_run():
    done = build_result(
        "saved_and_checked",
        workflow="w",
        run_id="r",
        request_id="q",
        mode="production",
        checked_how="system_of_record",
    )
    assert done["checked_how_label"] == "Checked in your system's records"
    refused = build_result(
        "invalid_input",
        workflow="w",
        run_id=None,
        request_id="q",
        mode="production",
        checked_how="system_of_record",
    )
    assert "checked_how" not in refused
    unknown = build_result(
        "saved_and_checked",
        workflow="w",
        run_id="r",
        request_id="q",
        mode="production",
        checked_how="something_else",
    )
    assert "checked_how" not in unknown


def _verify_yaml(**extra):
    data = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "rest",
            "base_url": "http://127.0.0.1:9",
            "records_path": "/r",
        },
        "app": {
            "target": "web",
            "url": "http://127.0.0.1:9",
            "name": "fixture",
            "version": "1",
        },
    }
    data.update(extra)
    return data


def test_qualify_minimum_readback_and_its_alias():
    assert int(VerifyConfig.model_validate(_verify_yaml()).minimum_tier()) == 1
    assert (
        int(
            VerifyConfig.model_validate(
                _verify_yaml(minimum_effect_strength="independent_session")
            ).minimum_tier()
        )
        == 2
    )
    cfg = VerifyConfig.model_validate(_verify_yaml(minimum_readback="separate_session"))
    assert cfg.minimum_readback == "separate_session"
    assert int(cfg.minimum_tier()) == 2
    with pytest.raises(ValueError, match="never makes a run VERIFIED"):
        VerifyConfig.model_validate(_verify_yaml(minimum_readback="reopened"))
    with pytest.raises(ValueError, match="not both"):
        VerifyConfig.model_validate(
            _verify_yaml(
                minimum_readback="system_of_record",
                minimum_effect_strength="independent_system_of_record",
            )
        )


# -- reference numbers learned from the save's screen ---------------------------


def _ln(text: str, region=(100, 100, 300, 24)):
    from types import SimpleNamespace

    return SimpleNamespace(text=text, region=region, confidence=0.95)


def test_reference_pattern_keeps_letters_and_flexes_digit_runs() -> None:
    import re

    from openadapt_flow.compiler.effect_mining import reference_pattern

    pattern = re.compile(reference_pattern("CLM-20417"))
    assert pattern.search("Saved. Claim CLM-20417.").group(1) == "CLM-20417"
    assert pattern.search("Claim CLM-9981234 created").group(1) == "CLM-9981234"
    assert pattern.search("XCLM-20417") is None  # no partial-token match
    assert pattern.search("INV-20417") is None


def test_mine_reference_learns_the_one_new_number() -> None:
    from openadapt_flow.compiler.effect_mining import mine_reference

    before = [_ln("New claim  Policy 55120")]
    after = [_ln("Policy 55120"), _ln("Saved. Claim CLM-20417", (90, 300, 260, 24))]
    mined = mine_reference(before, after, exclude_texts=("water damage",))
    assert mined is not None
    reference, token = mined
    assert token == "CLM-20417"
    x, y, w, h = reference.region
    assert x <= 90 and y <= 300 and x + w >= 350 and y + h >= 324


def test_mine_reference_learns_nothing_when_ambiguous_or_typed() -> None:
    from openadapt_flow.compiler.effect_mining import mine_reference

    two = [_ln("Saved E1042"), _ln("Replaces E0977", (100, 140, 300, 24))]
    assert mine_reference([], two) is None
    typed = [_ln("Saved. Policy 77812")]
    assert mine_reference([], typed, exclude_texts=("77812",)) is None
    dated = [_ln("Saved 2026-10-17")]
    assert mine_reference([], dated) is None
    assert mine_reference([], []) is None
