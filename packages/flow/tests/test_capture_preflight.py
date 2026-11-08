"""Trusted capture hooks; generic local fixtures, no model or app recipe.

Native/session-boundary tests use explicit resource doubles. Browser tests use
genuine normal compiler paths and headless contexts. Dispatch-only stubs do
not establish admission, cache verification or native qualification.
"""

from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow import authoring
from openadapt_flow.compile_agent.pipeline import FirstRunCompiler, compile_first_run
from openadapt_flow.integrations.agents.recording import TaskSetupError, load_task
from openadapt_flow.integrations.agents.skills import SkillCache
from openadapt_flow.jit import SurfaceApp, TaskSignature, WebApp, compile_agent
from openadapt_flow.jit.compile_agent import CompiledAgent


class Cancelled(BaseException):
    pass


class Observed(BaseException):
    pass


class NoModelActor:
    dialect = "generic"
    requires_model_key = False
    drives_surfaces = True

    def __init__(self, events: list[Any], expected: str = "ready"):
        self.events = events
        self.expected = expected

    def read(self, computer: Any) -> None:
        page = computer.page
        self.events.append(("actor", page, page.context))
        assert page.locator("#prepared").inner_text() == self.expected
        raise Observed()

    def run(self, request: Any, computer: Any) -> None:
        self.read(computer)

    def drive(self, request: Any, harness: Any) -> None:
        self.read(harness.computer)


@pytest.fixture
def local_task(tmp_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'<!doctype html><p id="prepared">unprepared</p>')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        database = tmp_path / "records.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE entries(id INTEGER PRIMARY KEY, title TEXT)"
            )
        task = {
            "instruction": "Create {before_capture}",
            "parameters": ["before_capture"],
            "url": f"http://127.0.0.1:{server.server_port}/",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(database),
                "sql_query": "SELECT id, title FROM entries",
            },
            "expect": {"title": "synthetic"},
            "on_screen": ["Synthetic record"],
            "canary_params": [{"before_capture": "unrun-canary"}],
            "cache_dir": str(tmp_path / "cache"),
            "headless": True,
        }
        path = tmp_path / "task.json"
        path.write_text(json.dumps(task))
        yield path, task
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def invoke(route: str, path: Path, hook: Any, actor: Any, tmp_path: Path):
    setup = load_task(path, before_capture=hook)
    values = {"before_capture": "synthetic"}
    if route == "direct":
        jit = compile_agent(
            actor,
            task=TaskSignature(
                "capture.test", "Create {before_capture}", {"before_capture": str}
            ),
            app=WebApp(url=setup.cache.common["url"]),
            verifier=None,
            policy="clinical-write",
            cache_dir=tmp_path / "direct",
            before_capture=hook,
        )
        return jit.run(**values)
    if route in ("compiler", "reload"):
        compiler, _ = setup.cache._compiler(setup.instruction, values)
        if route == "reload":
            spec = compiler.save_spec()
            assert "before_capture" not in json.loads(spec.read_text())
            compiler = FirstRunCompiler.load(compiler.dir, before_capture=hook)
        return compiler.record(actor, values)
    if route == "first_run":
        return compile_first_run(
            setup.instruction,
            params=values,
            agent=actor,
            confirm_checks=lambda _: None,
            canary_params=setup.cache.canary_params,
            before_capture=hook,
            **setup.cache.common,
        )
    if route == "cache":
        cache = SkillCache(
            offer=lambda _: None,
            canary_params=setup.cache.canary_params,
            before_capture=hook,
            **setup.cache.common,
        )
        return cache.run(setup.instruction, values, actor)
    assert route == "loader"
    return setup.cache.run(setup.instruction, values, actor)


@pytest.mark.parametrize(
    "route", ["direct", "compiler", "reload", "first_run", "cache", "loader"]
)
@pytest.mark.parametrize("refusal", [None, "preflight", "reservation", "cancel"])
def test_real_capture_owns_preparation_and_actor_context(
    local_task, tmp_path, route, refusal
):
    path, _ = local_task
    events = []

    def prepare(backend):
        page = backend.page
        events.append(("prepare", page, page.context))
        page.locator("#prepared").evaluate("element => element.textContent = 'ready'")
        assert page.locator("#prepared").inner_text() == "ready"
        if refusal == "preflight":
            raise ValueError("preflight refusal")
        if refusal == "cancel":
            raise Cancelled()
        events.append(("reservation",))
        if refusal == "reservation":
            raise ValueError("reservation refusal")

    expected = (
        Observed
        if refusal is None
        else Cancelled
        if refusal == "cancel"
        else ValueError
    )
    with pytest.raises(expected):
        invoke(route, path, prepare, NoModelActor(events), tmp_path)
    assert [e[0] for e in events] == (
        ["prepare", "reservation", "actor"]
        if refusal is None
        else ["prepare", "reservation"]
        if refusal == "reservation"
        else ["prepare"]
    )
    prepared = events[0]
    assert prepared[1].is_closed()
    if refusal is None:
        assert events[-1][1] is prepared[1] and events[-1][2] is prepared[2]


def test_loader_does_not_accept_a_callback_from_task_json(local_task):
    path, task = local_task
    task["before_capture"] = "not executable configuration"
    path.write_text(json.dumps(task))
    with pytest.raises(TaskSetupError, match="INVALID_TASK_KEYS"):
        load_task(path)


@pytest.mark.parametrize(
    "route", ["direct", "compiler", "reload", "first_run", "cache", "loader"]
)
def test_no_hook_keeps_the_normal_capture_path(local_task, tmp_path, route):
    events = []
    with pytest.raises(Observed):
        invoke(route, local_task[0], None, NoModelActor(events, "unprepared"), tmp_path)
    assert len(events) == 1 and events[0][0] == "actor"
    assert events[0][1].is_closed()


def test_normal_request_guard_precedes_navigation_and_hook(
    local_task, tmp_path, monkeypatch
):
    from playwright.sync_api import Page

    from openadapt_flow.teach_scope import TeachGuard

    events = []
    guarded = []
    install = TeachGuard.install
    goto = Page.goto

    def guard(self, page):
        guarded.append(page)
        return install(self, page)

    def navigate(page, *args, **kwargs):
        assert page in guarded
        return goto(page, *args, **kwargs)

    def prepare(backend):
        assert backend.page in guarded
        backend.page.locator("#prepared").evaluate(
            "element => element.textContent = 'ready'"
        )

    monkeypatch.setattr(TeachGuard, "install", guard)
    monkeypatch.setattr(Page, "goto", navigate)
    with pytest.raises(Observed):
        invoke("loader", local_task[0], prepare, NoModelActor(events), tmp_path)
    assert len(guarded) == 1 and events[0][1] is guarded[0]
    assert guarded[0].is_closed()


def test_capture_hook_closes_only_its_context_in_a_shared_process(local_task, tmp_path):
    from openadapt_flow.backends._browser_process import _SHARED_BROWSER, shared_browser

    events = []

    def prepare(backend):
        events.append(("prepare", backend.page, backend.page.context))
        backend.page.locator("#prepared").evaluate(
            "element => element.textContent = 'ready'"
        )

    with shared_browser(headless=True):
        _, browser = _SHARED_BROWSER[True]
        sentinel = browser.new_context()
        unrelated = sentinel.new_page()
        try:
            with pytest.raises(Observed):
                invoke("loader", local_task[0], prepare, NoModelActor(events), tmp_path)
            assert events[0][1] is events[1][1] and events[0][2] is events[1][2]
            assert events[0][1].is_closed()
            assert browser.is_connected() and not unrelated.is_closed()
            assert browser.contexts == [sentinel]
        finally:
            sentinel.close()


def test_hook_is_not_part_of_task_identity_or_saved_spec(local_task):
    path, _ = local_task
    a = load_task(path)
    b = load_task(path, before_capture=lambda _: None)
    values = {"before_capture": "synthetic"}
    assert a.cache.key(a.instruction, values) == b.cache.key(b.instruction, values)
    assert "before_capture" not in b.cache.common
    compiler, _ = b.cache._compiler(b.instruction, values)
    saved = compiler.save_spec()
    assert "before_capture" not in json.loads(saved.read_text())
    assert FirstRunCompiler.load(compiler.dir).before_capture is None

    # Component control, not a full recording: normal reset/SQL observation and
    # final record checking keep preparation writes visible in the compiler's
    # existing pre-open baseline. This is distinct from Recording's baseline.
    from openadapt_flow.compile_agent.pipeline import check_run_record

    compiler._on_reset()
    baseline = compiler._baseline
    assert baseline == []
    database = json.loads(path.read_text())["verifier"]["sqlite_database"]
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO entries(title) VALUES (?)", ("outside declared record",))
    assert compiler._baseline is baseline
    after = compiler.observer.system_of_record
    check = check_run_record(baseline, after, expect=compiler.expect, values=values)
    assert not check.passed and check.created == 1
    assert any("wrong record" in reason for reason in check.reasons)


@pytest.mark.parametrize("state", ["candidate", "admitted", "cache_hit"])
def test_first_capture_hook_is_not_called_by_other_dispatch_paths(
    local_task, monkeypatch, tmp_path, state
):
    path, _ = local_task

    def forbidden(_):
        raise AssertionError("non-capture dispatch invoked preparation")

    setup = load_task(path, before_capture=forbidden)
    values = {"before_capture": "synthetic"}
    if state == "cache_hit":
        # Dispatch test only; the sentinel is not a verified/admitted program.
        sentinel = object()
        monkeypatch.setattr(SkillCache, "_server", lambda *_: tmp_path / "sentinel")
        monkeypatch.setattr(SkillCache, "_skill_run", lambda *_: sentinel)
        assert setup.cache.run(setup.instruction, values, None) is sentinel
        return
    compiler, _ = setup.cache._compiler(setup.instruction, values)
    jit = compiler._jit()
    if state == "candidate":
        monkeypatch.setattr(CompiledAgent, "candidate", lambda _: {"state": "DRAFT"})
        assert jit.run(**values).status == "needs_human_admit"
    else:
        sentinel = object()
        monkeypatch.setattr(CompiledAgent, "admitted", property(lambda _: True))
        monkeypatch.setattr(CompiledAgent, "_run_admitted", lambda *_: sentinel)
        assert jit.run(**values) is sentinel


@pytest.mark.parametrize("failure", ["hook", "session", "start", "actor"])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_native_owned_backend_closes_once_and_preserves_primary(
    tmp_path, monkeypatch, failure, cancel, cleanup_fails
):
    events = []
    primary = Cancelled() if cancel else RuntimeError(failure)

    class Backend:
        def close(self):
            events.append("close")
            if cleanup_fails:
                raise ValueError("cleanup")

    backend = Backend()

    class Session:
        def __init__(self, received, *args, **kwargs):
            assert received is backend
            events.append("session")
            if failure == "session":
                raise primary

        def start_record(self):
            events.append("start")
            if failure == "start":
                raise primary

        def halt(self):
            pass

    def hook(received):
        assert received is backend
        events.append("hook")
        if failure == "hook":
            raise primary

    monkeypatch.setattr(authoring, "AuthoringSession", Session)
    jit = compile_agent(
        NoModelActor(events),
        task=TaskSignature("native.test", "do {x}", {"x": str}),
        app=SurfaceApp(
            {"kind": "windows", "agent_url": "http://127.0.0.1:1"},
            open_backend=lambda _: backend,
        ),
        verifier=None,
        policy="clinical-write",
        cache_dir=tmp_path / "native",
        before_capture=hook,
    )
    rec = jit.recording(x="synthetic")
    if failure == "actor":
        monkeypatch.setattr(type(rec), "_finish", lambda *_: None)
    with pytest.raises(type(primary)) as caught:
        with rec:
            events.append("actor")
            raise primary
    assert caught.value is primary
    assert events.count("close") == 1 and events[-1] == "close"
    assert events[0] == "hook"
    if failure == "hook":
        assert "session" not in events
    assert ("actor" in events) is (failure == "actor")


def test_cleanup_failure_cannot_be_converted_to_a_completed_capture(
    tmp_path, monkeypatch
):
    from openadapt_flow.jit.compile_agent import Recording

    cleanup = RuntimeError("owned backend close failed")

    def close():
        raise cleanup

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        def start_record(self):
            pass

    # Only the result conversion boundary is modeled; this is not a draft,
    # recording, verification or admission qualification.
    monkeypatch.setattr(authoring, "AuthoringSession", Session)
    monkeypatch.setattr(
        Recording, "_finish", lambda *_: SimpleNamespace(status="draft")
    )
    actor = SimpleNamespace(
        dialect="generic", requires_model_key=False, run=lambda *_: None
    )
    jit = compile_agent(
        actor,
        task=TaskSignature("cleanup.test", "do {x}", {"x": str}),
        app=SurfaceApp(
            {"kind": "windows", "agent_url": "http://127.0.0.1:1"},
            open_backend=lambda _: SimpleNamespace(close=close),
        ),
        verifier=None,
        policy="clinical-write",
        cache_dir=tmp_path,
    )
    with pytest.raises(RuntimeError) as caught:
        jit.run(x="synthetic")
    assert caught.value is cleanup


@pytest.mark.parametrize("cancel", [False, True])
def test_actor_primary_survives_halt_and_all_cleanup_failures(
    tmp_path, monkeypatch, cancel
):
    events = []
    primary = Cancelled() if cancel else RuntimeError("actor primary")

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        def start_record(self):
            pass

        def halt(self):
            events.append("halt")
            raise ValueError("secondary halt failure")

    def backend_close():
        events.append("backend-close")
        raise ValueError("secondary backend failure")

    def dispatcher_close():
        events.append("dispatcher-close")
        raise Cancelled()

    monkeypatch.setattr(authoring, "AuthoringSession", Session)
    jit = compile_agent(
        SimpleNamespace(dialect="generic", requires_model_key=False),
        task=TaskSignature("primary.test", "do {x}", {"x": str}),
        app=SurfaceApp(
            {"kind": "windows", "agent_url": "http://127.0.0.1:1"},
            open_backend=lambda _: SimpleNamespace(close=backend_close),
        ),
        verifier=None,
        policy="clinical-write",
        cache_dir=tmp_path,
    )
    recording = jit.recording(x="synthetic")
    with pytest.raises(type(primary)) as caught:
        with recording:
            recording.dispatcher.on_close(dispatcher_close)
            raise primary
    assert caught.value is primary
    assert events == ["halt", "dispatcher-close", "backend-close"]
    assert recording.result is None


@pytest.mark.parametrize("failure", [None, "dispatcher", "backend", "publication"])
def test_real_candidate_publication_waits_for_cleanup_and_reload_refuses_failed_capture(
    local_task, tmp_path, monkeypatch, failure
):
    from contextlib import ExitStack

    from openadapt_flow.ir import ActionKind, Step, Workflow
    from openadapt_flow.jit import AdmissionError, lifecycle
    from openadapt_flow.jit.compile_agent import Recording, RecordingDispatcher

    setup = load_task(local_task[0], key_dir=tmp_path / "synthetic-signing")
    values = {"before_capture": "synthetic"}
    compiler, _ = setup.cache._compiler(setup.instruction, values)
    compiler.save_spec()
    jit = compiler._jit()
    events = []
    primary = RuntimeError(failure)

    def compile_staged(bundle, **kwargs):
        # Capture compilation is modeled; the actual Workflow writer,
        # parameter lift, recorder signature and candidate writer are real.
        Workflow(
            name="synthetic",
            steps=[
                Step(
                    id="step_000",
                    intent="type synthetic",
                    action=ActionKind.TYPE,
                    text="synthetic",
                )
            ],
        ).save(bundle)
        events.append("compile")
        assert jit.candidate() is None
        return {"workflow_id": "synthetic"}

    def close(stage):
        assert jit.candidate() is None
        assert (jit.bundle_dir / "workflow.json").exists()
        events.append(stage)
        if failure == stage:
            raise primary

    recording = Recording(jit, values, values, "generic")
    recording._session = SimpleNamespace(
        compile=compile_staged,
        compiled_recording_dir=None,
        recording_dir=None,
    )
    recording.computer = SimpleNamespace(actions=1, log=[])
    recording._close = lambda: close("backend")
    recording.dispatcher = RecordingDispatcher(recording)
    recording.dispatcher.on_close(lambda: close("dispatcher"))
    original_event = lifecycle._event

    def persist_event(program, kind, **fields):
        if kind == "draft":
            assert events == ["compile", "dispatcher", "backend"]
            assert jit.candidate() is None
            assert list(program.glob("candidate-pending-*.json"))
            if failure == "publication":
                raise primary
        return original_event(program, kind, **fields)

    monkeypatch.setattr(lifecycle, "_event", persist_event)

    def finish():
        with ExitStack() as stack:
            stack.push(recording)

    if failure is None:
        finish()
        # Candidate persistence uses JSON arrays for LiftedParam tuple fields.
        assert json.loads(json.dumps(recording.result.candidate)) == jit.candidate()
        assert jit.candidate()["recorder"]
        assert (
            lifecycle._recorder(jit.program_dir, jit.key_dir)
            == jit.candidate()["recorder"]
        )
        assert FirstRunCompiler.load(compiler.dir)._jit().candidate() == jit.candidate()
        assert not list(jit.program_dir.glob("candidate-pending-*.json"))
    else:
        with pytest.raises(RuntimeError) as caught:
            finish()
        assert caught.value is primary
        assert recording.result is None and jit.candidate() is None
        reloaded = FirstRunCompiler.load(compiler.dir)._jit()
        assert reloaded.candidate() is None and not reloaded.admitted
        with pytest.raises(AdmissionError, match="no draft"):
            reloaded.confirm_params(principal_id="synthetic-reviewer", accept_params=[])
        with pytest.raises(AdmissionError, match="no draft"):
            reloaded.admit(principal_id="synthetic-reviewer")
        kept = list(jit.task_dir.glob("refused-*"))
        assert len(kept) == 1 and (kept[0] / "refused.json").exists()
        assert (kept[0] / "bundle" / "workflow.json").exists()
        if failure == "publication":
            assert list(kept[0].glob("candidate-pending-*.json"))
    assert events == ["compile", "dispatcher", "backend"]


@pytest.mark.parametrize("route", ["direct", "compiler", "reload", "first_run", "cache", "loader"])
@pytest.mark.parametrize("returned", ["async_function", "async_callable", "sync_coroutine", "custom_awaitable", "suspended_coroutine", "falsey_value"])
def test_capture_requires_synchronous_none_before_recording_baseline_session_and_actor(
    local_task, tmp_path, monkeypatch, route, returned
):
    from inspect import CORO_CLOSED, CORO_SUSPENDED, getcoroutinestate

    from openadapt_flow.record_sor import SystemOfRecordObserver

    events, opened, closes, coroutines = [], [], [], []
    original_open = WebApp.open

    def forbidden_baseline(_observer):
        events.append("baseline")
        raise AssertionError("invalid preparation reached baseline")

    def forbidden_session(*args, **kwargs):
        events.append("session")
        raise AssertionError("invalid preparation reached authoring")

    def open_owned(self, **kwargs):
        backend, close = original_open(self, **kwargs)
        opened.append(backend)
        # The ordinary observer is constructed normally; this spy is installed
        # only after normal guarded opening and detects a Recording baseline read.
        monkeypatch.setattr(SystemOfRecordObserver, "system_of_record", property(forbidden_baseline))

        def close_owned_backend():
            closes.append(backend)
            close()

        return backend, close_owned_backend

    monkeypatch.setattr(WebApp, "open", open_owned)
    monkeypatch.setattr(authoring, "AuthoringSession", forbidden_session)

    async def async_prepare(backend):
        events.append("async-body")
        raise AssertionError("preparation must never be awaited or scheduled")

    class AsyncPrepare:
        async def __call__(self, backend):
            events.append("async-body")
            raise AssertionError("preparation must never be awaited or scheduled")

    class Awaitable:
        def __await__(self):
            events.append("awaited")
            yield None

        def close(self):
            events.append("foreign-close")
            raise AssertionError("a custom awaitable must not be closed")

    class Suspend:
        def __await__(self):
            yield None

    async def suspend():
        try:
            events.append("started-by-caller")
            await Suspend()
        finally:
            events.append("suspended-finally")

    def sync_prepare(backend):
        events.append("sync-hook")
        if returned == "custom_awaitable":
            return Awaitable()
        if returned == "falsey_value":
            return False
        coroutine = suspend() if returned == "suspended_coroutine" else async_prepare(backend)
        coroutines.append(coroutine)
        if returned == "suspended_coroutine":
            coroutine.send(None)  # Deliberately started by this synthetic caller.
        return coroutine

    callback = async_prepare if returned == "async_function" else AsyncPrepare() if returned == "async_callable" else sync_prepare
    try:
        with pytest.raises(TypeError, match="before_capture must return None synchronously"):
            invoke(route, local_task[0], callback, NoModelActor(events), tmp_path)
        assert len(opened) == 1 and closes == opened
        assert opened[0].page.is_closed()
        assert not {"baseline", "session", "actor", "async-body", "awaited", "foreign-close", "suspended-finally"} & set(events)
        if returned == "sync_coroutine":
            assert getcoroutinestate(coroutines[0]) == CORO_CLOSED
        if returned == "suspended_coroutine":
            assert getcoroutinestate(coroutines[0]) == CORO_SUSPENDED
            assert events == ["sync-hook", "started-by-caller"]
    finally:
        # Test ownership only: production must not close an already-started
        # coroutine, whose finally can execute user code without a model budget.
        for coroutine in coroutines:
            coroutine.close()
