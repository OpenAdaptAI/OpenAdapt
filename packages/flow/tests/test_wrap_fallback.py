"""FallbackPolicy: the real cache, hold and report checks; MCP and run 1 modelled.

The compiled program's reply is the MCP boundary (as in
test_skill_cache_runtime.py). The fallback's run 1 is a small stand-in for
FirstRunCompiler that calls the real gate wrapper before its one save. The
browser path is in tests/e2e/test_wrap_fallback_e2e.py.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from openadapt_flow import compile_agent
from openadapt_flow.integrations.agents import skills
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    HaltObservation,
    RunReport,
    Step,
    StepResult,
    Workflow,
)
from openadapt_flow.wrap import fallback as fb
from openadapt_flow.wrap.fallback import FallbackPolicy, TerminalGate

PROMPT = "Create the synthetic record with {value}"
PARAMS = {"value": "one"}


def make_cache(tmp_path: Path) -> skills.SkillCache:
    verify = tmp_path / "verify.yaml"
    verify.write_text(
        "verifier:\n  kind: rest\n  base_url: http://127.0.0.1:1\n"
        "  records_path: /records\n  records_key: records\n"
    )
    return skills.SkillCache(
        url="http://127.0.0.1:1",
        verify=verify,
        expect={"kind": "synthetic"},
        on_screen=["Synthetic record"],
        offer=lambda _: None,
        canary_params=[],
        cache_dir=tmp_path / "cache",
    )


@pytest.fixture
def model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return build_model(tmp_path, monkeypatch)


def build_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from PIL import Image

    from openadapt_flow.jit import lifecycle

    cache = make_cache(tmp_path)
    directory = tmp_path / "cache" / "task"
    bundle = directory / "program" / "bundle"
    bundle.mkdir(parents=True)
    Image.new("RGB", (2, 2), "white").save(bundle / "target.png")
    Workflow(
        name="synthetic-task",
        steps=[
            Step(
                id="save",
                intent="Save synthetic value",
                action=ActionKind.CLICK,
                risk="irreversible",
                anchor=Anchor(
                    template="target.png", region=(0, 0, 2, 2), click_point=(1, 1)
                ),
            )
        ],
    ).save(bundle)
    digest = Workflow.load(bundle).manifest.content_digest
    monkeypatch.setattr(
        lifecycle, "verify_admission", lambda *_a, **_k: {"digest": digest}
    )
    jit = SimpleNamespace(admitted=True, bundle_dir=bundle, runs_dir=directory / "runs")
    jit.runs_dir.mkdir()
    rows: list[dict[str, str]] = []

    class Observer:
        @property
        def system_of_record(self):
            return copy.deepcopy(rows)

    compiler = SimpleNamespace(
        dir=directory,
        cache_dir=tmp_path / "cache",
        out_dir=directory / "compiled",
        observer=Observer(),
        key_dir=None,
        _jit=lambda: jit,
        require_credential_withholding=lambda: None,
    )
    compiler.out_dir.mkdir()
    server = compiler.out_dir / "server.py"
    server.write_text("# Model boundary only; never executed.\n")
    (compiler.out_dir / "emitted.json").write_text(
        json.dumps({"mcp_server": str(server)})
    )
    key = cache.key(PROMPT, PARAMS)
    (directory / skills._KEY_FILE).write_text(json.dumps(key))
    monkeypatch.setattr(cache, "_compiler", lambda *_: (compiler, key))
    monkeypatch.setattr(
        cache, "_first_run", lambda *_: pytest.fail("a first run, not a fallback")
    )
    return SimpleNamespace(
        cache=cache,
        compiler=compiler,
        jit=jit,
        rows=rows,
        sent=[],
        digest=digest,
        tmp=tmp_path,
        stop=directory / skills._STOP_FILE,
    )


def halted_run(model: SimpleNamespace, change: Optional[str] = None) -> Path:
    run = model.jit.runs_dir / f"halt-{len(list(model.jit.runs_dir.iterdir()))}"
    run.mkdir()
    report = RunReport(
        workflow_name="synthetic-task",
        started_at="2026-01-01T00:00:00Z",
        execution_outcome="HALTED",
        transaction_outcome="HALTED_BEFORE_EFFECT",
        bundle_content_digest=model.digest,
        halt=HaltObservation(state_id="save", reason="target_not_found"),
        results=[
            StepResult(
                step_id="save",
                intent="Save synthetic value",
                risk="irreversible",
                ok=False,
                delivery_attempted=False,
                error="Could not resolve target for step save",
            )
        ],
    )
    if change == "reconciliation":
        report.transaction_outcome = "RECONCILIATION_REQUIRED"
    elif change == "delivery":
        report.results[0].delivery_attempted = True
    elif change == "wrong_digest":
        report.bundle_content_digest = "f" * 64
    (run / "report.json").write_text(report.model_dump_json())
    return run


def install_reply(
    model: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    write: Optional[dict[str, str]] = None,
    report: Optional[str] = None,
    **reply: Any,
) -> None:
    async def hit(_python, _server, params):
        model.sent.append(dict(params))
        if write is not None:
            model.rows.append(dict(write))
        return {
            "status": "HALTED",
            "verified": False,
            "model_calls": 0,
            "receipt_outcome": None,
            "halt_reason": "target_not_found",
            "run_dir": str(halted_run(model, report)),
            **reply,
        }

    monkeypatch.setattr(skills, "_call_skill", hit)


class FakeRun1:
    """FirstRunCompiler's run 1, reduced to its contract with the policy:
    a baseline, one gated save, and the record check of a draft."""

    made: list["FakeRun1"] = []

    def __init__(self, prompt: str, *, rows: list, spend: Any = None, **kw: Any):
        self.kw = kw
        self.rows = rows
        self.spend = spend
        self._baseline: Optional[list] = None
        self.observer = SimpleNamespace()
        type(self).made.append(self)

    @property
    def system_of_record(self) -> list:
        return copy.deepcopy(self.rows)

    def _secret_params(self) -> frozenset:
        return frozenset()

    def record(self, agent: Any, params: dict, *, hold: Any) -> Any:
        self._baseline = copy.deepcopy(self.rows)
        agent.calls += 1
        agent.seen_cap = getattr(agent, "max_cost_usd", None)
        if self.spend is not None:
            agent.last_ledger = self.spend
        if agent.raises:
            raise RuntimeError("the agent crashed after its save")
        request = compile_agent.GateRequest(
            control="Save", identity=("Synthetic record",), url="", screenshot=None
        )
        approved = hold(request)
        gates = [{"decision": "approved_by_hold" if approved else "refused_by_hold"}]
        ledger = {"model_calls": agent.model_calls, "cost_usd": agent.cost}
        if not approved:
            return SimpleNamespace(
                status="refused_by_hold",
                detail="the hold did not approve 'Save'",
                run1={"agent": ledger, "gates": gates, "readbacks": []},
                record_check=None,
                to_dict=lambda: {"status": "refused_by_hold"},
            )
        self.rows.append({"id": "f1", "kind": "synthetic", "value": params["value"]})
        return SimpleNamespace(
            status="draft",
            detail="run 1 passed its record check",
            run1={"agent": ledger, "gates": gates, "readbacks": [{}]},
            record_check={"passed": True, "created": 1, "changed": 0, "removed": 0},
            to_dict=lambda: {"status": "draft"},
        )


class Agent:
    def __init__(self, *, model_free: bool = True, cap: Any = None, raises=False):
        self.calls = 0
        self.raises = raises
        self.requires_model_key = not model_free
        self.model_calls = 0 if model_free else 3
        self.cost = 0.0 if model_free else 0.01
        if cap is not None:
            self.max_cost_usd = cap


@pytest.fixture
def run1(model, monkeypatch) -> SimpleNamespace:
    state = SimpleNamespace(spend=None)
    FakeRun1.made = []

    def factory(prompt: str, **kw: Any) -> FakeRun1:
        made = FakeRun1(prompt, rows=model.rows, spend=state.spend, **kw)
        made.observer = made  # observer.system_of_record reads the same rows
        return made

    monkeypatch.setattr(compile_agent, "FirstRunCompiler", factory)
    return state


def policy(model, **kw: Any) -> FallbackPolicy:
    from openadapt_flow.agent.repair import RepairAccessPolicy, RepairDesk

    kw.setdefault("gate", lambda request: True)
    kw.setdefault("max_cost_usd", 0.5)
    desk = RepairDesk(model.tmp / "repairs", RepairAccessPolicy(None))
    kw.setdefault("repair_desk", desk)
    return FallbackPolicy(model.cache, **kw)


# -- when it falls back --------------------------------------------------------


def test_proved_pre_effect_drift_falls_back_behind_the_gate(
    model, monkeypatch, run1
) -> None:
    install_reply(model, monkeypatch)
    seen: list = []
    agent = Agent()
    chosen = policy(model, gate=lambda request: seen.append(request) or True)
    result = chosen.run(PROMPT, PARAMS, agent)
    assert result.path == fb.FALLBACK_PATH and result.verified, result.to_dict()
    assert result.status == "VERIFIED" and agent.calls == 1
    assert [r.control for r in seen] == ["Save"]
    assert model.rows == [{"id": "f1", "kind": "synthetic", "value": "one"}]
    # Its own run directory, beside (not inside) the task's cache.
    made = FakeRun1.made[0]
    root = model.tmp / "cache" / "fallback" / "task"
    assert made.kw["cache_dir"].parent.parent == root
    assert "reset" not in made.kw and made.kw["task_id"].startswith("skill-")
    assert not (root / fb._ACTIVE).exists()
    assert chosen.last["approvals"] == 1 and chosen.last["admitted"] is False


def test_verified_fallback_opens_a_repair_but_admits_nothing(
    model, monkeypatch, run1
) -> None:
    install_reply(model, monkeypatch)
    chosen = policy(model)
    result = chosen.run(PROMPT, PARAMS, Agent())
    repair = chosen.last["repair"]
    assert result.verified and repair["opened"] and repair["step_id"] == "save"
    token = json.loads(Path(repair["file"]).read_text())["repair_token"]
    assert token not in json.dumps(result.to_dict())
    assert (Path(repair["file"]).stat().st_mode & 0o777) == 0o600
    # The program stays held and unserved; no admission or selection happened.
    assert model.stop.is_file()
    assert model.cache.server(PROMPT, PARAMS) is None
    assert not (model.compiler.dir / "repair-selection.json").exists()
    assert chosen.last["admitted"] is False


def test_a_later_held_call_falls_back_on_the_same_halt(
    model, monkeypatch, run1
) -> None:
    install_reply(model, monkeypatch)
    plain = model.cache.run(PROMPT, PARAMS, Agent())
    assert plain.status == "HALTED"
    # Without the halted call going through the policy, no record check was
    # kept with the halt, so a held call does not fall back.
    later = policy(model).run(PROMPT, {"value": "two"}, Agent())
    assert later.status == "CACHE_HALTED" and model.rows == []
    model.stop.unlink()
    chosen = policy(model)
    assert chosen.run(PROMPT, PARAMS, Agent()).verified
    held = policy(model).run(PROMPT, {"value": "two"}, Agent())
    assert held.path == fb.FALLBACK_PATH and held.verified
    assert [r["value"] for r in model.rows] == ["one", "two"]
    assert len(model.sent) == 2  # the held call did not run the program again


# -- when it never falls back --------------------------------------------------


@pytest.mark.parametrize(
    ("case", "reply", "status"),
    [
        ("reconciliation_report", {"report": "reconciliation"}, "HALTED"),
        (
            "reconciliation_status",
            {"status": "RECONCILIATION_REQUIRED"},
            "RECONCILIATION_REQUIRED",
        ),
        ("delivery_attempted", {"report": "delivery"}, "HALTED"),
        ("unbound_report", {"report": "wrong_digest"}, "HALTED"),
        (
            "record_check_failed",
            {
                "verified": True,
                "receipt_outcome": "VERIFIED",
                "write": {"id": "1", "kind": "other", "value": "x"},
            },
            "RECORD_CHECK_FAILED",
        ),
        (
            "receipt_not_verified",
            {
                "verified": True,
                "write": {"id": "1", "kind": "synthetic", "value": "one"},
            },
            "RECEIPT_NOT_VERIFIED",
        ),
        ("model_call_in_replay", {"model_calls": 1}, "HALTED"),
        ("unproved_model_calls", {"model_calls": None}, "HALTED"),
        (
            "halt_after_a_write",
            {"write": {"id": "1", "kind": "synthetic", "value": "one"}},
            "HALTED",
        ),
    ],
)
def test_never_falls_back(model, monkeypatch, run1, case, reply, status) -> None:
    write = reply.pop("write", None)
    report = reply.pop("report", None)
    install_reply(model, monkeypatch, write=write, report=report, **reply)
    rows_before_agent = None
    agent = Agent()
    chosen = policy(model)
    result = chosen.run(PROMPT, PARAMS, agent)
    rows_before_agent = list(model.rows)
    assert result.status == status and result.path == "skill", case
    assert agent.calls == 0 and FakeRun1.made == []
    assert chosen.last is None or chosen.last["fallback"] is False
    # Held, and the next call is held too: never a fallback.
    again = policy(model).run(PROMPT, {"value": "two"}, agent)
    assert again.status == "CACHE_HALTED" and agent.calls == 0
    assert model.rows == rows_before_agent


@pytest.mark.parametrize("error", [RuntimeError("private"), KeyboardInterrupt()])
def test_a_lost_reply_never_falls_back(model, monkeypatch, run1, error) -> None:
    async def hit(*_):
        model.sent.append("sent")
        raise error

    monkeypatch.setattr(skills, "_call_skill", hit)
    agent = Agent()
    with pytest.raises(type(error)):
        policy(model).run(PROMPT, PARAMS, agent)
    again = policy(model).run(PROMPT, {"value": "two"}, agent)
    assert again.status == "CACHE_HALTED" and agent.calls == 0
    assert json.loads(model.stop.read_text())["phase"] == "running"


# -- the gate, the cap, the marker ----------------------------------------------


def test_gate_refusal_means_no_save(model, monkeypatch, run1) -> None:
    install_reply(model, monkeypatch)
    chosen = policy(model, gate=lambda request: False)
    result = chosen.run(PROMPT, PARAMS, Agent())
    assert result.status == "GATE_REFUSED" and not result.verified
    # Read the state, not the report: nothing was saved.
    assert model.cache.common["expect"] and model.rows == []
    assert chosen.last["task_record_check"]["created"] == 0
    root = model.tmp / "cache" / "fallback" / "task"
    assert not (root / fb._ACTIVE).exists()


def test_unattended_terminal_gate_refuses_before_the_agent(
    model, monkeypatch, run1
) -> None:
    install_reply(model, monkeypatch)
    gate = TerminalGate(ask=lambda _: pytest.fail("asked"), attended=lambda: False)
    agent = Agent()
    result = policy(model, gate=gate).run(PROMPT, PARAMS, agent)
    assert result.status == "FALLBACK_GATE_UNATTENDED" and agent.calls == 0
    assert gate(SimpleNamespace(control="Save")) is False
    assert model.rows == []


def test_attended_terminal_gate_uses_the_terminal_confirm(capsys) -> None:
    answers = iter(["y", "no"])
    gate = TerminalGate(ask=lambda _: next(answers), attended=lambda: True)
    request = compile_agent.GateRequest("Save", ("Synthetic record",), "", None)
    assert gate(request) is True and gate(request) is False
    assert "about to press 'Save'" in capsys.readouterr().out


@pytest.mark.parametrize("cap", [None, 0, -1, float("inf"), float("nan"), True, "1"])
def test_the_cap_is_required(model, cap) -> None:
    with pytest.raises(ValueError):
        FallbackPolicy(model.cache, max_cost_usd=cap)


def test_cost_cap_is_enforced(model, monkeypatch, run1) -> None:
    install_reply(model, monkeypatch)
    run1.spend = SimpleNamespace(model_calls=3, usage_known=True, cost_usd=lambda: 0.9)
    agent = Agent(model_free=False, cap=2.0)
    result = policy(model, max_cost_usd=0.5).run(PROMPT, PARAMS, agent)
    assert result.status == "COST_CAP_EXCEEDED" and not result.verified
    assert model.rows == []  # held at the gate: over the cap, no save
    assert agent.seen_cap == 0.5 and agent.max_cost_usd == 2.0


def test_unknown_spend_is_refused_at_the_gate(model, monkeypatch, run1) -> None:
    install_reply(model, monkeypatch)
    run1.spend = SimpleNamespace(model_calls=1, usage_known=False, cost_usd=lambda: 0)
    result = policy(model).run(PROMPT, PARAMS, Agent(model_free=False, cap=1.0))
    assert result.status == "COST_CAP_EXCEEDED" and model.rows == []


def test_an_uncappable_agent_is_not_started(model, monkeypatch, run1) -> None:
    install_reply(model, monkeypatch)
    agent = Agent(model_free=False)
    result = policy(model).run(PROMPT, PARAMS, agent)
    assert result.status == "FALLBACK_COST_UNBOUNDED" and agent.calls == 0


def test_a_crashed_fallback_needs_reconciliation(model, monkeypatch, run1) -> None:
    install_reply(model, monkeypatch)
    with pytest.raises(RuntimeError):
        policy(model).run(PROMPT, PARAMS, Agent(raises=True))
    agent = Agent()
    again = policy(model).run(PROMPT, {"value": "two"}, agent)
    assert again.status == "RECONCILIATION_REQUIRED" and agent.calls == 0
    assert fb.clear_in_flight(model.cache, PROMPT, PARAMS)
    assert policy(model).run(PROMPT, {"value": "two"}, agent).verified


def test_a_fallback_request_id_runs_once(model, monkeypatch, run1) -> None:
    install_reply(model, monkeypatch)
    assert policy(model, request_id="r1.fallback").run(PROMPT, PARAMS, Agent()).verified
    agent = Agent()
    again = policy(model, request_id="r1.fallback").run(PROMPT, PARAMS, agent)
    assert again.status == "FALLBACK_REQUEST_REUSED" and agent.calls == 0
    runs = fb.fallback_runs(model.cache, PROMPT, PARAMS)
    assert [r["status"] for r in runs["runs"]] == ["VERIFIED"]
    assert not runs["in_flight"]


@pytest.mark.parametrize("bad", ["", "../x", ".hidden", "a/b", "x" * 129])
def test_request_ids_are_plain_names(model, bad) -> None:
    with pytest.raises(ValueError):
        FallbackPolicy(model.cache, max_cost_usd=1, request_id=bad)


# -- the default is today's hold --------------------------------------------------


def test_an_ineligible_halt_through_the_policy_is_byte_identical(
    tmp_path, monkeypatch
) -> None:
    """The default (no policy) is today's hold. Through an opt-in policy, a
    halt that may not fall back returns the cache's own result object and
    leaves the same hold bytes, with no fallback directory."""

    results = {}
    for name in ("plain", "policy"):
        root = tmp_path / name
        root.mkdir()
        m = build_model(root, monkeypatch)
        install_reply(m, monkeypatch, report="reconciliation")
        runner: Any = m.cache
        if name == "policy":
            runner = FallbackPolicy(
                m.cache, max_cost_usd=1, gate=lambda r: pytest.fail("gate")
            )
        agent = Agent()
        first = runner.run(PROMPT, PARAMS, agent)
        second = runner.run(PROMPT, {"value": "two"}, agent)
        assert agent.calls == 0
        hold = m.stop.read_text().replace(str(root), "ROOT").replace(m.digest, "DIGEST")
        results[name] = (
            {**first.to_dict(), "seconds": 0, "replay": None},
            {**second.to_dict(), "seconds": 0, "replay": None},
            first.replay["status"],
            hold,
            sorted(p.name for p in (root / "cache").iterdir()),
        )
    assert results["plain"] == results["policy"]
    assert "fallback" not in results["policy"][4]


@pytest.mark.parametrize("report", [None, "reconciliation", "delivery", "wrong_digest"])
def test_eligibility_agrees_with_open_repair(model, monkeypatch, report) -> None:
    """proved_before_effect mirrors SkillCache.open_repair's halt checks
    (inline in skills.py); pin the two together on the same halts."""
    from openadapt_flow.agent.repair import RepairAccessPolicy, RepairDesk, RepairError

    install_reply(model, monkeypatch, report=report)
    assert model.cache.run(PROMPT, PARAMS, Agent()).status == "HALTED"
    found = fb.proved_before_effect(model.cache, PROMPT, PARAMS)
    desk = RepairDesk(model.tmp / "repairs", RepairAccessPolicy(None))
    try:
        model.cache.open_repair(PROMPT, PARAMS, repair_desk=desk)
        opened = True
    except RepairError:
        opened = False
    assert found.eligible is opened is (report is None), (report, found.reason)
