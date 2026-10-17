"""The sandbox path with real openadapt-flow, a real browser, and MockMed.

No fakes: Flow records the synthetic workflow once, then each case runs under
the standard profile with Flow's run gate, right-record checks, and an
independent read of MockMed's record store. Opt in with OPENADAPT_AGENT_E2E=1
(it takes a few minutes and needs the tutorial extra plus Chromium). CI runs
it in the sandbox-e2e job.
"""

from __future__ import annotations

import json
import os
from urllib.request import urlopen

import jsonschema
import pytest

from openadapt_agent.bridge import AgentBridge
from openadapt_agent.contract import RUN_RESULT_SCHEMA
from openadapt_agent.runner import RunnerConfig
from openadapt_agent.sandbox import SANDBOX_WORKFLOW, SandboxEngine, flow_engine_available

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENADAPT_AGENT_E2E") != "1",
    reason="set OPENADAPT_AGENT_E2E=1 to drive MockMed with real openadapt-flow",
)

NOTE = "Synthetic: recheck blood pressure in 2 weeks"


@pytest.fixture(scope="module")
def flow_sandbox(tmp_path_factory):
    available, why = flow_engine_available()
    if not available:
        pytest.skip(f"flow sandbox engine unavailable: {why}")
    os.environ.setdefault("DO_NOT_TRACK", "1")
    os.environ.setdefault("OPENADAPT_TELEMETRY", "0")
    runs = tmp_path_factory.mktemp("flow-sandbox")
    engine = SandboxEngine(runs, engine="flow")
    bridge = AgentBridge(None, RunnerConfig(runs_dir=runs), sandbox=engine, mode="sandbox")
    yield bridge, engine
    engine.close()


def call(bridge, case, request_id, note=NOTE):
    result = bridge.dispatch(
        "run_workflow",
        {
            "workflow": SANDBOX_WORKFLOW,
            "inputs": {"note": note, "sandbox_case": case},
            "request_id": request_id,
            "wait_seconds": 600,
        },
    )
    jsonschema.validate(result, RUN_RESULT_SCHEMA)
    return result


def store_records(engine):
    base = engine._session.base_url.rstrip("/")
    with urlopen(f"{base}/api/db", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))["records"]


def test_normal_case_saves_the_callers_note_and_checks_it(flow_sandbox):
    bridge, engine = flow_sandbox
    result = call(bridge, "normal", "e2e-normal")
    assert engine.engine == "flow"
    assert result["outcome"] == "done", result
    assert result["proof"] == "local"
    assert result["sandbox"] == {"case": "normal", "engine": "flow"}
    assert result["technical"]["transaction_outcome"] == "VERIFIED"
    assert result["model_calls"] == 0
    records = store_records(engine)
    assert [record["note"] for record in records] == [NOTE]

    again = call(bridge, "normal", "e2e-normal")
    assert again["run_id"] == result["run_id"]
    assert again["replayed"] is True
    assert len(store_records(engine)) == 1


def test_duplicate_record_stops_before_saving(flow_sandbox):
    bridge, engine = flow_sandbox
    result = call(bridge, "duplicate_record", "e2e-duplicate")
    assert result["outcome"] == "needs_review", result
    assert result["record_changed"] == "no"
    assert result["safe_to_retry"] is False
    assert result["technical"]["transaction_outcome"] == "HALTED_BEFORE_EFFECT"
    assert store_records(engine) == []


def test_false_saved_banner_is_not_sure_if_saved(flow_sandbox):
    bridge, engine = flow_sandbox
    result = call(bridge, "false_saved_banner", "e2e-banner")
    assert result["outcome"] == "not_sure_if_saved", result
    assert result["safe_to_retry"] is False
    assert result["technical"]["transaction_outcome"] == "RECONCILIATION_REQUIRED"
    assert store_records(engine) == []


def test_timeout_after_save_is_not_sure_even_though_it_saved(flow_sandbox):
    bridge, engine = flow_sandbox
    result = call(bridge, "timeout_after_save", "e2e-timeout")
    assert result["outcome"] == "not_sure_if_saved", result
    assert result["safe_to_retry"] is False
    # The write landed. Saying "nothing changed" here would be false.
    assert [record["note"] for record in store_records(engine)] == [NOTE]


def test_app_offline_did_not_run(flow_sandbox):
    bridge, _engine = flow_sandbox
    result = call(bridge, "app_offline", "e2e-offline")
    assert result["outcome"] == "did_not_run", result
    assert result["reason"] == "app_unreachable"
    assert result["safe_to_retry"] is True
