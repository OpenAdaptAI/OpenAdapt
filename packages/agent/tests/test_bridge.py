"""Bridge tool surface: schema generation, --allow-run gating, report access."""

from __future__ import annotations

import json

import pytest
from conftest import FlowCliStub

import openadapt_agent.runner as runner_mod
from openadapt_agent.bridge import AgentBridge, BridgeError
from openadapt_agent.bundles import tool_input_schema
from openadapt_agent.runner import RunnerConfig


def make_bridge(bundles_root, runner_config, allow_run=False):
    return AgentBridge(bundles_root, runner_config, allow_run=allow_run)


def test_schema_from_fixture_bundle(bundles_root, runner_config):
    bridge = make_bridge(bundles_root, runner_config, allow_run=True)
    specs = {s.name: s for s in bridge.list_tool_specs()}
    spec = specs["run_demo_triage"]
    schema = spec.input_schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == []
    assert schema["properties"]["note"]["type"] == "string"
    assert schema["properties"]["note"]["default"] == "Follow-up in 2 weeks"
    # URL override NOT exposed unless the operator opted in.
    assert "url" not in schema["properties"]


def test_url_property_only_with_override_flag(bundle_dir):
    from openadapt_agent.bundles import load_workflow_info

    info = load_workflow_info(bundle_dir)
    with_url = tool_input_schema(info, allow_url_override=True)
    without_url = tool_input_schema(info, allow_url_override=False)
    assert "url" in with_url["properties"]
    assert "url" not in without_url["properties"]


def test_read_only_tools_without_allow_run(bundles_root, runner_config):
    bridge = make_bridge(bundles_root, runner_config, allow_run=False)
    names = [s.name for s in bridge.list_tool_specs()]
    assert names == ["list_workflows", "get_workflow", "get_run_report"]
    listing = bridge.dispatch("list_workflows", {})
    assert listing["run_tools_enabled"] is False
    assert listing["workflows"][0]["slug"] == "demo_triage"
    # Calling a run tool anyway is refused loudly.
    with pytest.raises(BridgeError, match="--allow-run"):
        bridge.dispatch("run_demo_triage", {})


def test_get_workflow_reports_metadata_and_uncertified_status(
    bundles_root, runner_config
):
    bridge = make_bridge(bundles_root, runner_config)
    info = bridge.dispatch("get_workflow", {"workflow": "demo_triage"})
    assert info["name"] == "Demo Triage"
    assert info["steps"] == ["Open the patient chart", "Type the triage note"]
    assert info["params"] == {"note": "Follow-up in 2 weeks"}
    # No policy configured -> certification honestly "not evaluated".
    assert info["certification"]["certified"] is None


def test_run_dispatch_success_and_report_roundtrip(
    monkeypatch, bundles_root, runner_config, success_report
):
    stub = FlowCliStub(exit_code=0, report=success_report)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config, allow_run=True)
    result = bridge.dispatch("run_demo_triage", {"note": "custom note"})
    assert result["status"] == "success"
    # The persisted report is retrievable via the read-only tool.
    fetched = bridge.dispatch("get_run_report", {"run_id": result["run_id"]})
    assert fetched["report"]["success"] is True


def test_run_dispatch_halt_surfaces_evidence(
    monkeypatch, bundles_root, runner_config, halt_report
):
    stub = FlowCliStub(exit_code=1, report=halt_report)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config, allow_run=True)
    result = bridge.dispatch("run_demo_triage", {})
    assert result["status"] == "halt"
    assert result["success"] is False
    assert result["halt"]["observed_texts"] == ["Unexpected dialog: Save changes?"]
    assert result["report_path"].endswith("report.json")


def test_unknown_parameter_rejected(monkeypatch, bundles_root, runner_config):
    stub = FlowCliStub(exit_code=0)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config, allow_run=True)
    with pytest.raises(BridgeError, match="unknown parameter"):
        bridge.dispatch("run_demo_triage", {"nope": "x"})
    assert stub.calls == []


def test_get_run_report_traversal_refused(bundles_root, runner_config):
    bridge = make_bridge(bundles_root, runner_config)
    for bad in ["../secrets", "a/b", "..", ""]:
        with pytest.raises(BridgeError):
            bridge.dispatch("get_run_report", {"run_id": bad})


def test_unknown_workflow_and_tool(bundles_root, runner_config):
    bridge = make_bridge(bundles_root, runner_config)
    with pytest.raises(BridgeError, match="unknown workflow"):
        bridge.dispatch("get_workflow", {"workflow": "nope"})
    with pytest.raises(BridgeError, match="unknown tool"):
        bridge.dispatch("frobnicate", {})


def test_unloadable_bundle_listed_with_error_not_run(tmp_path, runner_config):
    bad = tmp_path / "bundles" / "broken"
    bad.mkdir(parents=True)
    (bad / "workflow.json").write_text("{not json")
    bridge = AgentBridge(tmp_path / "bundles", runner_config, allow_run=True)
    listing = bridge.dispatch("list_workflows", {})
    assert listing["workflows"][0]["load_error"]
    names = [s.name for s in bridge.list_tool_specs()]
    assert not any(n.startswith("run_") for n in names)


def test_tool_result_is_json_serializable(
    monkeypatch, bundles_root, runner_config, halt_report
):
    stub = FlowCliStub(exit_code=1, report=halt_report)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config, allow_run=True)
    result = bridge.dispatch("run_demo_triage", {})
    json.dumps(result)  # must not raise


def test_certify_forwarded_when_policy_configured(
    monkeypatch, bundles_root, tmp_path
):
    config = RunnerConfig(
        flow_cli=("openadapt-flow-stub",),
        runs_dir=tmp_path / "runs",
        policy="clinical-write",
    )
    stub = FlowCliStub(exit_code=2)
    stub.stdout = "certify FAILED: unverified write"
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = AgentBridge(bundles_root, config)
    info = bridge.dispatch("get_workflow", {"workflow": "demo_triage"})
    assert info["certification"]["certified"] is False
    cmd = stub.calls[0]
    assert cmd[1] == "certify"
    assert cmd[cmd.index("--policy") + 1] == "clinical-write"
