"""wrap-dispatch: program first, the default hold, the opt-in fallback, hold verbs.

The task setup is the same modelled SkillCache as tests/test_wrap_fallback.py
(real cache, hold and report checks; the MCP reply and run 1 modelled).
"""

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
def task(model, monkeypatch, tmp_path) -> SimpleNamespace:  # noqa: F811
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
    monkeypatch.setattr(task.setup.cache, "server", lambda *_: None)
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


def test_opt_in_fallback_result(task, monkeypatch, run1, tmp_path) -> None:  # noqa: F811
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
    assert result["fallback"]["request_id"] == "r1.fallback"
    assert result["fallback"]["admitted"] is False
    assert result["fallback"]["repair"]["opened"] is True
    assert set(result) >= RUN_KEYS
    # The CLI prints and writes the same JSON.
    monkeypatch.setattr(dc, "run", lambda _args: result)
    assert dc.cmd_wrap_dispatch(args) == 0
    assert json.loads(out.read_text())["path"] == "agent_fallback"


def test_status_evict_and_release(task, monkeypatch, run1, capsys) -> None:  # noqa: F811
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
