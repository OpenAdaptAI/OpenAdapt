"""End-to-end wiring tests for the integrated OpenEMR proof harness.

These drive the FULL compiled pipeline through the fixture substrate --
compile -> replay -> effect-verify -> silent-wrong-write catch -> drift HALT ->
teach -> re-run clean -- and assert the runtime components COMPOSE. Everything
is deterministic, offline (in-process MockMed ``fault_server`` as the system of
record), and makes ZERO model calls at $0. The paid agent arm is asserted to be
gated off and never invoked.

The individual runtime components are unit-tested elsewhere (``test_compiler``,
``test_replayer_effects``, ``test_effect_fault_matrix``, ``test_halt_learn_loop``);
this file pins that the harness wires them into one working loop.
"""

from __future__ import annotations

import json

import pytest

from benchmark.openemr_e2e import simulation as sim
from benchmark.openemr_e2e.harness import (
    AgentArmRefused,
    agent_arm_gate,
    recorded_ratio,
    run_openemr_e2e,
)
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.runtime.replayer import Replayer

NOTE = "Renal panel ordered ahead of the next quarterly visit."


def _replay(workflow, *, bundle_dir, run_dir, note, fault="", modal=None):
    url, db, stop = fault_serve()
    try:
        backend = sim.SimBackend(url, fault=fault)
        vision = sim.AddNoteVision(backend, modal_text=modal)
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(url.rstrip("/")),
            poll_interval_s=0.01,
        )
        report = replayer.run(
            workflow,
            params={sim.NOTE_PARAM: note},
            bundle_dir=bundle_dir,
            run_dir=run_dir,
        )
        return report, backend, db.snapshot()
    finally:
        stop()


# ===========================================================================
# The integrated loop (the deliverable): one run, all six phases green, $0.
# ===========================================================================


def test_full_pipeline_passes_at_zero_cost(tmp_path):
    result = run_openemr_e2e(tmp_path / "e2e", log=lambda _m: None)

    # Every phase composed.
    assert result.passed is True, [
        (p.name, p.passed, p.detail) for p in result.phases if not p.passed
    ]
    assert [p.name for p in result.phases] == [
        "compile",
        "clean_replay_effect_verify",
        "silent_wrong_write_catch",
        "inject_drift_halt",
        "teach",
        "rerun_clean",
    ]

    # The cost guardrail, enforced by construction.
    assert result.cost_usd == 0.0
    assert result.model_calls == 0
    assert result.agent_arm["invoked"] is False

    # Fixture-labelled (no live endpoint configured in CI).
    assert result.substrate == "fixture"
    assert result.live_probe is None

    # Artifacts on disk.
    out = tmp_path / "e2e"
    assert (out / "result.json").exists()
    assert (out / "SUMMARY.md").exists()
    assert (out / "bundle" / "workflow.json").exists()
    saved = json.loads((out / "result.json").read_text())
    assert saved["passed"] is True
    assert saved["cost_usd"] == 0.0
    assert saved["agent_arm"]["invoked"] is False


def test_summary_discloses_zero_cost_and_gated_agent_arm(tmp_path):
    run_openemr_e2e(tmp_path / "e2e", log=lambda _m: None)
    summary = (tmp_path / "e2e" / "SUMMARY.md").read_text()
    assert "$0.00" in summary
    assert "Model calls: **0**" in summary
    assert "refuses to invoke it" in summary
    assert "scripts/openemr_demo.py benchmark" in summary
    # Live-vs-fixture is disclosed, never a silent pass.
    assert "substrate **fixture**" in summary
    assert "never a silent pass" in summary


# ===========================================================================
# Phase-level assertions on the real runtime behaviour the harness relies on.
# ===========================================================================


def test_compile_bundle_carries_effects(tmp_path):
    workflow = sim.write_bundle(tmp_path / "bundle")
    assert (tmp_path / "bundle" / "workflow.json").exists()
    save = workflow.program.states["s_save"].step
    kinds = {e.kind.value for e in save.effects}
    assert kinds == {"record_written", "field_equals"}


def test_clean_replay_confirms_write_against_system_of_record(tmp_path):
    workflow = sim.write_bundle(tmp_path / "bundle")
    report, _be, snap = _replay(
        workflow, bundle_dir=tmp_path / "bundle", run_dir=tmp_path / "run", note=NOTE
    )
    assert report.success is True
    save = next(x for x in report.results if x.step_id == "s_save")
    assert save.effect_verified is True
    assert all("CONFIRMED" in line for line in save.effect_results)
    assert snap["records"][0]["note"] == NOTE
    # $0: the clean run makes no model calls.
    assert report.model_calls == 0


def test_silent_partial_write_is_caught_by_effect_verify(tmp_path):
    """The flagship: screen says 'Saved', the record dropped the note -> HALT."""
    workflow = sim.write_bundle(tmp_path / "bundle")
    report, _be, snap = _replay(
        workflow,
        bundle_dir=tmp_path / "bundle",
        run_dir=tmp_path / "run",
        note=NOTE,
        fault="partial",
    )
    save = next(x for x in report.results if x.step_id == "s_save")
    # The SCREEN oracle passed...
    assert save.postconditions_ok is True
    # ...but the RECORD check refuted and the run halted.
    assert report.success is False
    assert report.terminal_outcome == "halt"
    assert any("REFUTED" in line for line in save.effect_results)
    assert snap["records"][0]["note"] == ""


def test_drift_halts_and_emits_learnable_observation(tmp_path):
    workflow = sim.write_bundle(tmp_path / "bundle")
    report, _be, _snap = _replay(
        workflow,
        bundle_dir=tmp_path / "bundle",
        run_dir=tmp_path / "run",
        note=NOTE,
        modal=sim.CONSENT_MODAL_FACT,
    )
    assert report.success is False
    assert report.terminal_outcome == "halt"
    halt = report.halt
    assert halt is not None
    assert halt.intent == sim.INTENT_CONFIRM
    assert sim.CONSENT_MODAL_FACT in halt.observed_texts
    assert sim.INTENT_SAVE in halt.completed_intents


# ===========================================================================
# The cost guardrail: the paid agent arm is gated off and never invoked.
# ===========================================================================


def test_agent_arm_gate_noop_when_disabled():
    # Default: not enabled -> nothing happens, no exception.
    assert agent_arm_gate(enable=False, max_cost_usd=None) is None


def test_agent_arm_requires_cap():
    with pytest.raises(ValueError, match="max-cost-usd"):
        agent_arm_gate(enable=True, max_cost_usd=None)
    with pytest.raises(ValueError):
        agent_arm_gate(enable=True, max_cost_usd=0.0)


def test_agent_arm_refuses_even_with_cap():
    with pytest.raises(AgentArmRefused, match="no API is ever called"):
        agent_arm_gate(enable=True, max_cost_usd=1.50)


def test_run_refuses_paid_arm_without_spending(tmp_path):
    with pytest.raises(AgentArmRefused):
        run_openemr_e2e(
            tmp_path / "e2e",
            enable_agent_arm=True,
            max_cost_usd=1.50,
            log=lambda _m: None,
        )
    # Refused before any pipeline ran -> nothing spent, no artifacts required.


# ===========================================================================
# The recorded ratio is READ, never regenerated (no spend).
# ===========================================================================


def test_recorded_ratio_is_read_not_spent():
    ratio = recorded_ratio()
    # The committed OpenEMR benchmark results exist in the repo.
    assert ratio is not None
    assert ratio["compiled"]["cost_usd_per_run"] == 0.0
    assert ratio["agent"]["cost_usd_per_run"] > 0.0
    assert "recorded" in ratio["framing"]


def test_recorded_ratio_missing_file_is_not_an_error(tmp_path):
    assert recorded_ratio(tmp_path / "nope.json") is None


def test_live_probe_absent_labels_fixture(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENEMR_FHIR_BASE_URL", raising=False)
    result = run_openemr_e2e(tmp_path / "e2e", log=lambda _m: None)
    assert result.live_probe is None
    assert result.substrate == "fixture"


def test_require_live_without_endpoint_is_hard_error(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENEMR_FHIR_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="require_live"):
        run_openemr_e2e(tmp_path / "e2e", require_live=True, log=lambda _m: None)
