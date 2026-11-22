"""wrap-dispatch: program first, the default hold, the opt-in fallback, hold verbs.

The task setup is the same modelled SkillCache as tests/test_wrap_fallback.py
(real cache, hold and report checks; the MCP reply and run 1 modelled).
"""

# ruff: noqa: F811 - pytest fixtures are imported by name
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.compile_agent import cli as compile_cli
from openadapt_flow.wrap import WrappedTask
from openadapt_flow.wrap import dispatch_cli as dc
from tests.test_wrap_fallback import (  # noqa: F401 - fixtures
    PARAMS,
    PROMPT,
    Agent,
    build_model,
    install_reply,
    model,
    run1,
)

RUN_KEYS = {
    "path",
    "status",
    "verified",
    "model_calls",
    "cost_usd",
    "usage",
    "usage_known",
    "seconds",
    "wire_checked",
    "receipt",
    "receipt_outcome",
}


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="openadapt-flow")
    dc.register(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(["wrap-dispatch", *argv])


@pytest.fixture
def task(model, monkeypatch, tmp_path) -> SimpleNamespace:
    setup = SimpleNamespace(
        cache=model.cache, instruction=PROMPT, parameters=("value",)
    )
    monkeypatch.setattr(dc, "_setup", lambda *_a, **_k: setup)
    runs = tmp_path / "runs"
    return SimpleNamespace(setup=setup, runs=str(runs), model=model)


def run_args(task: SimpleNamespace, request: str, value: str, *extra: str):
    return parse(
        "--task",
        "task.json",
        "--request-id",
        request,
        "--param",
        f"value={value}",
        "--runs-dir",
        task.runs,
        *extra,
    )


def test_the_verb_is_registered_in_openadapt_flow() -> None:
    from openadapt_flow.__main__ import build_parser

    args = build_parser().parse_args(["wrap-dispatch", "status", "task.json"])
    assert args.func is dc.cmd_wrap_dispatch and args.dispatch_verb == "status"


def test_no_admitted_program_runs_nothing(task, monkeypatch) -> None:
    monkeypatch.setattr(task.setup.cache, "_server", lambda *_: None)
    result = dc.run(run_args(task, "r1", "one"))
    assert result["status"] == "NO_ADMITTED_PROGRAM" and result["path"] == "held"
    assert result["outcome"] == "ESCALATED" and result["model_calls"] == 0
    assert task.model.sent == [] and set(result) >= RUN_KEYS


def test_default_dispatch_is_the_wrap_hold(task, monkeypatch, tmp_path) -> None:
    install_reply(task.model, monkeypatch)
    first = dc.run(run_args(task, "r1", "one"))
    assert first["status"] == "HALTED" and first["path"] == "program"
    assert first["outcome"] == "ESCALATED" and first["fallback"] is None
    second = dc.run(run_args(task, "r2", "two"))
    assert second["status"] == "CACHE_HALTED" and second["path"] == "held"
    assert task.model.sent == [{"value": "one"}]

    # The same requests through openadapt wrap's WrappedTask on a fresh copy.
    (tmp_path / "plain").mkdir()
    other = build_model(tmp_path / "plain", monkeypatch)
    install_reply(other, monkeypatch)
    wrapped = WrappedTask(
        cache=other.cache,
        agent=dc._NoAgent(),
        instruction=PROMPT,
        parameters=("value",),
        runs_dir=tmp_path / "plain-runs",
    )
    plain = [
        wrapped.call("r1", {"value": "one"}),
        wrapped.call("r2", {"value": "two"}),
    ]
    for ours, theirs in zip((first, second), plain):
        mine = {k: v for k, v in ours.items() if k not in ("outcome", "fallback")}
        assert set(mine) == set(theirs)
        # ``path`` keeps wrap's field name; its value is the dispatch route.
        for key in set(theirs) - {"run_id", "seconds", "halt_reason", "path"}:
            assert mine[key] == theirs[key], key


def test_fallback_needs_its_own_cap(task) -> None:
    with pytest.raises(SystemExit):
        dc.run(run_args(task, "r1", "one", "--fallback", "agent"))
    with pytest.raises(SystemExit):
        dc.run(run_args(task, "r1", "one", "--fallback-max-cost-usd", "1"))
    with pytest.raises(SystemExit):
        dc.run(
            run_args(
                task, "r1", "one", "--fallback", "agent", "--fallback-max-cost-usd", "0"
            )
        )


def test_opt_in_fallback_result(task, monkeypatch, run1, tmp_path) -> None:
    install_reply(task.model, monkeypatch)
    out = tmp_path / "result.json"
    args = run_args(
        task,
        "r1",
        "one",
        "--fallback",
        "agent",
        "--fallback-max-cost-usd",
        "0.25",
        "--result",
        str(out),
    )
    approvals: list = []
    result = dc.run(args, agent=Agent(), gate=lambda r: approvals.append(r) or True)
    assert result["path"] == "agent_fallback" and result["outcome"] == "VERIFIED"
    assert result["verified"] and result["model_calls"] == 0 and len(approvals) == 1
    assert result["fallback"]["request_id"] == dc.child_request_id("r1")
    assert result["fallback"]["parent_request_id"] == "r1"
    assert result["fallback"]["admitted"] is False
    assert result["fallback"]["repair"]["opened"] is True
    assert set(result) >= RUN_KEYS
    # The CLI prints and writes the same JSON.
    monkeypatch.setattr(dc, "run", lambda _args: result)
    assert dc.cmd_wrap_dispatch(args) == 0
    assert json.loads(out.read_text())["path"] == "agent_fallback"


def test_status_evict_and_release(task, monkeypatch, run1, capsys) -> None:
    install_reply(task.model, monkeypatch)
    dc.run(run_args(task, "r1", "one"))
    shown = dc.status(parse("status", "task.json"))
    assert shown["held"] and shown["hold"]["phase"] == "halted"
    assert shown["fallback_eligible"] and shown["fallback"]["runs"] == []

    # Release needs a person at a terminal, and their typed confirmation.
    monkeypatch.setattr(compile_cli, "_interactive", lambda: False)
    with pytest.raises(SystemExit):
        dc.release(parse("release", "task.json", "--person", "dana"))
    monkeypatch.setattr(compile_cli, "_interactive", lambda: True)
    args = parse("release", "task.json", "--person", "dana")
    assert dc.release(args, ask=lambda _: "no")["reason"] == "NOT_CONFIRMED"
    done = dc.release(args, ask=lambda _: "release")
    assert done["released"] and Path(done["archived"]).is_file()
    assert not task.model.stop.exists()
    by = json.loads(Path(done["archived"]).with_suffix(".by.json").read_text())
    assert by["person"] == "dana" and by["phase"] == "halted"

    kept = dc.evict(parse("evict", "task.json"))
    assert kept["evicted"] and Path(kept["kept_at"]).is_dir()


def test_release_of_a_possible_write_needs_reconciling(task, monkeypatch) -> None:
    async def hit(*_):
        raise RuntimeError("lost")

    from openadapt_flow.integrations.agents import skills

    monkeypatch.setattr(skills, "_call_skill", hit)
    result = dc.run(run_args(task, "r1", "one"))
    assert result["status"] == "RECONCILIATION_REQUIRED"
    assert result["outcome"] == "ESCALATED"
    monkeypatch.setattr(compile_cli, "_interactive", lambda: True)
    args = parse("release", "task.json", "--person", "dana")
    assert dc.release(args, ask=lambda _: "release")["reason"] == "RECONCILE_FIRST"
    args = parse("release", "task.json", "--person", "dana", "--after-reconciling")
    assert dc.release(args, ask=lambda _: "release")["released"]


@pytest.mark.parametrize(
    ("status", "verified", "expected"),
    [
        ("VERIFIED", True, "VERIFIED"),
        ("CACHE_HALTED", False, "ESCALATED"),
        ("GATE_REFUSED", False, "ESCALATED"),
        ("RECORD_CHECK_FAILED", False, "CAUGHT_ERROR"),
        ("COST_CAP_EXCEEDED", False, "CAUGHT_ERROR"),
        ("SOMETHING_ELSE", False, "FAILED"),
    ],
)
def test_outcomes(status: str, verified: bool, expected: str) -> None:
    assert dc.outcome({"status": status, "verified": verified}) == expected


# -- review fixes: compiled-only dispatch, stored envelope, release ------------

FALLBACK = ("--fallback", "agent", "--fallback-max-cost-usd", "0.25")


@pytest.mark.parametrize("extra", [(), FALLBACK])
def test_a_program_evicted_after_the_check_is_a_miss(
    task,
    monkeypatch,
    run1,
    extra,
) -> None:
    """server() still says yes (the old precheck); at dispatch there is no
    admitted program. Nothing runs: no first run (fixture fails on one), no
    program call, no agent."""
    monkeypatch.setattr(task.setup.cache, "server", lambda *_: Path("server.py"))
    monkeypatch.setattr(task.setup.cache, "_server", lambda *_: None)
    agent = Agent()
    result = dc.run(run_args(task, "r1", "one", *extra), agent=agent)
    assert result["status"] == "NO_ADMITTED_PROGRAM" and result["path"] == "held"
    assert agent.calls == 0 and task.model.sent == []


def test_no_admitted_program_is_stored_with_the_request(task, monkeypatch) -> None:
    monkeypatch.setattr(task.setup.cache, "_server", lambda *_: None)
    first = dc.run(run_args(task, "r1", "one"))
    assert first["status"] == "NO_ADMITTED_PROGRAM" and first["replayed"] is False
    # The program is admitted now; the same request still reports its outcome.
    monkeypatch.undo()
    monkeypatch.setattr(dc, "_setup", lambda *_a, **_k: task.setup)
    install_reply(task.model, monkeypatch)
    again = dc.run(run_args(task, "r1", "one"))
    assert again["status"] == "NO_ADMITTED_PROGRAM" and again["replayed"] is True
    assert task.model.sent == []


def test_a_retry_replays_the_whole_dispatch_envelope(
    task,
    monkeypatch,
    run1,
) -> None:
    install_reply(task.model, monkeypatch)
    agent = Agent()
    first = dc.run(
        run_args(task, "r1", "one", *FALLBACK), agent=agent, gate=lambda r: True
    )
    assert first["verified"] and first["fallback"]["repair"]["opened"]
    again = dc.run(
        run_args(task, "r1", "one", *FALLBACK),
        agent=agent,
        gate=lambda r: pytest.fail("asked again"),
    )
    assert again["replayed"] is True and agent.calls == 1
    assert again["fallback"] == first["fallback"]
    assert again["path"] == "agent_fallback" and again["outcome"] == "VERIFIED"
    # The stored run record is exactly the reply, so the strict readback
    # does not fail on a reply/record mismatch.
    from openadapt_flow.request_binding import BindingError

    wrapped = WrappedTask(
        cache=task.setup.cache,
        agent=dc._NoAgent(),
        instruction=PROMPT,
        parameters=("value",),
        runs_dir=task.runs,
    )
    with pytest.raises(BindingError) as raised:
        wrapped.verify_reply("r1", {"value": "one"}, first)
    # Past the reply/record and identity checks; a fallback has no receipt.
    assert str(raised.value) == "receipt_unbound"


@pytest.mark.parametrize("parent", ["p" * 128, "tenant/a:b c", ".hidden"])
def test_any_valid_parent_id_gets_a_fallback(
    task,
    monkeypatch,
    run1,
    parent,
) -> None:
    install_reply(task.model, monkeypatch)
    result = dc.run(
        run_args(task, parent, "one", *FALLBACK), agent=Agent(), gate=lambda r: True
    )
    child = result["fallback"]["request_id"]
    assert result["verified"] and child == dc.child_request_id(parent)
    assert len(child) <= 128 and child.replace("-", "").isalnum()
    assert result["fallback"]["parent_request_id"] == parent


def test_release_of_a_reconciliation_reply_needs_the_acknowledgement(
    task, monkeypatch
) -> None:
    """The hold's phase is ``halted``, but its reply says a write may exist."""
    install_reply(task.model, monkeypatch, status="RECONCILIATION_REQUIRED")
    assert dc.run(run_args(task, "r1", "one"))["status"] == "RECONCILIATION_REQUIRED"
    assert json.loads(task.model.stop.read_text())["phase"] == "halted"
    monkeypatch.setattr(compile_cli, "_interactive", lambda: True)
    args = parse("release", "task.json", "--person", "dana")
    assert dc.release(args, ask=lambda _: "release")["reason"] == "RECONCILE_FIRST"
    assert task.model.stop.exists()
    args = parse("release", "task.json", "--person", "dana", "--after-reconciling")
    done = dc.release(args, ask=lambda _: "release")
    assert done["released"]
    by = json.loads(Path(done["archived"]).with_suffix(".by.json").read_text())
    assert by["after_reconciling"] is True


def test_release_refuses_while_the_fallback_runs(
    task,
    monkeypatch,
    run1,
) -> None:
    install_reply(task.model, monkeypatch)
    monkeypatch.setattr(compile_cli, "_interactive", lambda: True)
    tried: list = []

    def gate(request) -> bool:
        args = parse("release", "task.json", "--person", "dana", "--after-reconciling")
        tried.append(dc.release(args, ask=lambda _: "release"))
        return True

    result = dc.run(run_args(task, "r1", "one", *FALLBACK), agent=Agent(), gate=gate)
    assert tried[0]["released"] is False and tried[0]["reason"] == "FALLBACK_RUNNING"
    assert result["verified"]
    assert task.model.stop.exists()  # the hold was not released either


def test_release_after_a_crashed_fallback(task, monkeypatch, run1) -> None:
    install_reply(task.model, monkeypatch)
    crashed = dc.run(
        run_args(task, "r1", "one", *FALLBACK),
        agent=Agent(raises=True),
        gate=lambda r: True,
    )
    assert crashed["status"] == "RECONCILIATION_REQUIRED"
    shown = dc.status(parse("status", "task.json"))
    assert shown["fallback"]["in_flight_owner"] == "ended"
    monkeypatch.setattr(compile_cli, "_interactive", lambda: True)
    args = parse("release", "task.json", "--person", "dana")
    assert dc.release(args, ask=lambda _: "release")["reason"] == "RECONCILE_FIRST"
    args = parse("release", "task.json", "--person", "dana", "--after-reconciling")
    done = dc.release(args, ask=lambda _: "release")
    assert done["released"] and done["fallback_cleared"]
    assert done["fallback_request_id"] == dc.child_request_id("r1")


@pytest.mark.parametrize(
    "reply",
    [
        {"fail_read_after": True},  # the read after the halt failed
        {"write": {"id": "1", "kind": "synthetic", "value": "one"}},  # it wrote
    ],
)
def test_release_of_an_unproved_halt_needs_the_acknowledgement(
    task, monkeypatch, reply
) -> None:
    """The report says HALTED_BEFORE_EFFECT, but the record check does not
    prove nothing was written: the same rule as the fallback's."""
    install_reply(task.model, monkeypatch, **reply)
    assert dc.run(run_args(task, "r1", "one"))["status"] == "HALTED"
    shown = dc.status(parse("status", "task.json"))
    assert shown["fallback_eligible"] is False
    monkeypatch.setattr(compile_cli, "_interactive", lambda: True)
    args = parse("release", "task.json", "--person", "dana")
    assert dc.release(args, ask=lambda _: "release")["reason"] == "RECONCILE_FIRST"
    assert task.model.stop.exists()


# -- the item-approval gate through wrap-dispatch (no injected gate) -------------


def _approve_with_cli(approvals: Path, key_path: Path, root: str) -> tuple:
    """A person answers with openadapt-flow item-approval approve and their
    approver key, from a thread, while the fallback waits at its gate."""
    import threading
    import time

    from openadapt_flow.__main__ import main

    done: list = []

    def answer() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not done:
            for request in approvals.glob("*/item-approval/request-*.json"):
                data = json.loads(request.read_text())
                from openadapt_flow.approval_mode.item_approval import (
                    ItemApprovalRequest,
                )

                asked = ItemApprovalRequest.model_validate(data)
                done.append(
                    main(
                        [
                            "item-approval",
                            "approve",
                            str(request.parent.parent),
                            "--request-id",
                            asked.request_id,
                            "--request-sha256",
                            asked.digest(),
                            "--decided-by",
                            "human",
                            "--route",
                            "desktop_run_it",
                            "--unauthenticated",
                            "--approver-key",
                            str(key_path),
                            "--approver-root",
                            root,
                        ]
                    )
                )
                return
            time.sleep(0.05)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread, done


GATE = ("--fallback-gate", "item-approval", "--fallback-approval-wait-s", "15")


def test_wrap_dispatch_item_approval_gate_saves_after_a_signed_answer(
    task, monkeypatch, run1, approver, tmp_path
) -> None:
    import os

    from openadapt_flow.approval_mode.approver_roster import ROOT_ENV

    install_reply(task.model, monkeypatch)
    approvals = tmp_path / "approvals"
    key_path, _signer = approver
    thread, done = _approve_with_cli(approvals, key_path, os.environ[ROOT_ENV])
    agent = Agent()
    result = dc.run(
        run_args(
            task,
            "r1",
            "one",
            *FALLBACK,
            *GATE,
            "--fallback-approvals-dir",
            str(approvals),
        ),
        agent=agent,
    )
    thread.join(timeout=5)
    assert done == [0]
    assert result["verified"] and result["outcome"] == "VERIFIED"
    [answer] = result["fallback"]["gate_answers"]
    assert answer["decision"] == "approved" and answer["signer"] == "front-desk"
    assert task.model.rows == [{"id": "f1", "kind": "synthetic", "value": "one"}]


def test_wrap_dispatch_item_approval_gate_with_a_config_pin(
    task, monkeypatch, run1, approver, tmp_path
) -> None:
    import os

    from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
    from tests._approver_keys import approver_config

    install_reply(task.model, monkeypatch)
    root = os.environ[ROOT_ENV]
    monkeypatch.delenv(ROOT_ENV)
    config = approver_config(tmp_path / "deployment.yaml", root)
    approvals = tmp_path / "approvals"
    key_path, _signer = approver
    thread, done = _approve_with_cli(approvals, key_path, root)
    result = dc.run(
        run_args(
            task,
            "r1",
            "one",
            *FALLBACK,
            *GATE,
            "--fallback-approvals-dir",
            str(approvals),
            "--fallback-approvers-config",
            str(config),
        ),
        agent=Agent(),
    )
    thread.join(timeout=5)
    assert done == [0] and result["verified"]


def test_wrap_dispatch_item_approval_gate_without_a_pin_never_builds_the_agent(
    task, monkeypatch, run1, approver
) -> None:
    from openadapt_flow.approval_mode.approver_roster import ROOT_ENV

    install_reply(task.model, monkeypatch)
    monkeypatch.delenv(ROOT_ENV)
    monkeypatch.setattr(
        dc, "_fallback_agent", lambda *_: pytest.fail("agent built with no pin")
    )
    result = dc.run(run_args(task, "r1", "one", *FALLBACK, *GATE))
    assert result["status"] == "FALLBACK_GATE_UNATTENDED"
    assert result["outcome"] == "ESCALATED" and task.model.rows == []


def test_the_gate_flags_need_the_fallback(task) -> None:
    with pytest.raises(SystemExit):
        dc.run(run_args(task, "r1", "one", *GATE))
    with pytest.raises(SystemExit):
        dc.run(
            run_args(
                task,
                "r1",
                "one",
                *FALLBACK,
                "--fallback-gate",
                "item-approval",
                "--fallback-approval-wait-s",
                "200",
            )
        )


# -- a fallback that raises still names itself ----------------------------------


def test_a_crashed_fallback_is_stored_as_a_possible_write(
    task, monkeypatch, run1
) -> None:
    """From the reviewer's probe: the agent raises after the fallback started.
    The stored envelope says agent_fallback and RECONCILIATION_REQUIRED, with
    the child request and the error class, on the first call and on retry;
    result.json is written."""
    from openadapt_flow.wrap.fallback import fallback_root

    install_reply(task.model, monkeypatch)
    agent = Agent(raises=True)
    args = run_args(task, "r1", "one", *FALLBACK)
    crashed = dc.run(args, agent=agent, gate=lambda r: True)
    assert crashed["path"] == "agent_fallback"
    assert crashed["status"] == "RECONCILIATION_REQUIRED"
    assert crashed["outcome"] == "ESCALATED" and crashed["verified"] is False
    child = dc.child_request_id("r1")
    assert crashed["fallback"]["request_id"] == child
    assert crashed["fallback"]["error"] == "RuntimeError"
    assert crashed["fallback"]["parent_request_id"] == "r1"
    root = fallback_root(task.setup.cache, PROMPT, {"value": ""})
    saved = json.loads((root / child / "result.json").read_text())
    assert saved["path"] == "agent_fallback"
    assert saved["status"] == "RECONCILIATION_REQUIRED" and saved["verified"] is False
    assert saved["replay"]["fallback"]["error"] == "RuntimeError"
    again = dc.run(args, agent=agent, gate=lambda r: pytest.fail("asked again"))
    assert again["replayed"] is True and agent.calls == 1
    assert {k: again[k] for k in ("path", "status", "outcome", "fallback")} == {
        k: crashed[k] for k in ("path", "status", "outcome", "fallback")
    }


def test_a_repair_desk_failure_keeps_the_verified_save(task, monkeypatch, run1) -> None:
    install_reply(task.model, monkeypatch)

    def broken(*_a, **_k):
        raise OSError("repair desk unavailable")

    monkeypatch.setattr(task.setup.cache, "open_repair", broken)
    result = dc.run(
        run_args(task, "r1", "one", *FALLBACK), agent=Agent(), gate=lambda r: True
    )
    assert result["verified"] and result["path"] == "agent_fallback"
    assert result["fallback"]["repair"] == {
        "opened": False,
        "reason": "repair_failed:OSError",
    }


@pytest.mark.parametrize(
    "extra",
    [
        ("--fallback-approver-root", "a" * 64),
        ("--fallback-approvers-config", "deployment.yaml"),
        ("--fallback-approval-wait-s", "10"),
        (*FALLBACK, "--fallback-approver-root", "a" * 64),
        (*FALLBACK, "--fallback-approvals-dir", "approvals"),
    ],
)
def test_gate_options_without_the_item_approval_gate_are_refused(task, extra) -> None:
    with pytest.raises(SystemExit):
        dc.run(run_args(task, "r1", "one", *extra))


@pytest.mark.parametrize("bad", ["a/b", ".hidden", "x" * 129, ""])
def test_a_bad_fallback_request_id_is_a_clean_error(task, bad) -> None:
    with pytest.raises(SystemExit, match="fallback-request-id"):
        dc.run(run_args(task, "r1", "one", *FALLBACK, "--fallback-request-id", bad))


@pytest.mark.parametrize(
    "interrupt", [KeyboardInterrupt, lambda: SystemExit("agent exited")]
)
@pytest.mark.parametrize("where", ["gate", "agent"])
def test_an_interrupted_fallback_is_stored_before_it_reraises(
    task, monkeypatch, run1, interrupt, where
) -> None:
    """From the reviewer's probe: Ctrl-C at the gate, or a SystemExit in the
    agent. The request is stored as a finished agent_fallback that may have
    written, result.json says so, the marker stays, and a retry replays it."""
    from openadapt_flow.wrap.fallback import fallback_root

    install_reply(task.model, monkeypatch)
    agent = Agent()
    args = run_args(task, "r1", "one", *FALLBACK)

    def gate(_request):
        raise interrupt()

    if where == "agent":
        run1.after = lambda made: (_ for _ in ()).throw(interrupt())
        gate = lambda _request: True  # noqa: E731
    with pytest.raises((KeyboardInterrupt, SystemExit)):
        dc.run(args, agent=agent, gate=gate)
    root = fallback_root(task.setup.cache, PROMPT, {"value": ""})
    child = dc.child_request_id("r1")
    saved = json.loads((root / child / "result.json").read_text())
    assert saved["path"] == "agent_fallback"
    assert saved["status"] == "RECONCILIATION_REQUIRED"
    assert json.loads((root / "in-flight.json").read_text())["state"] == "ended"
    again = dc.run(args, agent=agent, gate=lambda r: pytest.fail("asked"))
    assert again["replayed"] is True and agent.calls == 1
    assert again["path"] == "agent_fallback"
    assert again["status"] == "RECONCILIATION_REQUIRED"
    assert again["outcome"] == "ESCALATED"
    assert again["fallback"]["request_id"] == child
    assert again["fallback"]["error"] in ("KeyboardInterrupt", "SystemExit")


def test_a_reused_request_id_reports_nothing_ran(task, monkeypatch) -> None:
    install_reply(task.model, monkeypatch)
    dc.run(run_args(task, "r1", "one"))
    conflict = dc.run(run_args(task, "r1", "two"))
    assert conflict["status"] == "REQUEST_ID_CONFLICT"
    assert conflict["path"] == "held" and len(task.model.sent) == 1


def test_a_repair_file_write_failure_keeps_the_verified_save(
    task, monkeypatch, run1
) -> None:
    from openadapt_flow.wrap import fallback as fb

    install_reply(task.model, monkeypatch)
    real = fb._write

    def write(path, data, **kw):
        if path.name == "repair.json":
            raise OSError("disk full")
        return real(path, data, **kw)

    monkeypatch.setattr(fb, "_write", write)
    result = dc.run(
        run_args(task, "r1", "one", *FALLBACK), agent=Agent(), gate=lambda r: True
    )
    assert result["verified"]
    assert result["fallback"]["repair"]["reason"] == "repair_failed:OSError"
