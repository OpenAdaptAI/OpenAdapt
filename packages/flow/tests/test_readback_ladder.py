"""The read-back ladder: rungs, the workflow minimum, report.json and the
agent result (``openadapt_flow.readback_ladder``)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openadapt_flow.agent.contract import build_result, reason_for_run
from openadapt_flow.execution_profiles import (
    ExecutionProfile,
    readback_tier_floor,
    required_effect_tier,
)
from openadapt_flow.ir import Workflow
from openadapt_flow.qualify.config import VerifyConfig
from openadapt_flow.readback_ladder import (
    CHECKED_HOW_LABELS,
    checked_how_from_report,
    parse_minimum_readback,
    rung_for_tier,
    summarize_report,
    tier_floor,
)
from openadapt_flow.verification import VerificationTier

H = "sha256:" + "0" * 64


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
    ("tier", "confirmed", "rung"),
    [
        (1, True, "R3"),
        (2, True, "R2"),
        (3, True, "R2"),
        (4, True, "R1"),
        (1, False, "R0"),
        (None, True, "R0"),
    ],
)
def test_each_tier_maps_to_one_rung(tier, confirmed, rung):
    assert rung_for_tier(tier, confirmed=confirmed) == rung


def test_minimum_names_and_floors():
    assert parse_minimum_readback("reopened") == "R2"
    assert parse_minimum_readback(" r3 ") == "R3"
    assert parse_minimum_readback("confirmation") == "R1"
    with pytest.raises(ValueError, match="minimum_readback must be one of"):
        parse_minimum_readback("R0")
    assert tier_floor("R1") is VerificationTier.IMMEDIATE_SCREEN
    assert tier_floor("R2") is VerificationTier.PERSISTED_STATE_REACQUISITION
    assert tier_floor("R3") is VerificationTier.INDEPENDENT_SYSTEM
    assert readback_tier_floor(_workflow()) is (
        VerificationTier.PERSISTED_STATE_REACQUISITION
    )


@pytest.mark.parametrize(
    ("profile", "minimum", "tier"),
    [
        (ExecutionProfile.STANDARD, None, 3),
        (ExecutionProfile.STANDARD, "R1", 4),
        (ExecutionProfile.STANDARD, "R3", 1),
        (ExecutionProfile.REGULATED, None, 2),
        (ExecutionProfile.REGULATED, "R1", 2),
        (ExecutionProfile.REGULATED, "R3", 1),
        (ExecutionProfile.DEMO, "R3", None),
    ],
)
def test_the_workflow_minimum_sets_the_run_floor(profile, minimum, tier):
    got = required_effect_tier(_workflow(minimum), profile)
    assert (None if got is None else int(got)) == tier


def test_summary_rungs_per_save_and_for_the_run():
    summary = summarize_report(
        _report(
            _result("step_001", _evidence(1), _evidence(3)),
            _result("step_004", _evidence(1)),
            SimpleNamespace(
                step_id="step_005", effect_evidence=[], effect_contract_hashes=[]
            ),
        ),
        _workflow(),
    )
    assert summary["minimum"] == "R2"
    assert [s["rung"] for s in summary["saves"]] == ["R2", "R3"]
    assert summary["saves"][0]["flow_tier"] == 3
    assert summary["reached"] == "R2"
    assert summary["checked_how"] == "reopened"
    assert summary["label"] == "Reopened and checked on screen"
    assert summary["below_minimum"] is False


def test_a_confirmed_check_below_the_minimum_is_flagged():
    summary = summarize_report(_report(_result("step_001", _evidence(4))), _workflow())
    assert summary["reached"] == "R1"
    assert summary["below_minimum"] is True
    # R1 is enough only when the workflow asks for R1.
    allowed = summarize_report(
        _report(_result("step_001", _evidence(4))), _workflow("R1")
    )
    assert allowed["below_minimum"] is False


def test_an_unconfirmed_or_missing_check_is_not_below_minimum_but_unchecked():
    refuted = summarize_report(
        _report(_result("step_001", _evidence(4), _evidence(3, verdict="refuted"))),
        _workflow(),
    )
    assert refuted["reached"] == "R0"
    assert refuted["below_minimum"] is False
    missing = summarize_report(_report(_result("step_001", declared=1)), _workflow())
    assert missing["saves"] == [
        {"step_id": "step_001", "rung": "R0", "flow_tier": None}
    ]
    assert missing["checked_how"] == "not_checked"


def test_no_save_means_no_readback_block():
    assert summarize_report(_report(), _workflow()) is None


def test_workflow_bytes_are_unchanged_without_a_minimum():
    plain = Workflow(name="w")
    assert "minimum_readback" not in plain.model_dump(mode="json")
    strict = Workflow(name="w", minimum_readback="R3")
    again = Workflow.model_validate(strict.model_dump(mode="json"))
    assert again.minimum_readback == "R3"


def _agent_report(tx, *, below=False, checked_how="screen_said_saved"):
    execution = "VERIFIED" if tx == "VERIFIED" else "COMPLETED_UNVERIFIED"
    return {
        "execution_outcome": execution,
        "transaction_outcome": tx,
        "success": tx == "VERIFIED",
        "results": [],
        "readback": {
            "minimum": "R2",
            "reached": "R1",
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
    assert result["checked_how"] == "screen_said_saved"
    assert result["checked_how_label"] == "The screen said it saved"
    plain = _agent_report("COMPLETED_UNVERIFIED", below=False)
    assert reason_for_run(exit_code=0, report=plain) == "not_checked"


def test_checked_how_rides_on_done_and_never_on_did_not_run():
    done = build_result(
        "saved_and_checked",
        workflow="w",
        run_id="r",
        request_id="q",
        mode="production",
        checked_how="records",
    )
    assert done["checked_how_label"] == "Checked in your system's records"
    refused = build_result(
        "invalid_input",
        workflow="w",
        run_id=None,
        request_id="q",
        mode="production",
        checked_how="records",
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
    cfg = VerifyConfig.model_validate(_verify_yaml(minimum_readback="reopened"))
    assert cfg.minimum_readback == "R2"
    assert int(cfg.minimum_tier()) == 3
    with pytest.raises(ValueError, match="not both"):
        VerifyConfig.model_validate(
            _verify_yaml(
                minimum_readback="R3",
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


# -- R1 wiring -----------------------------------------------------------------


def _replayer_stub():
    from openadapt_flow.runtime.replayer import Replayer

    return Replayer.__new__(Replayer)


def test_same_screen_check_is_wired_only_when_the_workflow_asks_for_r1() -> None:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime.effects.effect import (
        Effect,
        EffectKind,
        ReadbackSpec,
        ValueExpr,
    )

    same_screen = Effect(
        kind=EffectKind.FIELD_EQUALS,
        value=ValueExpr(literal="saved"),
        readback=ReadbackSpec(region=(0, 0, 200, 40)),
    )
    stub = _replayer_stub()
    default = Workflow(name="w", steps=[])
    r1 = Workflow(name="w", steps=[], minimum_readback="R1")
    assert stub._all_default_readback([same_screen], default) is False
    assert stub._all_default_readback([same_screen], r1) is True
    assert stub._all_default_readback([], r1) is False
