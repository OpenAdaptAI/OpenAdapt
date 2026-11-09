"""Opt-in real-RDP multi-window visual workflow qualification."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from benchmark.rdp_multiapp.run_qualification import run


def test_real_rdp_multiapp_visual_subset(tmp_path: Path) -> None:
    container = os.getenv("OPENADAPT_RDP_MULTIAPP_CONTAINER")
    oracle = os.getenv("OPENADAPT_RDP_MULTIAPP_ORACLE_ROOT")
    if not container or not oracle:
        pytest.skip(
            "set OPENADAPT_RDP_MULTIAPP_CONTAINER and "
            "OPENADAPT_RDP_MULTIAPP_ORACLE_ROOT for the real-RDP fixture"
        )

    output = tmp_path / "results.json"
    result = run(container, Path(oracle), output, tmp_path / "work")

    assert result["accepted_subset"] is True
    assert result["run_count"] == 21
    assert result["silent_incorrect_successes"] == 0
    assert result["over_halts"] == 0
    assert result["model_calls"] == 0
    assert result["full_campaign_complete"] is False
    fault_trials = {
        condition: [
            trial for trial in result["trials"] if trial["condition"] == condition
        ]
        for condition in ("duplicate_save_control", "partial_render")
    }
    assert all(len(trials) == 3 for trials in fault_trials.values())
    assert all(
        trial["safe_halt"]
        and trial["exact_fault_evidence"]
        and trial["oracle"]["database"] == []
        and trial["oracle"]["worklist_unchanged"]
        and trial["oracle"]["mail"] == []
        and trial["oracle"]["no_consequential_input"]
        and trial["model_calls"] == 0
        for trials in fault_trials.values()
        for trial in trials
    )
    assert all(
        trial["typed_target_refusal"]
        for trial in fault_trials["duplicate_save_control"]
    )
    assert all(
        trial["relevant_partial_refusal"]
        and trial["fault_ack"]["fault"] == "partial_render"
        and trial["fault_ack"]["scenario"] == "healthy"
        and trial["fault_ack"]["fault_token"]
        and trial["fault_ack"]["save_control_count"] == 1
        and trial["fault_ack"]["identity_surface"] == "loading_skeleton"
        and any(
            evidence["stage"] == "identity_verification"
            and evidence["code"] in {"identity_unverifiable", "identity_conflict"}
            for evidence in trial["safety_refusal_evidence"]
        )
        for trial in fault_trials["partial_render"]
    )
    uncertain = [
        trial
        for trial in result["trials"]
        if trial["condition"] == "commit_then_timeout"
    ]
    assert len(uncertain) == 3
    assert all(trial["commit_timeout_injected"] for trial in uncertain)
    assert all(trial["save_delivery_calls"] == 1 for trial in uncertain)
    assert all(
        trial["oracle"]["input_counts"]["save_appointment"] == 1 for trial in uncertain
    )
    assert all(
        trial["transaction_outcome"] in {"VERIFIED", "RECONCILIATION_REQUIRED"}
        for trial in uncertain
    )
    assert json.loads(output.read_text()) == result
