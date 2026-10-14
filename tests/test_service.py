"""run_workflow / get_run over operator bundles, with a stubbed Flow CLI."""

from __future__ import annotations

import json
import os
import time

import anyio
import jsonschema
import pytest
from conftest import FlowCliStub

import openadapt_agent.runner as runner_mod
from openadapt_agent.bridge import AgentBridge, BridgeError
from openadapt_agent.contract import RUN_RESULT_SCHEMA
from openadapt_agent.mcp import build_server
from openadapt_agent.runs import RunStore


def wire(model):
    """Both SDK generations serialize the same protocol aliases."""
    return model.model_dump(by_alias=True, mode="json")


def make_bridge(bundles_root, runner_config, **kwargs):
    kwargs.setdefault("allow_run", True)
    return AgentBridge(bundles_root, runner_config, **kwargs)


def workflow_name(bridge):
    return next(iter(bridge.catalog))


def run(bridge, request_id="ref-0001", inputs=None, wait=10, workflow=None):
    return bridge.dispatch(
        "run_workflow",
        {
            "workflow": workflow or workflow_name(bridge),
            "inputs": {"note": "fresh note"} if inputs is None else inputs,
            "request_id": request_id,
            "wait_seconds": wait,
        },
    )


def test_verified_run_is_done_and_the_same_request_never_runs_twice(
    monkeypatch, bundles_root, runner_config, success_report
):
    stub = FlowCliStub(exit_code=0, report=success_report)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config)

    first = run(bridge)
    assert first["outcome"] == "done"
    assert first["proof"] == "local"
    assert first["request_id"] == "ref-0001"
    assert first["workflow"] == workflow_name(bridge)
    jsonschema.validate(first, RUN_RESULT_SCHEMA)

    again = run(bridge)
    assert again["run_id"] == first["run_id"]
    assert again["replayed"] is True
    assert len(stub.calls) == 1


def test_list_workflows_returns_cards_and_legacy_fields(bundles_root, runner_config):
    listing = make_bridge(bundles_root, runner_config).dispatch("list_workflows", {})
    item = listing["workflows"][0]
    assert item["name"].startswith("workflow_")
    assert item["id"] == item["name"]
    assert item["purpose"]
    assert item["done_means"]
    assert item["inputs"]["required"] == ["note"]
    assert item["inputs"]["properties"]["note"]["type"] == "string"
    assert "Follow-up in 2 weeks" not in json.dumps(listing)
    assert listing["how_to_run"].startswith("Call run_workflow")


def test_invalid_input_did_not_run_and_leaves_a_retrievable_record(
    monkeypatch, bundles_root, runner_config
):
    stub = FlowCliStub(exit_code=0)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config)
    secret = "Jane Roe MRN 8812345"
    result = run(bridge, inputs={"note": 5, "patient": secret})
    assert result["outcome"] == "did_not_run"
    assert result["reason"] == "invalid_input"
    assert result["safe_to_retry"] is True
    assert {"input": "note", "problem": "must be text"} in result["invalid_inputs"]
    assert secret not in json.dumps(result)
    assert stub.calls == []
    fetched = bridge.dispatch("get_run", {"run_id": result["run_id"]})
    assert fetched == result
    # The fixed request may reuse the same request_id.
    stub.report = None
    stub.exit_code = 2
    retried = run(bridge)
    assert retried["reason"] == "not_ready_to_run"
    assert len(stub.calls) == 1


def test_unknown_workflow_never_echoes_the_name(bundles_root, runner_config):
    bridge = make_bridge(bundles_root, runner_config)
    result = run(bridge, workflow="Jane Roe chart")
    assert result["reason"] == "unknown_workflow"
    assert result["workflow"] is None
    assert "Jane Roe" not in json.dumps(result)


def test_refusal_is_retryable_with_the_same_request_id(
    monkeypatch, bundles_root, runner_config, success_report
):
    stub = FlowCliStub(exit_code=2, report=None)
    stub.stdout = "  [REFUSE] Certification passed: 8 violations\n"
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config)
    refused = run(bridge)
    assert refused["outcome"] == "did_not_run"
    assert refused["failed_checks"] == ["not_certified"]
    assert bridge.dispatch("get_run_report", {"run_id": refused["run_id"]})["status"] == "refused"

    stub.exit_code = 0
    stub.report = success_report
    done = run(bridge)
    assert done["outcome"] == "done"
    assert done["run_id"] != refused["run_id"]
    assert len(stub.calls) == 2


@pytest.mark.parametrize("transaction", ["RECONCILIATION_REQUIRED", "HALTED_BEFORE_EFFECT"])
def test_uncertain_or_reviewed_runs_are_never_run_again(
    monkeypatch, bundles_root, runner_config, halt_report, transaction
):
    halt_report["transaction_outcome"] = transaction
    stub = FlowCliStub(exit_code=1, report=halt_report)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config)
    first = run(bridge)
    assert first["safe_to_retry"] is False
    assert first["outcome"] in {"not_sure_if_saved", "needs_review"}
    again = run(bridge)
    assert again["run_id"] == first["run_id"]
    assert len(stub.calls) == 1


def test_reused_request_id_with_new_inputs_is_refused(
    monkeypatch, bundles_root, runner_config, success_report
):
    monkeypatch.setattr(
        runner_mod.subprocess, "run", FlowCliStub(exit_code=0, report=success_report)
    )
    bridge = make_bridge(bundles_root, runner_config)
    first = run(bridge)
    conflict = run(bridge, inputs={"note": "a different note"})
    assert conflict["reason"] == "request_id_conflict"
    assert conflict["first_run_id"] == first["run_id"]
    assert conflict["safe_to_retry"] is False


def test_slow_run_returns_running_then_get_run_waits_for_it(
    monkeypatch, bundles_root, runner_config, success_report
):
    stub = FlowCliStub(exit_code=0, report=success_report)

    def slow(*args, **kwargs):
        time.sleep(0.5)
        return stub(*args, **kwargs)

    monkeypatch.setattr(runner_mod.subprocess, "run", slow)
    bridge = make_bridge(bundles_root, runner_config)
    started = run(bridge, wait=0)
    assert started["outcome"] == "running"
    assert started["next_step"] == "call_get_run"
    jsonschema.validate(started, RUN_RESULT_SCHEMA)
    finished = bridge.dispatch("get_run", {"run_id": started["run_id"], "wait_seconds": 10})
    assert finished["outcome"] == "done"


def test_run_left_by_a_dead_server_reads_as_interrupted(bundles_root, runner_config):
    store = RunStore(runner_config.runs_dir)
    begin = store.begin(request_id="ref-0009", workflow="w", inputs={}, mode="production")
    record = dict(begin.record, pid=2**22 + 12345)
    store.write(record)
    bridge = make_bridge(bundles_root, runner_config)
    result = bridge.dispatch("get_run", {"run_id": begin.run_id, "wait_seconds": 0})
    assert result["outcome"] == "not_sure_if_saved"
    assert result["reason"] == "interrupted"
    assert result["safe_to_retry"] is False


def test_review_resolved_by_a_person_updates_get_run(
    monkeypatch, bundles_root, runner_config, halt_report, success_report
):
    monkeypatch.setattr(runner_mod.subprocess, "run", FlowCliStub(exit_code=1, report=halt_report))
    bridge = make_bridge(bundles_root, runner_config)
    paused = run(bridge)
    assert paused["outcome"] == "needs_review"
    # A person finishes the step; Flow resumes the durable run and rewrites it.
    report_path = runner_config.runs_dir / paused["run_id"] / "report.json"
    report_path.write_text(json.dumps(success_report))
    resolved = bridge.dispatch("get_run", {"run_id": paused["run_id"]})
    assert resolved["outcome"] == "done"
    assert run(bridge)["outcome"] == "done"


def test_continued_pause_never_turns_into_a_retryable_result(
    monkeypatch, bundles_root, runner_config, halt_report, success_report
):
    """Regression: a reviewed run must never invite a second write.

    When a person continues a pause, Flow clears the pause just before it
    replaces the pre-resume report. A get_run in that moment sees no pause
    and the old REJECTED_POLICY report. That must not become a retryable
    did_not_run, or the same request_id would start a second attempt while
    the resumed run writes.
    """
    halt_report["transaction_outcome"] = "REJECTED_POLICY"
    stub = FlowCliStub(exit_code=1, report=halt_report)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config)
    pause = {
        "item": {"id": "a1", "status": "pending", "durably_paused": True, "category": "identity"}
    }
    monkeypatch.setattr(bridge.attended, "for_run_dir", lambda run_dir: pause["item"])
    paused = run(bridge)
    assert paused["outcome"] == "needs_review"

    pause["item"] = {"id": "a1", "status": "halted", "durably_paused": False, "category": "identity"}
    during = bridge.dispatch("get_run", {"run_id": paused["run_id"]})
    assert during["outcome"] == "needs_review"
    assert during["safe_to_retry"] is False
    assert run(bridge)["run_id"] == paused["run_id"]
    assert len(stub.calls) == 1

    # The resumed run's report lands, and get_run reports it.
    report_path = runner_config.runs_dir / paused["run_id"] / "report.json"
    report_path.write_text(json.dumps(success_report))
    pause["item"] = None
    assert bridge.dispatch("get_run", {"run_id": paused["run_id"]})["outcome"] == "done"


def test_get_run_rejects_unknown_and_unsafe_ids(bundles_root, runner_config):
    bridge = make_bridge(bundles_root, runner_config)
    with pytest.raises(BridgeError):
        bridge.dispatch("get_run", {"run_id": "../secrets"})
    with pytest.raises(BridgeError, match="no run"):
        bridge.dispatch("get_run", {"run_id": "run-" + "a" * 24})


def test_read_only_server_has_no_run_workflow_and_creates_nothing(bundles_root, runner_config):
    bridge = AgentBridge(bundles_root, runner_config)
    assert "run_workflow" not in [spec.name for spec in bridge.list_tool_specs()]
    with pytest.raises(BridgeError, match="runs are disabled"):
        run(bridge)
    bridge.dispatch("list_workflows", {})
    assert not (runner_config.runs_dir / "openadapt-agent").exists()


def test_mcp_returns_structured_content_that_the_client_validates(
    monkeypatch, bundles_root, runner_config, success_report, mcp_client
):
    monkeypatch.setattr(
        runner_mod.subprocess, "run", FlowCliStub(exit_code=0, report=success_report)
    )
    bridge = make_bridge(bundles_root, runner_config)
    server = build_server(bridge)

    async def call():
        async with mcp_client(server) as client:
            listing = wire(await client.call_tool("list_workflows", {}))
            name = listing["structuredContent"]["workflows"][0]["name"]
            result = await client.call_tool(
                "run_workflow",
                {"workflow": name, "inputs": {"note": "x"}, "request_id": "ref-0042"},
            )
            missing = await client.call_tool(
                "run_workflow", {"workflow": name, "inputs": {"note": "x"}}
            )
            return wire(result), wire(missing)

    result, missing = anyio.run(call)
    assert result["isError"] is False
    assert result["structuredContent"]["outcome"] == "done"
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert missing["isError"] is True
    assert "request_id" in missing["content"][0]["text"]


def test_records_hold_no_input_values(monkeypatch, bundles_root, runner_config, success_report):
    monkeypatch.setattr(
        runner_mod.subprocess, "run", FlowCliStub(exit_code=0, report=success_report)
    )
    bridge = make_bridge(bundles_root, runner_config)
    secret = "Jane Roe MRN 8812345"
    run(bridge, inputs={"note": secret})
    for root, _dirs, files in os.walk(runner_config.runs_dir / "openadapt-agent"):
        for name in files:
            if name.endswith(".json"):
                assert secret not in open(os.path.join(root, name)).read()


def test_attention_lookup_works_under_a_symlinked_runs_directory(monkeypatch, tmp_path):
    from openadapt_agent.attended import AttendedBridge

    real = tmp_path / "real"
    (real / "runs" / "run-abc").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    seen = {}

    def fake_attention_item(root, path):
        seen["relative"] = path.relative_to(root)
        return None

    monkeypatch.setattr("openadapt_flow.console.attention.attention_item", fake_attention_item)
    bridge = AttendedBridge(tmp_path / "link" / "runs")
    assert bridge.for_run_dir(tmp_path / "link" / "runs" / "run-abc") is None
    assert str(seen["relative"]) == "run-abc"


def test_failed_attention_lookup_waits_for_a_person_instead_of_inviting_retry(tmp_path):
    from openadapt_agent.contract import REASONS, reason_for_run
    from openadapt_agent.service import attention_for

    class Broken:
        def for_run_dir(self, run_dir):
            raise ValueError("boom")

    attention = attention_for(Broken(), tmp_path)
    report = {
        "success": False,
        "execution_outcome": "HALTED",
        "transaction_outcome": "REJECTED_POLICY",
        "execution_profile": "standard",
        "production_eligible": False,
    }
    reason = reason_for_run(exit_code=1, report=report, attention=attention)
    assert REASONS[reason].outcome == "needs_review"
    assert REASONS[reason].safe_to_retry is False


@pytest.mark.parametrize("request_id", ["", "abc", "has space", "x" * 200, None, 42])
def test_bad_request_id_is_refused_before_anything_runs(
    monkeypatch, bundles_root, runner_config, request_id
):
    stub = FlowCliStub(exit_code=0)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config)
    with pytest.raises(BridgeError, match="request_id"):
        bridge.dispatch(
            "run_workflow",
            {"workflow": workflow_name(bridge), "inputs": {"note": "x"}, "request_id": request_id},
        )
    assert stub.calls == []


def test_a_run_finishing_during_get_is_never_marked_interrupted(
    monkeypatch, bundles_root, runner_config, success_report
):
    """Regression: the finish/active race must not overwrite a real result."""
    import threading

    gate = threading.Event()
    stub = FlowCliStub(exit_code=0, report=success_report)

    def held(*args, **kwargs):
        gate.wait(10)
        return stub(*args, **kwargs)

    monkeypatch.setattr(runner_mod.subprocess, "run", held)
    bridge = make_bridge(bundles_root, runner_config)
    started = run(bridge, wait=0)
    service = bridge.service
    real_read = service.store.read

    def read_then_finish(run_id):
        record = real_read(run_id)
        gate.set()
        time.sleep(0.5)  # let the worker write its result and leave
        return record

    monkeypatch.setattr(service.store, "read", read_then_finish)
    first = bridge.dispatch("get_run", {"run_id": started["run_id"], "wait_seconds": 0})
    monkeypatch.setattr(service.store, "read", real_read)
    assert first["outcome"] in {"running", "done"}
    final = bridge.dispatch("get_run", {"run_id": started["run_id"], "wait_seconds": 10})
    assert final["outcome"] == "done"


def test_same_request_id_while_a_run_is_starting_waits_instead_of_interrupting(
    monkeypatch, bundles_root, runner_config, success_report
):
    """Regression: a concurrent replay must not see a new run as abandoned."""
    import threading

    stub = FlowCliStub(exit_code=0, report=success_report)
    monkeypatch.setattr(runner_mod.subprocess, "run", stub)
    bridge = make_bridge(bundles_root, runner_config)
    service = bridge.service
    real_start = service._start
    starting = threading.Event()

    def slow_start(*args, **kwargs):
        starting.set()
        time.sleep(0.3)  # the moment between reserving and starting the worker
        return real_start(*args, **kwargs)

    monkeypatch.setattr(service, "_start", slow_start)
    results = {}
    first = threading.Thread(target=lambda: results.setdefault("first", run(bridge)))
    first.start()
    assert starting.wait(5)
    second = run(bridge)
    first.join(10)
    assert results["first"]["outcome"] == "done"
    assert second["outcome"] == "done", second
    assert second["replayed"] is True
    assert second["run_id"] == results["first"]["run_id"]
    assert len(stub.calls) == 1


def test_orphaned_run_reads_as_interrupted_through_get_run(bundles_root, runner_config):
    bridge = make_bridge(bundles_root, runner_config)
    store = bridge.service.store
    record = store.new_record(request_id="ref-0010", workflow="w", mode="production")
    record["pid"] = None
    store.write(record)
    result = bridge.dispatch("get_run", {"run_id": record["run_id"], "wait_seconds": 0})
    assert result["reason"] == "interrupted"


def test_run_owned_by_another_computer_is_not_overwritten(bundles_root, runner_config):
    bridge = make_bridge(bundles_root, runner_config)
    store = bridge.service.store
    record = store.new_record(request_id="ref-0011", workflow="w", mode="production")
    record.update(pid=2**22 + 7, host="another-computer.invalid")
    store.write(record)
    result = bridge.dispatch("get_run", {"run_id": record["run_id"], "wait_seconds": 0})
    assert result["outcome"] == "running"
    assert store.read(record["run_id"])["state"] == "running"


def test_attended_mode_with_another_flow_cli_is_refused(bundles_root, capsys):
    from openadapt_agent.cli import main

    argv = ["serve", "--mode", "attended", "--bundles", str(bundles_root), "--flow-cli", "x"]
    assert main(argv) == 2
    assert "cannot select a different runtime" in capsys.readouterr().err
