"""FlowRunner + outcome mapping: success, halt, refusal, timeout, gating."""

from __future__ import annotations

import subprocess

import pytest
from conftest import FlowCliStub

import openadapt_agent.runner as runner_mod
from openadapt_agent.runner import FlowRunner, RunnerConfig, classify_outcome


def _run(monkeypatch, runner_config, stub, **kwargs):
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    return FlowRunner(runner_config).run(
        workflow="demo_triage",
        bundle_dir=kwargs.pop("bundle_dir"),
        params=kwargs.pop("params", {"note": "hello"}),
        **kwargs,
    )


def test_success_mapping(monkeypatch, runner_config, bundle_dir, success_report):
    stub = FlowCliStub(exit_code=0, report=success_report)
    outcome = _run(monkeypatch, runner_config, stub, bundle_dir=bundle_dir)
    assert outcome.status == "success"
    assert outcome.to_dict()["success"] is True
    assert outcome.summary["steps_ok"] == 2
    assert outcome.report_path and outcome.report_path.endswith("report.json")
    # Governed run verb, params via file (never argv), run dir owned by us.
    cmd = stub.calls[0]
    assert cmd[0] == "openadapt-flow-stub" and cmd[1] == "run"
    assert "--params-file" in cmd and "--run-dir" in cmd
    assert "hello" not in " ".join(cmd)


def test_halt_maps_to_structured_halt_not_success(
    monkeypatch, runner_config, bundle_dir, halt_report
):
    stub = FlowCliStub(exit_code=1, report=halt_report)
    outcome = _run(monkeypatch, runner_config, stub, bundle_dir=bundle_dir)
    assert outcome.status == "halt"
    assert outcome.to_dict()["success"] is False
    assert outcome.halt is not None
    assert outcome.halt["reason"] == "unmet postcondition"
    assert outcome.halt["observed_texts"] == ["Unexpected dialog: Save changes?"]
    assert "report.json" in outcome.detail


def test_exit_zero_with_failed_report_is_never_success(
    monkeypatch, runner_config, bundle_dir, halt_report
):
    """Defense in depth: the persisted report outranks the exit code."""
    stub = FlowCliStub(exit_code=0, report=halt_report)
    outcome = _run(monkeypatch, runner_config, stub, bundle_dir=bundle_dir)
    assert outcome.status == "halt"
    assert outcome.to_dict()["success"] is False


def test_exit_zero_without_report_is_error(monkeypatch, runner_config, bundle_dir):
    stub = FlowCliStub(exit_code=0, report=None)
    outcome = _run(monkeypatch, runner_config, stub, bundle_dir=bundle_dir)
    assert outcome.status == "error"
    assert "without evidence" in outcome.detail


def test_exit_two_is_governed_refusal(monkeypatch, runner_config, bundle_dir):
    stub = FlowCliStub(exit_code=2, report=None)
    stub.stdout = "run REFUSED: coverage gate failed"
    outcome = _run(monkeypatch, runner_config, stub, bundle_dir=bundle_dir)
    assert outcome.status == "refused"
    assert "Nothing was executed" in outcome.detail
    assert "REFUSED" in outcome.stdout_tail


def test_timeout_maps_to_timeout(monkeypatch, runner_config, bundle_dir):
    def raise_timeout(cmd, capture_output=True, text=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd, timeout, output=b"partial", stderr=b"")

    outcome = _run(monkeypatch, runner_config, raise_timeout, bundle_dir=bundle_dir)
    assert outcome.status == "timeout"
    assert outcome.to_dict()["success"] is False
    assert "killed" in outcome.detail


def test_missing_cli_maps_to_error(monkeypatch, runner_config, bundle_dir):
    def raise_missing(cmd, capture_output=True, text=True, timeout=None):
        raise FileNotFoundError(cmd[0])

    outcome = _run(monkeypatch, runner_config, raise_missing, bundle_dir=bundle_dir)
    assert outcome.status == "error"
    assert "not found" in outcome.detail


def test_url_override_refused_without_flag(monkeypatch, runner_config, bundle_dir):
    stub = FlowCliStub(exit_code=0)
    outcome = _run(
        monkeypatch,
        runner_config,
        stub,
        bundle_dir=bundle_dir,
        url_override="https://evil.example",
    )
    assert outcome.status == "refused"
    assert stub.calls == []  # nothing executed


def test_url_override_honoured_with_flag(
    monkeypatch, tmp_path, bundle_dir, success_report
):
    config = RunnerConfig(
        flow_cli=("openadapt-flow-stub",),
        runs_dir=tmp_path / "runs",
        allow_url_override=True,
    )
    stub = FlowCliStub(exit_code=0, report=success_report)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    outcome = FlowRunner(config).run(
        workflow="demo_triage",
        bundle_dir=bundle_dir,
        params={},
        url_override="https://staging.example",
    )
    assert outcome.status == "success"
    cmd = stub.calls[0]
    assert cmd[cmd.index("--url") + 1] == "https://staging.example"


def test_operator_fixed_args_forwarded(monkeypatch, tmp_path, bundle_dir, success_report):
    config = RunnerConfig(
        flow_cli=("openadapt-flow-stub",),
        runs_dir=tmp_path / "runs",
        url="https://app.example",
        deployment_config="deploy.yaml",
        policy="clinical-write",
        extra_run_args=("--strict-templates",),
    )
    stub = FlowCliStub(exit_code=0, report=success_report)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    FlowRunner(config).run(workflow="w", bundle_dir=bundle_dir, params={})
    cmd = stub.calls[0]
    assert cmd[cmd.index("--url") + 1] == "https://app.example"
    assert cmd[cmd.index("--config") + 1] == "deploy.yaml"
    assert cmd[cmd.index("--policy") + 1] == "clinical-write"
    assert "--strict-templates" in cmd


@pytest.mark.parametrize("exit_code", [3, 130])
def test_other_nonzero_exit_is_halt_with_evidence_pointer(exit_code):
    outcome = classify_outcome("w", exit_code, None, stdout="boom", stderr="")
    assert outcome.status == "halt"
    assert outcome.to_dict()["success"] is False
