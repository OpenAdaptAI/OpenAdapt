"""Bridge tools keep protected workflow and run evidence local by default."""

from __future__ import annotations

import json
import re

import pytest
from conftest import FlowCliStub

import openadapt_agent.runner as runner_mod
from openadapt_agent.bridge import AgentBridge, BridgeError
from openadapt_agent.bundles import tool_input_schema
from openadapt_agent.runner import RunnerConfig


def make_bridge(
    bundles_root,
    runner_config,
    allow_run=False,
    **kwargs,
):
    return AgentBridge(
        bundles_root,
        runner_config,
        allow_run=allow_run,
        **kwargs,
    )


def workflow_id(bridge: AgentBridge) -> str:
    assert len(bridge.workflows) == 1
    return next(iter(bridge.workflows))


def run_tool(bridge: AgentBridge) -> str:
    return f"run_{workflow_id(bridge)}"


def test_schema_uses_opaque_id_requires_params_and_exports_no_examples(
    bundles_root,
    runner_config,
):
    bridge = make_bridge(bundles_root, runner_config, allow_run=True)
    workflow = workflow_id(bridge)
    assert re.fullmatch(r"workflow_[0-9a-f]{24}", workflow)
    spec = {item.name: item for item in bridge.list_tool_specs()}[f"run_{workflow}"]
    schema = spec.input_schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["note"]
    assert schema["properties"]["note"] == {
        "type": "string",
        "description": "Value for the declared 'note' workflow parameter.",
    }
    serialized = json.dumps(spec.__dict__)
    assert "Follow-up in 2 weeks" not in serialized
    assert "Demo Triage" not in serialized


def test_url_property_only_with_override_flag(bundle_dir):
    from openadapt_agent.bundles import load_workflow_info

    info = load_workflow_info(bundle_dir)
    with_url = tool_input_schema(info, allow_url_override=True)
    without_url = tool_input_schema(info, allow_url_override=False)
    assert "url" in with_url["properties"]
    assert "url" not in without_url["properties"]


def test_read_only_tools_and_workflow_listing_are_phi_safe(
    bundles_root,
    runner_config,
):
    bridge = make_bridge(bundles_root, runner_config, allow_run=False)
    names = [spec.name for spec in bridge.list_tool_specs()]
    assert names == [
        "list_workflows",
        "get_workflow",
        "get_run_report",
        "list_needs_attention",
        "get_attention_item",
    ]
    listing = bridge.dispatch("list_workflows", {})
    assert listing["lifecycle"] == "beta"
    assert listing["run_tools_enabled"] is False
    assert listing["protected_export_enabled"] is False
    item = listing["workflows"][0]
    assert re.fullmatch(r"workflow_[0-9a-f]{24}", item["id"])
    assert item["step_count"] == 2
    assert item["parameters"] == [{"name": "note", "type": "string", "required": True}]
    serialized = json.dumps(listing)
    for protected in (
        "Demo Triage",
        "Follow-up in 2 weeks",
        "Open the patient chart",
        str(bundles_root),
    ):
        assert protected not in serialized

    with pytest.raises(BridgeError, match="--allow-run"):
        bridge.dispatch(f"run_{item['id']}", {"note": "fresh value"})


def test_get_workflow_returns_structural_metadata_not_recorded_content(
    bundles_root,
    runner_config,
):
    bridge = make_bridge(bundles_root, runner_config)
    workflow = workflow_id(bridge)
    info = bridge.dispatch("get_workflow", {"workflow": workflow})
    assert info["id"] == workflow
    assert info["step_count"] == 2
    assert info["parameters"][0]["name"] == "note"
    assert info["certification"]["certified"] is None
    serialized = json.dumps(info)
    assert "Demo Triage" not in serialized
    assert "Follow-up in 2 weeks" not in serialized
    assert "Open the patient chart" not in serialized
    assert str(bundles_root) not in serialized


def test_run_success_and_report_roundtrip_are_phi_safe(
    monkeypatch,
    bundles_root,
    runner_config,
    success_report,
):
    secret = "Jane Roe MRN-9911 bearer sk_live_secret"
    success_report["workflow_name"] = secret
    success_report["results"][0]["intent"] = secret
    success_report["stdout"] = secret
    stub = FlowCliStub(exit_code=0, report=success_report)
    stub.stdout = secret
    stub.stderr = secret
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config, allow_run=True)
    result = bridge.dispatch(run_tool(bridge), {"note": "custom note"})
    assert result["status"] == "success"
    assert result["success"] is True
    assert re.fullmatch(r"run-[0-9a-f]{24}", result["run_id"])
    assert "protected" not in result
    assert secret not in json.dumps(result)
    assert str(runner_config.runs_dir) not in json.dumps(result)

    fetched = bridge.dispatch("get_run_report", {"run_id": result["run_id"]})
    assert fetched["status"] == "success"
    assert fetched["summary"]["steps_ok"] == 2
    assert "protected" not in fetched
    assert secret not in json.dumps(fetched)
    assert str(runner_config.runs_dir) not in json.dumps(fetched)


def test_run_halt_returns_safe_card_not_raw_evidence(
    monkeypatch,
    bundles_root,
    runner_config,
    halt_report,
):
    secret = "Jane Roe MRN-9911 Unexpected secret dialog"
    halt_report["workflow_name"] = secret
    halt_report["halt"]["reason"] = secret
    halt_report["halt"]["observed_texts"] = [secret]
    halt_report["results"][1]["error"] = secret
    stub = FlowCliStub(exit_code=1, report=halt_report)
    stub.stdout = secret
    stub.stderr = secret
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config, allow_run=True)
    result = bridge.dispatch(run_tool(bridge), {"note": "fresh note"})
    assert result["status"] == "halt"
    assert result["success"] is False
    assert "halt" not in result
    assert "protected" not in result
    assert "needs_attention" in result
    assert secret not in json.dumps(result)
    assert str(runner_config.runs_dir) not in json.dumps(result)


def test_missing_or_unknown_parameters_refuse_before_execution(
    monkeypatch,
    bundles_root,
    runner_config,
):
    stub = FlowCliStub(exit_code=0)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config, allow_run=True)
    tool = run_tool(bridge)
    with pytest.raises(BridgeError, match="declared workflow parameter schema"):
        bridge.dispatch(tool, {})
    with pytest.raises(BridgeError, match="declared workflow parameter schema"):
        bridge.dispatch(tool, {"note": "fresh", "secret-unknown": "x"})
    with pytest.raises(BridgeError, match="declared workflow parameter schema"):
        bridge.dispatch(tool, {"note": {"unexpected": "object"}})
    assert stub.calls == []


def test_synthetic_recorded_default_reuse_is_separate_explicit_mode(
    monkeypatch,
    bundles_root,
    runner_config,
    success_report,
):
    stub = FlowCliStub(exit_code=0, report=success_report)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(
        bundles_root,
        runner_config,
        allow_run=True,
        allow_recorded_defaults=True,
    )
    spec = {item.name: item for item in bridge.list_tool_specs()}[run_tool(bridge)]
    assert spec.input_schema["required"] == []
    assert "default" not in spec.input_schema["properties"]["note"]
    assert "Follow-up in 2 weeks" not in json.dumps(spec.__dict__)
    assert bridge.dispatch(run_tool(bridge), {})["status"] == "success"


def test_protected_export_is_explicit_and_preserves_local_detail(
    monkeypatch,
    bundles_root,
    runner_config,
    halt_report,
):
    secret = "Jane Roe MRN-9911 protected halt"
    halt_report["halt"]["reason"] = secret
    stub = FlowCliStub(exit_code=1, report=halt_report)
    stub.stderr = secret
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(
        bundles_root,
        runner_config,
        allow_run=True,
        allow_protected_export=True,
    )
    listing = bridge.dispatch("list_workflows", {})
    assert listing["protected_export_enabled"] is True
    assert listing["protected"]["bundles_dir"] == str(bundles_root)
    workflow = workflow_id(bridge)
    detail = bridge.dispatch("get_workflow", {"workflow": workflow})
    assert detail["protected"]["name"] == "Demo Triage"
    assert detail["protected"]["recorded_params"]["note"] == "Follow-up in 2 weeks"

    result = bridge.dispatch(f"run_{workflow}", {"note": "fresh"})
    assert secret in json.dumps(result["protected"])
    report = bridge.dispatch("get_run_report", {"run_id": result["run_id"]})
    assert secret in json.dumps(report["protected"])
    assert str(runner_config.runs_dir) in json.dumps(report["protected"])


def test_get_run_report_traversal_and_symlink_are_refused(
    bundles_root,
    runner_config,
    tmp_path,
):
    bridge = make_bridge(bundles_root, runner_config)
    for bad in ["../secrets", "a/b", "..", ""]:
        with pytest.raises(BridgeError):
            bridge.dispatch("get_run_report", {"run_id": bad})

    runs = runner_config.runs_dir
    runs.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.json").write_text('{"success": true}')
    (runs / ("run-" + "a" * 24)).symlink_to(
        outside,
        target_is_directory=True,
    )
    with pytest.raises(BridgeError, match="boundary"):
        bridge.dispatch(
            "get_run_report",
            {"run_id": "run-" + "a" * 24},
        )


def test_unreadable_report_exception_text_stays_local(
    bundles_root,
    runner_config,
):
    secret = "Jane Roe MRN-9911 sk_live_secret"
    run_id = "run-" + "b" * 24
    run = runner_config.runs_dir / run_id
    run.mkdir(parents=True)
    (run / "report.json").write_text("{not-json " + secret)
    bridge = make_bridge(bundles_root, runner_config)

    with pytest.raises(BridgeError) as error:
        bridge.dispatch("get_run_report", {"run_id": run_id})
    assert secret not in str(error.value)
    assert str(run) not in str(error.value)
    assert "could not be read safely" in str(error.value)


def test_unknown_workflow_and_tool_do_not_echo_untrusted_names(
    bundles_root,
    runner_config,
):
    secret = "Jane Roe sk_live_secret"
    bridge = make_bridge(bundles_root, runner_config)
    with pytest.raises(BridgeError) as workflow_error:
        bridge.dispatch("get_workflow", {"workflow": secret})
    assert secret not in str(workflow_error.value)
    with pytest.raises(BridgeError) as tool_error:
        bridge.dispatch(secret, {})
    assert secret not in str(tool_error.value)


def test_unloadable_bundle_is_safe_by_default_and_not_runnable(
    tmp_path,
    runner_config,
):
    secret = "Jane Roe MRN-9911 sk_live_secret"
    bad = tmp_path / f"bundles-{secret}" / "broken"
    bad.mkdir(parents=True)
    (bad / "workflow.json").write_text("{not json " + secret)
    bridge = AgentBridge(bad.parent, runner_config, allow_run=True)
    listing = bridge.dispatch("list_workflows", {})
    assert listing["workflows"][0]["available"] is False
    assert secret not in json.dumps(listing)
    assert str(bad) not in json.dumps(listing)
    names = [spec.name for spec in bridge.list_tool_specs()]
    assert not any(name.startswith("run_") for name in names)


def test_tool_result_is_json_serializable(
    monkeypatch,
    bundles_root,
    runner_config,
    halt_report,
):
    stub = FlowCliStub(exit_code=1, report=halt_report)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config, allow_run=True)
    result = bridge.dispatch(run_tool(bridge), {"note": "fresh"})
    json.dumps(result)


def test_certify_result_is_fixed_copy_unless_protected_export_enabled(
    monkeypatch,
    bundles_root,
    tmp_path,
):
    secret = "Jane Roe MRN-9911 unverified write"
    config = RunnerConfig(
        flow_cli=("openadapt-flow-stub",),
        runs_dir=tmp_path / "runs",
        policy="clinical-write",
    )
    stub = FlowCliStub(exit_code=2)
    stub.stdout = secret
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = AgentBridge(bundles_root, config)
    workflow = workflow_id(bridge)
    info = bridge.dispatch("get_workflow", {"workflow": workflow})
    assert info["certification"]["certified"] is False
    assert secret not in json.dumps(info)
    cmd = stub.calls[0]
    assert cmd[1] == "certify"
    assert cmd[cmd.index("--policy") + 1] == "clinical-write"


def test_workflow_names_intents_recorded_values_and_paths_never_default_export(
    tmp_path,
):
    from openadapt_flow.ir import ActionKind, Step, Workflow

    secret = "Jane Roe MRN-9911 sk_live_secret"
    bundle = tmp_path / secret / "bundle"
    Workflow(
        name=secret,
        params={"patient_id": secret},
        steps=[
            Step(
                id="step",
                intent=secret,
                action=ActionKind.KEY,
                key="Tab",
            )
        ],
    ).save(bundle)
    config = RunnerConfig(
        flow_cli=("openadapt-flow-stub",),
        runs_dir=tmp_path / "runs",
    )
    bridge = AgentBridge(bundle, config, allow_run=True)
    workflow = workflow_id(bridge)
    surfaces = [
        bridge.dispatch("list_workflows", {}),
        bridge.dispatch("get_workflow", {"workflow": workflow}),
        [spec.__dict__ for spec in bridge.list_tool_specs()],
    ]
    serialized = json.dumps(surfaces)
    assert secret not in serialized
    assert str(bundle) not in serialized
    assert "patient_id" in serialized
