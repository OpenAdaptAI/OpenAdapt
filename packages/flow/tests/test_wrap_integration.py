"""Wrapper/cache/store integration with real SQLite reads and writes.

The browser capture, post-check admission/emission and compiled MCP action are
explicit component fixtures. The real FirstRunCompiler record check and pipeline,
SkillCache, RunStore, public MCP SDK and SQLite observer remain in use. These are
not real-agent, browser, admitted-program or held-out application results.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from openadapt_flow.agent.runs import RunStore
from openadapt_flow.compile_agent import pipeline
from openadapt_flow.integrations.agents import skills
from openadapt_flow.ir import Workflow
from openadapt_flow.wrap import TaskArgumentsError, WrappedTask

PROMPT = "Create the task record with note {note}"


class ComponentAgent:
    dialect = "component"
    requires_model_key = False

    def __init__(self, fixture: SqliteComponent) -> None:
        self.fixture = fixture
        self.calls = 0
        self.summary = "Saved!"

    def perform(self, params: Mapping[str, Any]) -> None:
        self.calls += 1
        self.fixture.events.append("agent")
        self.fixture.write(str(params["note"]), self.fixture.agent_fault)
        if self.fixture.block_agent:
            self.fixture.entered.set()
            assert self.fixture.release.wait(5), "component owner was not released"


class CapturedJit:
    """Captured-action boundary only; it does not create real admission."""

    def __init__(self, fixture: SqliteComponent, compiler: Any) -> None:
        self.fixture = fixture
        self.compiler = compiler
        self.program_dir = compiler.dir / "component-program"
        self.bundle_dir = self.program_dir / "bundle"
        self.runs_dir = compiler.dir / "component-runs"
        self.is_admitted = False
        self.agent: Any = None

    @property
    def admitted(self) -> bool:
        return self.is_admitted and (self.bundle_dir / "workflow.json").is_file()

    def candidate(self) -> dict[str, str] | None:
        if (self.program_dir / "captured.json").is_file():
            return {"fixture": "captured action, not a qualified program"}
        return None

    def run(self, **params: Any) -> Any:
        self.program_dir.mkdir(parents=True, exist_ok=True)
        self.compiler._baseline = self.compiler.observer.system_of_record
        self.fixture.events.append("read-before")
        self.agent.inner.perform(params)
        self.agent.ledger = SimpleNamespace(
            to_dict=lambda: {"summary": "Saved!", "usage_known": False}
        )
        (self.program_dir / "captured.json").write_text('{"component": true}')
        return SimpleNamespace(status=pipeline.lifecycle_status_draft(), detail=None)


class SqliteComponent:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.database = root / "records.sqlite"
        with sqlite3.connect(self.database) as con:
            con.execute(
                "CREATE TABLE task_records (id INTEGER PRIMARY KEY, owner TEXT, note TEXT)"
            )
        self.verify = root / "verify.yaml"
        self.verify.write_text(
            yaml.safe_dump(
                {"verifier": {"kind": "sql", "sqlite_database": str(self.database),
                              "sql_query": "SELECT id, owner, note FROM task_records ORDER BY id"}}
            )
        )
        self.events: list[str] = []
        self.states: dict[str, CapturedJit] = {}
        self.agent_fault = "ok"
        self.skill_fault = "ok"
        self.skill_calls: list[dict[str, Any]] = []
        self.block_agent = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.agent = ComponentAgent(self)
        self.cache = skills.SkillCache(
            url="https://component.invalid/",
            verify=self.verify,
            expect={"owner": "scope-1"},
            on_screen=["Scope 1"],
            offer=lambda _proposal: "component-reviewer",
            canary_params=[{"note": "component canary"}],
            cache_dir=root / "cache",
            environment="component-v1",
        )
        original_check = pipeline.check_run_record

        def checked(*args: Any, **kwargs: Any) -> Any:
            result = original_check(*args, **kwargs)
            self.events.append("record-check-pass" if result.passed else "record-check-fail")
            return result

        def jit(compiler: Any, agent: Any = None) -> CapturedJit:
            key = str(compiler.dir)
            if key not in self.states:
                self.states[key] = CapturedJit(self, compiler)
            state = self.states[key]
            state.compiler = compiler
            return state

        def confirm(compiler: Any, **kwargs: Any) -> Path:
            self.events.append("component-confirm")
            path = compiler.out_dir / pipeline.CONFIRMED_FILE
            path.write_text("component fixture, not operator admission")
            return path

        def admit(compiler: Any, **kwargs: Any) -> Any:
            self.events.append("component-admit")
            jit(compiler).is_admitted = True
            return pipeline.CompileResult(
                status="admitted", dir=compiler.dir, task_key=compiler.task.key
            )

        def emit(compiler: Any) -> dict[str, str]:
            self.events.append("component-emit")
            state = jit(compiler)
            Workflow(name="component-only", steps=[]).save(state.bundle_dir)
            server = compiler.out_dir / "component_server.py"
            server.write_text("raise RuntimeError('component MCP boundary must be intercepted')\n")
            result = {"mcp_server": str(server)}
            (compiler.out_dir / "emitted.json").write_text(json.dumps(result))
            return result

        async def call_skill(python: str, server: Path, params: dict[str, Any]) -> dict[str, Any]:
            self.skill_calls.append(dict(params))
            if self.skill_fault == "drift":
                self.events.append("compiled-prewrite-halt")
                return {"status": "HALTED", "verified": False, "model_calls": 0,
                        "receipt_outcome": "HALTED", "halt_reason": "component target drift"}
            self.write(str(params["note"]), self.skill_fault)
            # Deliberately claims success even for fault modes. The genuine
            # cache's independent SQLite read-back must reject this claim.
            return {"status": "VERIFIED", "verified": True, "model_calls": 0,
                    "receipt_outcome": "VERIFIED", "receipt": "component-boundary-only"}

        monkeypatch.setattr(pipeline, "check_run_record", checked)
        monkeypatch.setattr(pipeline.FirstRunCompiler, "_jit", jit)
        monkeypatch.setattr(pipeline.FirstRunCompiler, "confirm_checks", confirm)
        monkeypatch.setattr(pipeline.FirstRunCompiler, "admit", admit)
        monkeypatch.setattr(pipeline.FirstRunCompiler, "emit", emit)
        monkeypatch.setattr(skills, "_call_skill", call_skill)

    def write(self, note: str, fault: str = "ok") -> None:
        if fault == "no-write":
            return
        owner = "other-scope" if fault == "wrong-scope" else "scope-1"
        value = "wrong-value" if fault == "wrong-value" else note
        with sqlite3.connect(self.database) as con:
            con.execute("INSERT INTO task_records(owner, note) VALUES (?, ?)", (owner, value))
            if fault == "extra-write":
                con.execute("INSERT INTO task_records(owner, note) VALUES (?, ?)", (owner, "extra"))

    def rows(self) -> list[tuple[int, str, str]]:
        with sqlite3.connect(self.database) as con:
            return con.execute("SELECT id, owner, note FROM task_records ORDER BY id").fetchall()

    def task(self) -> WrappedTask:
        return WrappedTask(cache=self.cache, agent=self.agent, instruction=PROMPT,
                           parameters=("note",), runs_dir=self.root / "requests")


@pytest.fixture
def component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SqliteComponent:
    return SqliteComponent(tmp_path, monkeypatch)


def test_miss_checks_sqlite_before_tool_promotion_then_new_values_skip_agent(
    component: SqliteComponent,
) -> None:
    task = component.task()
    first = task.run({"note": "first value"})
    assert first.path == "agent" and first.verified and first.status == "admitted"
    assert first.record_check["passed"] is True
    assert component.events == ["read-before", "agent", "record-check-pass",
                                "component-confirm", "component-admit", "component-emit"]
    assert component.cache.server(PROMPT, {"note": "first value"}) is not None
    for note in ("second value", "third value"):
        result = task.run({"note": note})
        assert result.path == "skill" and result.verified
        assert result.model_calls == 0 and result.cost_usd == 0.0
        assert result.record_check["passed"] is True
    assert component.agent.calls == 1
    assert component.skill_calls == [{"note": "second value"}, {"note": "third value"}]
    assert [row[2] for row in component.rows()] == ["first value", "second value", "third value"]


@pytest.mark.parametrize("fault", ["no-write", "wrong-value", "wrong-scope", "extra-write"])
def test_saved_summary_cannot_promote_a_miss_without_native_record_check(
    component: SqliteComponent, fault: str,
) -> None:
    component.agent_fault = fault
    result = component.task().run({"note": "intended value"})
    assert component.agent.summary == "Saved!"
    assert result.status == "record_check_failed" and result.verified is False
    assert result.record_check["passed"] is False
    assert component.events == ["read-before", "agent", "record-check-fail"]
    assert component.cache.server(PROMPT, {"note": "intended value"}) is None
    assert not list((component.root / "cache").rglob("skill-key.json"))
    assert not list((component.root / "cache").rglob("emitted.json"))
    assert component.skill_calls == []


@pytest.mark.parametrize("fault", ["no-write", "wrong-value", "wrong-scope", "extra-write"])
def test_saved_claim_on_hit_is_refuted_and_cannot_fall_back_to_agent(
    component: SqliteComponent, fault: str,
) -> None:
    task = component.task()
    assert task.run({"note": "first"}).verified
    component.skill_fault = fault
    failed = task.run({"note": "must land"})
    assert failed.path == "skill" and not failed.verified
    assert failed.status == "RECORD_CHECK_FAILED"
    assert failed.record_check["passed"] is False
    before = component.rows()
    stopped = task.run({"note": "new request after failure"})
    assert stopped.status == "CACHE_HALTED" and stopped.verified is False
    assert component.rows() == before
    assert len(component.skill_calls) == 1 and component.agent.calls == 1


def test_compiled_prewrite_drift_stays_held_across_new_request_and_restart(
    component: SqliteComponent,
) -> None:
    assert component.task().call("first", {"note": "first"})["verified"] is True
    before = component.rows()
    component.skill_fault = "drift"
    halted = component.task().call("drift", {"note": "second"})
    assert halted["status"] == "HALTED" and halted["verified"] is False
    assert component.events[-1] == "compiled-prewrite-halt"
    assert component.rows() == before
    again = component.task().call("another-id", {"note": "second"})
    assert again["status"] == "CACHE_HALTED" and again["verified"] is False
    assert len(component.skill_calls) == 1 and component.agent.calls == 1
    assert component.rows() == before


@pytest.mark.parametrize("field", ["instruction", "task", "policy", "verify", "provider", "agent", "cache"])
@pytest.mark.parametrize("entry", ["run", "call"])
def test_caller_cannot_replace_operator_task_policy_or_provider(
    component: SqliteComponent, field: str, entry: str,
) -> None:
    values = {"note": "synthetic", field: "caller replacement"}
    task = component.task()
    with pytest.raises(TaskArgumentsError):
        if entry == "run":
            task.run(values)
        else:
            task.call("override", values)
    assert component.agent.calls == 0 and component.skill_calls == []
    assert component.rows() == [] and component.events == []
    assert not (component.root / "requests").exists()


def test_durable_duplicate_replays_and_changed_parameters_conflict(
    component: SqliteComponent,
) -> None:
    first = component.task().call("same-id", {"note": "first"})
    replay = component.task().call("same-id", {"note": "first"})
    conflict = component.task().call("same-id", {"note": "different"})
    assert first["verified"] is True
    assert replay["run_id"] == first["run_id"] and replay["replayed"] is True
    assert conflict["status"] == "REQUEST_ID_CONFLICT" and conflict["verified"] is False
    assert component.agent.calls == 1 and component.skill_calls == []
    assert [row[2] for row in component.rows()] == ["first"]
    store = RunStore(component.root / "requests")
    assert store.ledger_kind == "openadapt-flow"
    record = store.read(first["run_id"])
    assert record is not None and record["state"] == "finished"
    assert "inputs" not in record and record["result"]["verified"] is True


async def mcp_session(task: WrappedTask, steps: Any) -> Any:
    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    from openadapt_flow.wrap.mcp import build_server

    server = build_server(task)
    async with create_client_server_memory_streams() as (client, transport):
        async with anyio.create_task_group() as group:
            group.start_soon(server.run, *transport, server.create_initialization_options())
            async with ClientSession(*client) as session:
                await session.initialize()
                result = await steps(session)
            group.cancel_scope.cancel()
    return result


def payload(result: Any) -> dict[str, Any]:
    return json.loads(result.content[0].text)


def test_real_mcp_schema_rejects_policy_overrides_before_any_action(
    component: SqliteComponent,
) -> None:
    import anyio

    async def steps(session: Any) -> None:
        listed = await session.list_tools()
        assert len(listed.tools) == 1 and listed.tools[0].name == "run_task"
        schema = listed.tools[0].inputSchema
        assert schema["additionalProperties"] is False
        assert schema["properties"]["parameters"]["additionalProperties"] is False
        for field in ("instruction", "policy", "provider", "verify"):
            for nested in (False, True):
                arguments: dict[str, Any] = {"request_id": "override", "parameters": {"note": "first"}}
                target = arguments["parameters"] if nested else arguments
                target[field] = "caller replacement"
                result = await session.call_tool("run_task", arguments)
                assert result.isError is True
                assert payload(result) == {"error": "invalid task call"}

    anyio.run(mcp_session, component.task(), steps)
    assert component.rows() == [] and component.agent.calls == 0
    assert component.skill_calls == []


@pytest.mark.parametrize("same_parameters", [True, False])
def test_real_mcp_concurrent_duplicate_never_sends_a_second_action(
    component: SqliteComponent, same_parameters: bool,
) -> None:
    import anyio

    component.block_agent = True

    async def steps(session: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}

        async def first() -> None:
            out["first"] = payload(await session.call_tool(
                "run_task", {"request_id": "concurrent", "parameters": {"note": "first"}}
            ))

        with anyio.fail_after(10):
            async with anyio.create_task_group() as group:
                group.start_soon(first)
                assert await anyio.to_thread.run_sync(component.entered.wait, 3)
                try:
                    note = "first" if same_parameters else "changed"
                    with anyio.fail_after(3):
                        result = await session.call_tool(
                            "run_task", {"request_id": "concurrent", "parameters": {"note": note}}
                        )
                    out["duplicate"] = payload(result)
                finally:
                    component.release.set()
        out["replay"] = payload(await session.call_tool(
            "run_task", {"request_id": "concurrent", "parameters": {"note": "first"}}
        ))
        return out

    try:
        out = anyio.run(mcp_session, component.task(), steps)
    finally:
        component.release.set()
    expected = "RECONCILIATION_REQUIRED" if same_parameters else "REQUEST_ID_CONFLICT"
    assert out["duplicate"]["status"] == expected and not out["duplicate"]["verified"]
    assert out["first"]["verified"] is True and out["replay"]["replayed"] is True
    assert out["replay"]["run_id"] == out["first"]["run_id"]
    assert component.agent.calls == 1 and component.skill_calls == []
    assert [row[2] for row in component.rows()] == ["first"]
