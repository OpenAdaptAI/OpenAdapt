"""The zero-flag sandbox shows every outcome (simulated engine; no browser).

tests/test_sandbox_flow.py drives the same cases with real openadapt-flow.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from openadapt_agent.bridge import AgentBridge
from openadapt_agent.contract import RUN_RESULT_SCHEMA
from openadapt_agent.runner import RunnerConfig
from openadapt_agent.sandbox import CASES, SANDBOX_WORKFLOW, SandboxEngine


@pytest.fixture()
def sandbox_bridge(tmp_path):
    runs = tmp_path / "sandbox"
    engine = SandboxEngine(runs, engine="simulated")
    bridge = AgentBridge(None, RunnerConfig(runs_dir=runs), sandbox=engine, mode="sandbox")
    yield bridge
    engine.close()


def run(bridge, case=None, request_id="demo-0001", note="Synthetic follow-up in 2 weeks"):
    inputs = {"note": note}
    if case is not None:
        inputs["sandbox_case"] = case
    return bridge.dispatch(
        "run_workflow",
        {"workflow": SANDBOX_WORKFLOW, "inputs": inputs, "request_id": request_id},
    )


def test_sandbox_lists_one_workflow_with_cases(sandbox_bridge):
    names = [spec.name for spec in sandbox_bridge.list_tool_specs()]
    assert names == ["list_workflows", "run_workflow", "get_run"]
    listing = sandbox_bridge.dispatch("list_workflows", {})
    assert listing["mode"] == "sandbox"
    workflow = listing["workflows"][0]
    assert workflow["name"] == SANDBOX_WORKFLOW
    assert workflow["inputs"]["required"] == ["note"]
    assert workflow["inputs"]["properties"]["sandbox_case"]["enum"] == list(CASES)
    assert listing["sandbox"]["engine"] == "simulated"
    assert {case["outcome"] for case in listing["sandbox"]["cases"]} == {
        "done",
        "needs_review",
        "not_sure_if_saved",
        "did_not_run",
    }


@pytest.mark.parametrize("case", list(CASES))
def test_each_case_returns_its_outcome(sandbox_bridge, case):
    result = run(sandbox_bridge, case=case, request_id=f"demo-{case}")
    jsonschema.validate(result, RUN_RESULT_SCHEMA)
    assert result["outcome"] == CASES[case].outcome
    assert result["mode"] == "sandbox"
    assert result["sandbox"] == {"case": case, "engine": "simulated"}
    assert result["what_happened"].startswith("Simulated sandbox result")
    if result["outcome"] == "done":
        assert result["proof"] == "simulated"
    if result["outcome"] == "not_sure_if_saved":
        assert result["safe_to_retry"] is False
        assert "did not change" not in result["what_happened"]


def test_app_offline_says_the_app_is_unreachable(sandbox_bridge):
    result = run(sandbox_bridge, case="app_offline", request_id="demo-offline")
    assert result["reason"] == "app_unreachable"
    assert result["safe_to_retry"] is True
    assert "isn't reachable" in result["what_happened"]


def test_default_case_is_normal(sandbox_bridge):
    assert run(sandbox_bridge)["outcome"] == "done"


@pytest.mark.parametrize("note", ["", "x" * 501])
def test_invalid_note_did_not_run(sandbox_bridge, note):
    result = run(sandbox_bridge, note=note)
    assert result["outcome"] == "did_not_run"
    assert result["reason"] == "invalid_input"
    assert result["invalid_inputs"][0]["input"] == "note"


def test_same_request_id_replays(sandbox_bridge):
    first = run(sandbox_bridge, case="false_saved_banner")
    again = run(sandbox_bridge, case="false_saved_banner")
    assert again["run_id"] == first["run_id"]
    assert again["replayed"] is True
    changed = run(sandbox_bridge, case="normal")
    assert changed["reason"] == "request_id_conflict"


def test_get_run_returns_the_stored_result(sandbox_bridge):
    first = run(sandbox_bridge, case="duplicate_record")
    fetched = sandbox_bridge.dispatch("get_run", {"run_id": first["run_id"]})
    assert fetched == first
    assert json.dumps(fetched)


def test_legacy_tools_are_not_offered_in_the_sandbox(sandbox_bridge):
    from openadapt_agent.bridge import BridgeError

    with pytest.raises(BridgeError):
        sandbox_bridge.dispatch("list_needs_attention", {})


def test_omitted_default_and_explicit_default_are_the_same_request(sandbox_bridge):
    first = run(sandbox_bridge, request_id="demo-default")
    explicit = run(sandbox_bridge, case="normal", request_id="demo-default")
    assert explicit["run_id"] == first["run_id"]
    assert explicit["replayed"] is True
