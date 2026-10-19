"""Task ledger: typed job state, fail-closed rules, and questions a person answers."""

from __future__ import annotations

import json

import anyio
import pytest

from openadapt_flow.agent.bridge import AgentBridge, BridgeError
from openadapt_flow.agent.cli import main
from openadapt_flow.agent.ledger import (
    ASK_WORKFLOW_NAME,
    LEDGER_TOOLS,
    TaskLedger,
)
from openadapt_flow.agent.ledger_demo import WORKFLOW, SyntheticReferralApp, measure
from openadapt_flow.agent.mcp import build_server
from openadapt_flow.agent.runner import RunnerConfig
from openadapt_flow.approval_mode.item_approval import ItemApprovalStore, make_answer
from openadapt_flow.approval_mode.model import ITEM_APPROVAL_ROUTE
from openadapt_flow.console.save_approvals import (
    LEDGER_QUESTION_WORKFLOW,
    _waiting,
    list_save_approvals,
    save_approval_detail,
)

RULES = [
    {
        "id": "clinic",
        "text": "North clinic only",
        "input": "clinic",
        "rule": "equals",
        "value": "north",
    }
]


@pytest.fixture
def world(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    app = SyntheticReferralApp()
    bridge = AgentBridge(None, RunnerConfig(runs_dir=runs), sandbox=app, mode="sandbox")
    ledger = TaskLedger(runs / "task-ledger")
    return runs, app, build_server(bridge, ledger=ledger), ledger


def _inputs(item="R01", clinic="north", **extra):
    return {
        "item": item,
        "patient_name": f"Synthetic Patient {item[1:]}",
        "clinic": clinic,
        "priority": "routine",
        **extra,
    }


def answer(runs, decision="approve", *, decided_by="human", route=ITEM_APPROVAL_ROUTE):
    """A person answers every waiting question, as the phone or Run it does."""
    answered = 0
    for run_dir, request in _waiting(runs):
        store = ItemApprovalStore(run_dir)
        store.write_answer(
            make_answer(
                request,
                decision=decision,
                decline_reason="wrong_values" if decision == "decline" else None,
                decided_by=decided_by,
                route=route,
                approver_id=None,
                auth_method="none",
                policy=store.context(),
            )
        )
        answered += 1
    return answered


def session(server, mcp_client, script):
    async def go():
        async with mcp_client(server) as client:

            async def call(name, args):
                result = await client.call_tool(name, args)
                failed = getattr(result, "is_error", None)
                if failed is None:
                    failed = getattr(result, "isError", False)
                return (not failed), json.loads(result.content[0].text)

            return await script(call)

    return anyio.run(go)


def test_ledger_tools_are_served_only_with_a_ledger(world, mcp_client, tmp_path):
    runs, app, server, _ = world

    async def listing(srv):
        async with mcp_client(srv) as client:
            return [tool.name for tool in (await client.list_tools()).tools]

    with_ledger = anyio.run(listing, server)
    assert set(LEDGER_TOOLS) <= set(with_ledger)
    assert "call open_job first" in (server.instructions or "")
    plain = build_server(
        AgentBridge(None, RunnerConfig(runs_dir=tmp_path / "r2"), sandbox=app, mode="sandbox")
    )
    assert not set(LEDGER_TOOLS) & set(anyio.run(listing, plain))


def test_question_name_matches_the_console():
    assert ASK_WORKFLOW_NAME == LEDGER_QUESTION_WORKFLOW


def test_a_run_that_breaks_a_locked_rule_is_refused_and_nothing_runs(world, mcp_client):
    runs, app, server, _ = world

    async def script(call):
        ok, job = await call(
            "open_job",
            {
                "intent": "Enter one synthetic referral.",
                "constraints": RULES,
                "items": [{"item_id": "R01"}],
            },
        )
        assert ok and job["items_total"] == 1
        request_id = job["not_verified_items"][0]["request_id"]
        refused = await call(
            "run_workflow",
            {"workflow": WORKFLOW, "inputs": _inputs(clinic="south"), "request_id": request_id},
        )
        missing = await call(
            "run_workflow",
            {
                "workflow": WORKFLOW,
                "inputs": {k: v for k, v in _inputs().items() if k != "clinic"},
                "request_id": request_id,
            },
        )
        other_id = await call(
            "run_workflow",
            {"workflow": WORKFLOW, "inputs": _inputs(), "request_id": job["job_id"] + ".R01.r9"},
        )
        ok_run = await call(
            "run_workflow",
            {"workflow": WORKFLOW, "inputs": _inputs(), "request_id": request_id},
        )
        ok, summary = await call("get_job", {"job_id": job["job_id"]})
        return refused, missing, other_id, ok_run, summary

    refused, missing, other_id, ok_run, summary = session(server, mcp_client, script)
    assert refused[0] is False and "breaks the job's rules" in refused[1]["error"]
    assert "Nothing ran" in refused[1]["error"]
    assert missing[0] is False
    assert other_id[0] is False and "current request_id" in other_id[1]["error"]
    assert ok_run[0] is True and ok_run[1]["outcome"] == "done"
    # Only the one allowed run reached the app.
    assert app.attempts == {"R01": 1}
    assert summary["counts"] == {"verified": 1}
    assert summary["refused_runs"] == 3
    assert summary["unverified"] == 0


def test_no_tool_lets_the_agent_set_a_status(world):
    _, _, _, ledger = world
    for spec in ledger.list_tool_specs():
        properties = spec.input_schema["properties"]
        assert "status" not in properties
        assert spec.input_schema.get("additionalProperties") is False
        for nested in ("items", "constraint"):
            schema = properties.get(nested, {})
            schema = schema.get("items", schema)
            assert "status" not in schema.get("properties", {})


def test_only_a_person_changes_a_locked_rule(world, mcp_client):
    runs, app, server, _ = world

    async def script(call):
        ok, job = await call(
            "open_job",
            {
                "intent": "Enter synthetic referrals.",
                "constraints": RULES,
                "items": [{"item_id": "R01"}, {"item_id": "R02"}],
            },
        )
        job_id = job["job_id"]
        ok, asked = await call(
            "change_constraint",
            {
                "job_id": job_id,
                "constraint": {
                    "id": "clinic",
                    "text": "South clinic",
                    "input": "clinic",
                    "rule": "equals",
                    "value": "south",
                },
                "source": "document",
            },
        )
        # Waiting: the old rule still holds.
        ok, summary = await call("get_job", {"job_id": job_id})
        rule_while_waiting = summary["constraints"][0]
        await anyio.to_thread.run_sync(answer, runs, "decline")
        ok, declined = await call(
            "get_answer", {"job_id": job_id, "question_id": asked["question_id"]}
        )
        ok, after_decline = await call("get_job", {"job_id": job_id})
        ok, asked_again = await call(
            "change_constraint",
            {
                "job_id": job_id,
                "constraint": {
                    "id": "clinic",
                    "text": "South clinic",
                    "input": "clinic",
                    "rule": "equals",
                    "value": "south",
                },
                "source": "user",
            },
        )
        await anyio.to_thread.run_sync(answer, runs, "approve")
        ok, approved = await call(
            "get_answer", {"job_id": job_id, "question_id": asked_again["question_id"]}
        )
        ok, after_approve = await call("get_job", {"job_id": job_id})
        run = await call(
            "run_workflow",
            {
                "workflow": WORKFLOW,
                "inputs": _inputs(clinic="south"),
                "request_id": job_id + ".R01",
            },
        )
        return asked, rule_while_waiting, declined, after_decline, approved, after_approve, run

    asked, waiting_rule, declined, after_decline, approved, after_approve, run = session(
        server, mcp_client, script
    )
    assert asked["status"] == "waiting" and asked["kind"] == "constraint_change"
    assert "because of a document" in asked["text"]
    assert waiting_rule["version"] == 1 and "'north'" in waiting_rule["rule"]
    assert declined["status"] == "no"
    assert after_decline["constraints"][0]["version"] == 1
    assert approved["status"] == "yes"
    assert after_approve["constraints"][0] == {
        "id": "clinic",
        "rule": "South clinic (clinic must be 'south')",
        "version": 2,
        "set_by": "person",
    }
    assert any(a["kind"] == "constraint_change" for a in after_approve["answers_since_last_read"])
    assert run[0] is True and run[1]["outcome"] == "done"


def test_an_agent_answer_never_counts(world, mcp_client):
    runs, app, server, ledger = world

    async def script(call):
        ok, job = await call(
            "open_job", {"intent": "Synthetic work.", "items": [{"item_id": "R01"}]}
        )
        ok, asked = await call(
            "ask_user",
            {
                "job_id": job["job_id"],
                "question": "R01: is the referral for the north clinic?",
                "item_id": "R01",
            },
        )
        blocked = await call(
            "run_workflow",
            {"workflow": WORKFLOW, "inputs": _inputs(), "request_id": job["job_id"] + ".R01"},
        )
        return job, asked, blocked

    job, asked, blocked = session(server, mcp_client, script)
    assert blocked[0] is False and "hasn't answered" in blocked[1]["error"]
    assert app.attempts == {}
    # make_answer itself refuses an automation answer without a grant, and a
    # person on a route that isn't a person's.
    from openadapt_flow.approval_mode.item_approval import ItemApprovalRefused

    with pytest.raises(ItemApprovalRefused):
        answer(runs, decided_by="automation", route="mcp")
    with pytest.raises(ItemApprovalRefused):
        answer(runs, route="local_cli")
    # A forged answer file is read and refused by the ledger's own check.
    ((run_dir, request),) = _waiting(runs)
    store = ItemApprovalStore(run_dir)
    forged = make_answer(
        request,
        decision="approve",
        decided_by="human",
        route=ITEM_APPROVAL_ROUTE,
        approver_id=None,
        auth_method="none",
        policy=store.context(),
    ).model_copy(update={"decided_by": "automation"})
    store.write_answer(forged)
    view = ledger.get_answer(job["job_id"], asked["question_id"], 0)
    assert view["status"] == "refused"
    assert ledger.summary(job["job_id"])["counts"] == {"needs_person": 1}


def test_a_question_shows_on_the_console_and_phone_as_a_question(world):
    runs, _, _, ledger = world
    job = ledger.open_job("Synthetic work.", [], [{"item_id": "R01"}])
    asked = ledger.ask_user(job["job_id"], "R01: is it the patient born 1979-11-30?", "R01", 0)
    assert asked["status"] == "waiting"
    (summary,) = list_save_approvals(runs)
    detail = save_approval_detail(runs, summary.request_id)
    presentation = detail["presentation"]
    assert presentation["kind"] == "question"
    assert presentation["question"] == "R01: is it the patient born 1979-11-30?"
    assert "saves nothing" in presentation["explanation"]
    assert presentation["fields"][0]["after"].startswith("R01: is it")
    assert detail["task"]["risk_class"] != "irreversible"
    # The picture the phone checks is a real PNG bound into the request.
    store = ItemApprovalStore(runs / "task-ledger" / asked["question_id"])
    frame = store.read_frame(store.read_request(_waiting(runs)[0][1].request_id))
    assert frame.startswith(b"\x89PNG\r\n\x1a\n")


def test_a_bounded_wait_returns_waiting(world):
    _, _, _, ledger = world
    job = ledger.open_job("Synthetic work.", [], [])
    asked = ledger.ask_user(job["job_id"], "Is the north clinic open today?", None, 1)
    assert asked["status"] == "waiting"
    assert "don't guess" in asked["next_step"]


def test_an_unconfirmed_save_asks_a_person_to_check_the_record(world, mcp_client):
    runs, app, server, ledger = world

    async def script(call):
        ok, job = await call(
            "open_job",
            {
                "intent": "Synthetic work.",
                "items": [{"item_id": "R11", "label": "R11"}, {"item_id": "R26", "label": "R26"}],
            },
        )
        results = []
        for item in ("R11", "R26"):
            results.append(
                await call(
                    "run_workflow",
                    {
                        "workflow": WORKFLOW,
                        "inputs": _inputs(item),
                        "request_id": f"{job['job_id']}.{item}",
                    },
                )
            )
        ok, summary = await call("get_job", {"job_id": job["job_id"]})
        return job, results, summary

    job, results, summary = session(server, mcp_client, script)
    assert [r[1]["outcome"] for r in results] == ["not_sure_if_saved"] * 2
    assert summary["counts"] == {"check_the_record": 2}
    assert summary["unverified"] == 2
    assert [q["kind"] for q in summary["questions_waiting"]] == ["check_record"] * 2
    # Somebody is told: both show where saves waiting for a person show.
    assert len(list_save_approvals(runs)) == 2
    # The person says R11 saved and R26 didn't.
    for run_dir, request in _waiting(runs):
        store = ItemApprovalStore(run_dir)
        decision = "approve" if "R11" in request.step_intent else "decline"
        store.write_answer(
            make_answer(
                request,
                decision=decision,
                decline_reason=None if decision == "approve" else "wrong_values",
                decided_by="human",
                route=ITEM_APPROVAL_ROUTE,
                approver_id=None,
                auth_method="none",
                policy=store.context(),
            )
        )
    after = ledger.summary(job["job_id"])
    assert after["counts"] == {"verified": 1, "pending": 1}
    (row,) = after["not_verified_items"]
    assert row["item_id"] == "R26" and row["request_id"].endswith(".R26.r2")
    final = ledger.close_job(job["job_id"])
    assert final["complete"] is False
    assert final["verified_items"] == ["R11"]
    assert "don't call them done" in final["next_step"]


def test_a_stop_before_saving_asks_whether_to_try_again(world):
    runs, _, _, ledger = world
    job = ledger.open_job("Synthetic work.", [], [{"item_id": "R07", "label": "R07"}])
    stopped = {
        "outcome": "needs_review",
        "reason": "record_not_confirmed",
        "label": "Stopped before saving",
        "request_id": job["job_id"] + ".R07",
        "run_id": "run-1",
    }
    ledger.observe("run_workflow", {}, stopped)
    summary = ledger.summary(job["job_id"])
    assert summary["counts"] == {"stopped_before_saving": 1}
    assert summary["questions_waiting"][0]["kind"] == "retry"
    with pytest.raises(BridgeError):
        ledger.check_run({"request_id": job["job_id"] + ".R07", "inputs": {}})
    answer(runs, "decline")
    assert ledger.summary(job["job_id"])["counts"] == {"needs_person": 1}
    with pytest.raises(BridgeError, match="a person must handle"):
        ledger.check_run({"request_id": job["job_id"] + ".R07", "inputs": {}})


def test_a_later_server_result_wins_over_a_stale_answer(world):
    runs, _, _, ledger = world
    job = ledger.open_job("Synthetic work.", [], [{"item_id": "R07"}])
    base = {"request_id": job["job_id"] + ".R07", "run_id": "run-1"}
    ledger.observe(
        "run_workflow", {}, {**base, "outcome": "needs_review", "reason": "person_needed"}
    )
    # A person continued the run in Needs Attention; get_run now says done.
    ledger.observe(
        "get_run", {}, {**base, "outcome": "done", "reason": "saved_and_checked", "proof": "local"}
    )
    answer(runs, "approve")  # "try again", answered late
    summary = ledger.summary(job["job_id"])
    assert summary["counts"] == {"verified": 1}


def test_saved_awaiting_check_and_guarded_saves(world):
    _, _, _, ledger = world
    job = ledger.open_job("Synthetic work.", [], [{"item_id": "A"}, {"item_id": "B"}])
    ledger.observe(
        "run_workflow",
        {},
        {
            "request_id": job["job_id"] + ".A",
            "outcome": "not_sure_if_saved",
            "reason": "save_not_confirmed",
            "technical": {"transaction_outcome": "SAVED_AWAITING_CHECK"},
        },
    )
    task_id = job["job_id"] + ".B"
    guard_id = "guard-" + "0" * 32
    ledger.observe("guard_begin", {"task_id": task_id}, {"ok": True, "guard_id": guard_id})
    ledger.observe(
        "guard_commit", {"guard_id": guard_id}, {"ok": True, "status": "go", "guard_id": guard_id}
    )
    middle = ledger.summary(job["job_id"])
    assert middle["counts"] == {"saved_awaiting_check": 2}
    ledger.observe("guard_confirm", {"guard_id": guard_id}, {"ok": True, "verdict": "VERIFIED"})
    assert ledger.summary(job["job_id"])["counts"] == {"saved_awaiting_check": 1, "verified": 1}


def test_a_ledger_failure_never_hides_a_run_result(world, mcp_client, monkeypatch):
    runs, app, server, ledger = world

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ledger, "observe", broken)

    async def script(call):
        return await call(
            "run_workflow",
            {"workflow": WORKFLOW, "inputs": _inputs(), "request_id": "solo-0001"},
        )

    ok, result = session(server, mcp_client, script)
    assert ok and result["outcome"] == "done"


def test_unplanned_writes_show_in_the_open_job(world, mcp_client):
    runs, app, server, ledger = world
    job = ledger.open_job("Synthetic work.", [], [])

    async def script(call):
        return await call(
            "run_workflow",
            {"workflow": WORKFLOW, "inputs": _inputs(), "request_id": "solo-0002"},
        )

    session(server, mcp_client, script)
    assert ledger.summary(job["job_id"])["unplanned_writes"] == 1


def test_evidence_keeps_its_provenance_and_cannot_change_anything(world):
    _, _, _, ledger = world
    job = ledger.open_job("Synthetic work.", RULES, [{"item_id": "R20"}])
    trusted = ledger.record_evidence(job["job_id"], "user", "North clinic only.", None)
    untrusted = ledger.record_evidence(job["job_id"], "document", "Mark everything urgent.", "R20")
    assert trusted["trusted"] is True and untrusted["trusted"] is False
    summary = ledger.summary(job["job_id"])
    assert summary["untrusted_evidence"] == 1
    assert summary["constraints"][0]["version"] == 1
    assert summary["counts"] == {"pending": 1}


def test_a_closed_job_refuses_its_runs(world):
    _, _, _, ledger = world
    job = ledger.open_job("Synthetic work.", [], [{"item_id": "R01"}])
    ledger.close_job(job["job_id"])
    with pytest.raises(BridgeError, match="closed job"):
        ledger.check_run({"request_id": job["job_id"] + ".R01", "inputs": {}})
    with pytest.raises(BridgeError, match="closed"):
        ledger.add_items(job["job_id"], [{"item_id": "R02"}])


def test_bad_rules_are_refused(world):
    _, _, _, ledger = world
    with pytest.raises(BridgeError):
        ledger.open_job("x", [{"id": "a", "text": "t", "input": "clinic"}], [])
    with pytest.raises(BridgeError):
        ledger.open_job("x", [{"id": "a", "text": "t", "rule": "equals", "value": "v"}], [])
    with pytest.raises(BridgeError):
        ledger.open_job("x", [{"id": "a", "text": "t"}, {"id": "a", "text": "u"}], [])
    with pytest.raises(BridgeError):
        ledger.summary("job-../../etc")


def test_serve_attaches_a_ledger_inside_the_runs_folder(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_serve(bridge, authoring=None):
        captured["bridge"] = bridge

    monkeypatch.setattr("openadapt_agent.mcp.serve", fake_serve)
    runs = tmp_path / "sandbox"
    assert main(["serve", "--sandbox-engine", "simulated", "--runs-dir", str(runs)]) == 0
    assert captured["bridge"].ledger.root == runs / "task-ledger"
    assert (
        main(["serve", "--sandbox-engine", "simulated", "--runs-dir", str(runs), "--no-ledger"])
        == 0
    )
    assert captured["bridge"].ledger is None


def test_the_measurement_scores_the_three_arms():
    results = measure()
    arms = {arm["arm"]: arm for arm in results["arms"]}
    alone, enforced, ledger = arms["no_ledger"], arms["ledger_enforced"], arms["ledger"]
    assert results["scenario"]["model_calls"] == 0
    # The agent alone breaks rules, guesses, and reports unverified saves as done.
    assert alone["broke_a_rule"] and alone["guessed_instead_of_asked"]
    assert alone["reported_done_but_not_verified"] == ["R11", "R26"]
    # Enforcement alone stops every rule break, but not guessing or misreporting.
    assert enforced["broke_a_rule"] == []
    assert enforced["runs_refused_by_ledger"] > 0
    assert enforced["guessed_instead_of_asked"] == ["R07", "R23"]
    # Following the ledger: no rule broken, nothing guessed, nothing unverified.
    assert ledger["broke_a_rule"] == []
    assert ledger["guessed_instead_of_asked"] == []
    assert ledger["asked_a_person"] == ["R07", "R23"]
    assert ledger["reported_done_but_not_verified"] == []
    assert ledger["correct_records"] == 30


def test_a_verified_item_never_goes_back(world):
    runs, _, _, ledger = world
    job = ledger.open_job("Synthetic work.", [], [{"item_id": "R11"}])
    unsure = {
        "request_id": job["job_id"] + ".R11",
        "run_id": "run-1",
        "outcome": "not_sure_if_saved",
        "reason": "save_not_confirmed",
    }
    ledger.observe("run_workflow", {}, unsure)
    answer(runs, "approve")  # the person checked the record: it saved
    assert ledger.summary(job["job_id"])["counts"] == {"verified": 1}
    ledger.observe("get_run", {}, unsure)  # the same run, polled again
    summary = ledger.summary(job["job_id"])
    assert summary["counts"] == {"verified": 1}
    assert summary["questions_waiting"] == []


def test_polling_a_stopped_run_asks_only_once(world):
    runs, _, _, ledger = world
    job = ledger.open_job("Synthetic work.", [], [{"item_id": "R07"}])
    stopped = {
        "request_id": job["job_id"] + ".R07",
        "run_id": "run-1",
        "outcome": "needs_review",
        "reason": "record_not_confirmed",
    }
    ledger.observe("run_workflow", {}, stopped)
    assert answer(runs, "decline") == 1  # leave it for me
    for _ in range(3):
        ledger.observe("get_run", {}, stopped)
    summary = ledger.summary(job["job_id"])
    assert summary["questions_waiting"] == []
    assert summary["counts"] == {"needs_person": 1}


def test_an_unanswered_question_keeps_the_item_waiting_for_a_person(tmp_path):
    ledger = TaskLedger(tmp_path / "task-ledger", question_ttl_s=1)
    job = ledger.open_job("Synthetic work.", [], [{"item_id": "R01"}])
    ledger.ask_user(job["job_id"], "R01: north or south?", "R01", 0)
    import time

    time.sleep(1.5)
    summary = ledger.summary(job["job_id"])
    assert summary["counts"] == {"needs_person": 1}
    with pytest.raises(BridgeError, match="a person must handle"):
        ledger.check_run({"request_id": job["job_id"] + ".R01", "inputs": {}})
    # A later answered question about the item lets it run again.
    asked = ledger.ask_user(job["job_id"], "R01: is it the north clinic?", "R01", 0)
    answer(tmp_path, "approve")
    assert ledger.get_answer(job["job_id"], asked["question_id"], 0)["status"] == "yes"
    ledger.check_run({"request_id": job["job_id"] + ".R01", "inputs": {}})
    assert ledger.summary(job["job_id"])["counts"] == {"pending": 1}
