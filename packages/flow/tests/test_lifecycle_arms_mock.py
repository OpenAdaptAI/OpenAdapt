"""The comparison arms end to end on the offline OpenEMR mock, with scripted stand-ins for the model.

No API key and no fixture: the "model" is a policy that reads the tool results
and answers like a competent agent would, so the arms' loops, tools, spend
metering and maintenance path run for real against a browser.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from benchmark.lifecycle.arm import OperatorDecision, WorkItem
from benchmark.lifecycle.arms import (
    agent_script,
    agent_tools,
    budget,
    cua,
    human_script,
)
from benchmark.lifecycle.arms.mock_openemr import MockOpenEMR, mock_env

pytest.importorskip("playwright.sync_api")

ROSA = dict(
    title="Mrs.",
    fname="Rosa",
    lname="Delgado",
    DOB="1958-03-27",
    sex="Female",
    street="311 BIRCH COURT",
    city="EXAMPLETON",
    state_label="Massachusetts",
    postal_code="02139",
    phone_home="555-0119",
    email="r@example.invalid",
    country_code="USA",
)
NADIA = dict(
    ROSA,
    title="Ms.",
    fname="Nadia",
    lname="Brennan",
    DOB="1971-04-19",
    street="27 WILLOW BEND ROAD",
    phone_home="555-0121",
    email="n@example.invalid",
)
_ids = itertools.count()


def item(params: dict[str, str], seq: int = 1) -> WorkItem:
    return WorkItem(
        seq=seq,
        item_id=f"ref-{seq:05d}",
        day=0,
        app="openemr",
        params=params,
        source_text="\n".join(f"{k}: {v}" for k, v in params.items()),
    )


class Op:
    def __init__(self) -> None:
        self.asked: list[Any] = []

    def ask(self, req: Any) -> OperatorDecision:
        self.asked.append(req)
        if req.kind == "repair_review":
            ok = req.detail.get("probes_passed")
            return OperatorDecision(
                "accept_repair" if ok else "reject_repair", 140.0, "test"
            )
        return OperatorDecision("do_by_hand", 120.0, "test")


def usage(i: int = 1000, o: int = 100) -> Any:
    return SimpleNamespace(
        input_tokens=i,
        output_tokens=o,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def tool_use(name: str, inp: dict[str, Any]) -> Any:
    return SimpleNamespace(type="tool_use", id=f"tu{next(_ids)}", name=name, input=inp)


def text(t: str) -> Any:
    return SimpleNamespace(type="text", text=t)


class FakeClient:
    """messages.create / messages.stream driven by ``policy(messages) -> content blocks``."""

    def __init__(self, policy: Callable[[list[dict[str, Any]]], list[Any]]) -> None:
        self.policy = policy
        self.calls = 0
        outer = self

        class _Stream:
            def __init__(self, resp: Any) -> None:
                self.resp = resp

            def __enter__(self) -> "_Stream":
                return self

            def __exit__(self, *a: Any) -> None:
                return None

            def get_final_message(self) -> Any:
                return self.resp

        class _Messages:
            def create(self, **kw: Any) -> Any:
                outer.calls += 1
                content = outer.policy(kw["messages"])
                stop = (
                    "tool_use"
                    if any(b.type == "tool_use" for b in content)
                    else "end_turn"
                )
                return SimpleNamespace(content=content, usage=usage(), stop_reason=stop)

            def stream(self, **kw: Any) -> Any:
                return _Stream(self.create(**kw))

        self.messages = _Messages()


def last_results(messages: list[dict[str, Any]]) -> list[str]:
    content = messages[-1]["content"]
    if isinstance(content, str):
        return [content]
    return [
        c["content"] if isinstance(c["content"], str) else json.dumps(c["content"])
        for c in content
    ]


@pytest.fixture
def spend(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "SPEND_PATH", tmp_path / "spend.json")
    monkeypatch.setenv("LIFECYCLE_SPEND_CAP_USD", "5")
    return tmp_path / "spend.json"


@pytest.fixture
def app():
    a = MockOpenEMR()
    a.seed(ROSA)
    return a


def tools_policy(params: dict[str, str]) -> Callable[[list[dict[str, Any]]], list[Any]]:
    """A competent tools agent: check the record, fill, save, confirm, read back."""

    state = {"step": 0}

    def refs(screen: dict[str, Any]) -> dict[str, str]:
        out = {}
        for fr in screen["frames"]:
            for c in fr["controls"]:
                out[c["id"]] = c["ref"]
            for b in fr["buttons"]:
                out[b.get("id") or b["text"]] = b["ref"]
        return out

    def policy(messages: list[dict[str, Any]]) -> list[Any]:
        s = state["step"]
        state["step"] += 1
        if s == 0:
            return [
                tool_use(
                    "find_patients",
                    {"fname": params["fname"], "lname": params["lname"]},
                )
            ]
        if s == 1:
            found = json.loads(last_results(messages)[0])
            if any(p["DOB"] == params["DOB"] for p in found):
                return [
                    tool_use(
                        "finish", {"result": "NOT SAVED", "reason": "chart exists"}
                    )
                ]
            return [tool_use("read_screen", {})]
        if s == 2:
            r = refs(json.loads(last_results(messages)[0]))
            calls = [
                tool_use("set_field", {"ref": r[f"form_{k}"], "value": params[k]})
                for k in ("title", "fname", "lname", "DOB", "sex")
            ]
            return calls + [tool_use("click_text", {"text": "Contact"})]
        if s == 3:
            return [tool_use("read_screen", {})]
        if s == 4:
            r = refs(json.loads(last_results(messages)[0]))
            calls = [
                tool_use("set_field", {"ref": r[f"form_{k}"], "value": params[k]})
                for k in ("street", "city", "postal_code", "phone_home", "email")
            ]
            calls.append(
                tool_use(
                    "set_field",
                    {"ref": r["form_state"], "value": params["state_label"]},
                )
            )
            calls.append(
                tool_use("set_field", {"ref": r["form_country_code"], "value": "USA"})
            )
            return calls + [tool_use("click", {"ref": r["create"]})]
        if s == 5:
            return [tool_use("read_screen", {})]
        if s == 6:
            r = refs(json.loads(last_results(messages)[0]))
            return [tool_use("click", {"ref": r["confirmCreate"]})]
        if s == 7:
            return [
                tool_use(
                    "find_patients",
                    {
                        "fname": params["fname"],
                        "lname": params["lname"],
                        "DOB": params["DOB"],
                    },
                )
            ]
        found = json.loads(last_results(messages)[0])
        res = "SAVED" if len(found) == 1 else "NOT SAVED"
        return [
            text("checked"),
            tool_use(
                "finish", {"result": res, "reason": f"{len(found)} chart(s) read back"}
            ),
        ]

    return policy


def test_agent_tools_registers_and_meters(tmp_path, spend, app, monkeypatch):
    monkeypatch.setattr(budget, "client", lambda *a: FakeClient(tools_policy(NADIA)))
    arm = agent_tools.factory({"model": "claude-opus-5-5"})
    env, op = mock_env(app, tmp_path), Op()
    try:
        arm.setup(env, op)
        res = arm.run_item(item(NADIA), env, op)
    finally:
        arm.close()
    assert res.claimed == "done", res.product_status
    assert [p["fname"] for p in app.patients] == ["Rosa", "Nadia"]
    assert res.model_calls == 9 and res.cost_usd > 0
    assert json.loads(spend.read_text())["total_usd"] == pytest.approx(
        res.cost_usd, abs=1e-6
    )
    assert not op.asked


def test_agent_tools_stops_on_an_existing_chart(tmp_path, spend, app, monkeypatch):
    monkeypatch.setattr(budget, "client", lambda *a: FakeClient(tools_policy(ROSA)))
    arm = agent_tools.factory({})
    env, op = mock_env(app, tmp_path), Op()
    try:
        arm.setup(env, op)
        res = arm.run_item(item(ROSA), env, op)
    finally:
        arm.close()
    assert res.claimed == "stopped" and len(app.patients) == 1
    assert [r.kind for r in op.asked] == ["stop"]


def test_item_budget_turns_into_a_stop(tmp_path, spend, app, monkeypatch):
    monkeypatch.setattr(
        budget, "client", lambda *a: FakeClient(lambda m: [tool_use("read_screen", {})])
    )
    arm = agent_tools.factory({"item_budget_usd": 0.02})
    env, op = mock_env(app, tmp_path), Op()
    try:
        arm.setup(env, op)
        res = arm.run_item(item(NADIA), env, op)
    finally:
        arm.close()
    assert res.claimed == "error" and res.product_status.startswith("budget")
    assert op.asked and op.asked[0].kind == "stop"


def test_cua_reads_the_result_line(tmp_path, spend, app, monkeypatch):
    replies = iter(
        [
            [tool_use("screenshot", {})],
            [text("All done.\nRESULT: NOT SAVED: test stand-in")],
        ]
    )
    monkeypatch.setattr(
        budget, "client", lambda *a: FakeClient(lambda m: next(replies))
    )
    arm = cua.factory({"model": "claude-opus-5-5"})
    env, op = mock_env(app, tmp_path), Op()
    try:
        arm.setup(env, op)
        res = arm.run_item(item(NADIA), env, op)
    finally:
        arm.close()
    assert res.claimed == "stopped" and res.model_calls == 2
    assert (tmp_path / "cua_alone" / "ref-00001" / "calls.json").is_file()


V1 = (Path(agent_script.__file__).parent / "human_script_v1.py").read_text()
V2 = V1.replace(
    "def _fill(ctx, r):\n    form = ctx.form\n",
    "def _fill(ctx, r):\n    form = ctx.form\n"
    "    if form.locator('#notice-ok').count():\n        form.locator('#notice-ok').click()\n",
)


def script_policy(
    source_for_turn: Callable[[int], str],
) -> Callable[[list[dict[str, Any]]], list[Any]]:
    state = {"turn": 0}

    def policy(messages: list[dict[str, Any]]) -> list[Any]:
        t = state["turn"]
        state["turn"] += 1
        src = source_for_turn(t)
        if t % 2 == 0:
            return [tool_use("dry_run", {"source": src, "referral": NADIA})]
        return [tool_use("submit", {"source": src})]

    return policy


def test_agent_script_authors_runs_and_maintains(tmp_path, spend, app, monkeypatch):
    sources = {0: V1, 1: V1, 2: V2, 3: V2}
    monkeypatch.setattr(
        budget, "client", lambda *a: FakeClient(script_policy(lambda t: sources[t]))
    )
    # One client per session loop; keep the turn counter across both loops.
    shared = FakeClient(script_policy(lambda t: sources[t]))
    monkeypatch.setattr(budget, "client", lambda *a: shared)
    arm = agent_script.factory({"model": "claude-opus-5-5"})
    env, op = mock_env(app, tmp_path), Op()
    try:
        phases = arm.setup(env, op)
        assert [p["phase"] for p in phases] == ["author_script", "compile"]
        assert (
            phases[0]["cost_usd"] > 0
            and phases[0]["detail"]["dry_runs"][0]["save_requests_blocked"] == 1
        )
        assert len(app.patients) == 1  # the dry run wrote nothing
        first = arm.run_item(item(NADIA, 1), env, op)
        assert (
            first.claimed == "done" and first.model_calls == 0 and first.cost_usd == 0
        )
        app.drift.add("interstitial")
        grace = dict(NADIA, fname="Grace", lname="Adeyemi", email="g@example.invalid")
        second = arm.run_item(item(grace, 2), env, op)
    finally:
        arm.close()
    assert second.claimed == "done", second.product_status
    m = second.phases
    assert len(m) == 1 and m[0]["detail"]["accepted"] and m[0]["cost_usd"] > 0
    assert [r.kind for r in op.asked] == ["repair_review"]
    assert (tmp_path / "agent_script" / "script_v2.py").is_file()
    assert [p["fname"] for p in app.patients] == ["Rosa", "Nadia", "Grace"]


def test_human_script_breaks_on_drift_and_hands_over(tmp_path, app):
    arm = human_script.factory({})
    env, op = mock_env(app, tmp_path), Op()
    try:
        phases = arm.setup(env, op)
        assert phases[0]["attended_s"] > 0
        assert arm.run_item(item(NADIA, 1), env, op).claimed == "done"
        app.drift.add("interstitial")
        res = arm.run_item(
            item(dict(NADIA, fname="Theo", lname="Lindqvist"), 2), env, op
        )
    finally:
        arm.close()
    assert res.claimed == "stopped" and res.detail["broke"]
    assert [r.kind for r in op.asked] == ["stop"]
