"""Skill cache: an agent's first verified browser run becomes a program, and
the next matching task runs that program through MCP instead of the agent.

The agent, in any framework, does the task with :class:`BrowserTools`
(``observe``, ``click``, ``type_text``, ``press_key``, ``scroll``,
``finish``). They are
compile-agent's harness: the structural listing first, and a check that the
right record is on screen before every save. Run 1 goes through
:func:`~openadapt_flow.compile_agent.compile_first_run`:

1. The system of record must show exactly the record the task named. If it
   doesn't, nothing is compiled.
2. ``offer`` is shown the proposed record checks and returns the name of the
   person who confirms them, or None to leave the run as a draft.
3. Canary runs with new values, then admission by a registered admitter.
4. The program is emitted as an MCP server with one tool.

Every later call with the same task, on the same record, runs that tool: no
model calls, each save read back twice (see :class:`SkillCache`), and a
verify-run receipt. ::

    cache = SkillCache(url=..., verify="verify.yaml", expect=..., on_screen=...,
                       offer=ask_a_person, admitter=..., canary_params=[...])
    agent = FrameworkAgent(run_my_agent, principal_id="agent:intake")
    first = cache.run("Register {fname} {lname}", {"fname": ..., ...}, agent)
    later = cache.run("Register {fname} {lname}", {"fname": ..., ...}, agent)
    assert later.path == "skill" and later.model_calls == 0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Optional, Sequence, Union

from openadapt_flow.compile_agent.agents import AgentLedger, ModelCall
from openadapt_flow.compile_agent.harness import GateRefused, StaleRef
from openadapt_flow.teach_scope import ScopeViolation, UnsupportedAction

__all__ = [
    "BrowserTools",
    "FrameworkAgent",
    "SkillCache",
    "SkillRun",
    "TaskRecordCheck",
    "Usage",
    "task_record_check",
]

#: What a framework run reports it spent. ``calls`` holds one mapping per
#: model call with ``input_tokens``, ``output_tokens`` and optionally
#: ``cache_read_input_tokens``, ``cache_creation_input_tokens``, ``seconds``.
Usage = Mapping[str, Any]
_SCROLL_PX = 600


class BrowserTools:
    """compile-agent's harness as plain functions an agent framework wraps.

    Each action returns the page's new structural listing. A save on the
    wrong record, or an action outside the task's scope, ends the run: the
    tool says so, every later call is refused, and the run is not compiled.

    The browser belongs to the thread that made these tools. Frameworks that
    run tools on worker threads (LangGraph's ``ToolNode``, the OpenAI Agents
    SDK) are served through :meth:`serve_while`, which runs each action on
    the browser's thread.
    """

    def __init__(self, harness: Any) -> None:
        self._harness = harness
        self.stopped: Optional[BaseException] = None
        self.summary: Optional[str] = None
        self.errors = 0
        self._owner = threading.get_ident()
        self._jobs: "queue.Queue[tuple[Callable[[], str], Future]]" = queue.Queue()

    def serve_while(self, work: Callable[[], Any]) -> Any:
        """Run ``work`` on a worker thread; do its browser actions here."""

        done: Future = Future()

        def target() -> None:
            try:
                done.set_result(work())
            except BaseException as exc:  # handed back to the caller
                done.set_exception(exc)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        while not done.done() or not self._jobs.empty():
            try:
                job, reply = self._jobs.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                reply.set_result(job())
            except BaseException as exc:
                reply.set_exception(exc)
        thread.join()
        return done.result()

    def observe(self) -> str:
        """Read the current page: one line per heading, text, row or control.
        Controls carry refs such as e3."""

        return self._do(lambda: None)

    def click(self, ref: str) -> str:
        """Click the control with this ref from the latest listing."""

        return self._do(lambda: self._harness.click(ref))

    def type_text(self, text: str, ref: str = "") -> str:
        """Type text. With a ref, click that field first; without one, type into
        the focused field. Typing appends to what the field holds."""

        return self._do(lambda: self._harness.type_text(text, ref or None))

    def press_key(self, key: str) -> str:
        """Press one key, such as Enter, Tab or Escape."""

        return self._do(lambda: self._harness.press(key))

    def scroll(self, direction: str = "down") -> str:
        """Scroll the page up or down by most of a screen. The listing shows
        only what is on screen."""

        if direction not in ("up", "down"):
            return "Not done: direction is up or down"
        dy = _SCROLL_PX if direction == "down" else -_SCROLL_PX
        return self._do(lambda: self._harness.act({"kind": "scroll", "dy": dy}))

    def finish(self, summary: str) -> str:
        """End the task, once, when it is done or cannot be done."""

        self.summary = summary
        return "Finished."

    def functions(self) -> list[Callable[..., str]]:
        """The tools, for ``function_tool``, ``@tool`` or ``StructuredTool``."""

        return [
            self.observe,
            self.click,
            self.type_text,
            self.press_key,
            self.scroll,
            self.finish,
        ]

    def _do(self, action: Callable[[], Any]) -> str:
        if threading.get_ident() == self._owner:
            return self._act(action)
        reply: Future = Future()
        self._jobs.put((lambda: self._act(action), reply))
        return reply.result()

    def _act(self, action: Callable[[], Any]) -> str:
        if self.stopped is not None:
            return "Stopped: the run has ended. Call finish."
        if self.summary is not None:
            return "The task is finished."
        try:
            action()
        except (GateRefused, ScopeViolation) as exc:
            self.stopped = exc
            return f"Stopped: {exc}. Call finish."
        except (StaleRef, UnsupportedAction, LookupError, ValueError) as exc:
            # Nothing was performed; the model can try another action.
            self.errors += 1
            return f"Not done: {exc}"
        return self._harness.observe().text


class FrameworkAgent:
    """Any framework's agent as compile-agent's run-1 agent.

    ``run(task, tools)`` runs the agent once on ``task`` (the instruction with
    its values) with ``tools`` (:class:`BrowserTools`), and returns what it
    spent (:data:`Usage`) or None. ``model`` and ``prices`` (USD per million
    tokens: input, output, cache_write, cache_read) price the run.
    """

    dialect: ClassVar[str] = "generic"
    requires_model_key: ClassVar[bool] = True

    def __init__(
        self,
        run: Callable[[str, BrowserTools], Optional[Usage]],
        *,
        principal_id: Optional[str] = None,
        model: Optional[str] = None,
        prices: Optional[Mapping[str, float]] = None,
    ) -> None:
        self._run = run
        self.principal_id = principal_id
        self.model = model
        self.prices = dict(prices) if prices else None
        self.last_ledger: Optional[AgentLedger] = None

    def drive(self, request: Any, harness: Any) -> AgentLedger:
        ledger = AgentLedger(agent="framework", model=self.model, prices=self.prices)
        self.last_ledger = ledger
        tools = BrowserTools(harness)
        try:
            task = f"Task: {request.instruction}"
            usage = tools.serve_while(lambda: self._run(task, tools)) or {}
        finally:
            ledger.wall_seconds = time.monotonic() - ledger.started_at
        for turn, call in enumerate(usage.get("calls") or (), start=1):
            ledger.calls.append(_model_call(turn, call))
        ledger.model_seconds = sum(c.seconds for c in ledger.calls)
        ledger.tool_errors = tools.errors
        if tools.stopped is not None:
            raise tools.stopped
        ledger.summary = tools.summary
        ledger.finished = tools.summary is not None
        return ledger


def _model_call(turn: int, call: Mapping[str, Any]) -> ModelCall:
    return ModelCall(
        turn=turn,
        input_tokens=int(call.get("input_tokens") or 0),
        output_tokens=int(call.get("output_tokens") or 0),
        cache_creation_input_tokens=int(call.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(call.get("cache_read_input_tokens") or 0),
        seconds=float(call.get("seconds") or 0.0),
        stop_reason=call.get("stop_reason"),
        images_in_request=0,
    )


@dataclass
class SkillRun:
    """One task through the cache.

    ``path`` is ``agent`` (run 1, compiled when verified and admitted) or
    ``skill`` (the compiled program through MCP). ``verified`` is true only
    when the system of record shows the record this task names, with its
    values. ``wire_checked`` says whether the run's writes were also checked
    before they left (compile-agent's write adapter): run 1 only, and only
    with ``write_adapter``.
    """

    path: str
    status: str
    verified: bool
    model_calls: int
    cost_usd: float
    seconds: float
    detail: Optional[str] = None
    wire_checked: bool = False
    compile: dict[str, Any] = field(default_factory=dict)
    replay: dict[str, Any] = field(default_factory=dict)
    record_check: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


#: Where skills are kept unless ``cache_dir`` says otherwise: per user.
DEFAULT_CACHE_DIR = "~/.openadapt/skills"
_KEY_FILE = "skill-key.json"


class SkillCache:
    """Compile a framework agent's first verified run; call the skill after.

    The arguments are :func:`~openadapt_flow.compile_agent.compile_first_run`'s:
    ``url``, ``verify`` (the ``verify.yaml`` that reads the system of record),
    ``expect`` and ``on_screen`` (the record the task names), ``admitter`` or
    ``person``, ``canary_params``, ``cache_dir``, ``key_dir``, ``model_key``
    (the customer's key the framework's model runs on) and ``reset`` (tests
    only). ``offer`` is ``confirm_checks``: it gets the proposed checks and
    returns who confirmed them, or None to keep the run a draft.

    A skill is reused only for the same task: the same instruction,
    parameter names, ``expect``, ``on_screen``, ``url``, ``verify.yaml``
    contents, policy and ``environment`` (name the app's version or
    deployment there, so a changed app compiles again). Every skill run is
    checked twice in the system of record: by the program's own confirmed
    checks (its receipt) and by :func:`task_record_check`: exactly one new
    record of this task's record (matching ``expect``), with this task's
    values, and nothing else changed on that record. Other users' writes to
    other records during the run are counted, not refused. A skill run that fails either is evicted, and the next run of
    the task goes to the agent again.
    """

    def __init__(
        self,
        *,
        url: str,
        verify: Union[str, Path],
        expect: Mapping[str, Any],
        on_screen: Sequence[str],
        offer: Callable[[dict[str, Any]], Optional[str]],
        canary_params: Sequence[Mapping[str, Any]],
        admitter: Any = None,
        person: Optional[str] = None,
        drop_fields: Sequence[str] = (),
        cache_dir: Union[str, Path] = DEFAULT_CACHE_DIR,
        key_dir: Optional[Union[str, Path]] = None,
        model_key: Any = None,
        reset: Optional[Callable[[], None]] = None,
        environment: str = "",
        python: str = sys.executable,
        **compile_options: Any,
    ) -> None:
        self.common: dict[str, Any] = dict(
            url=url,
            verify=verify,
            expect=dict(expect),
            on_screen=list(on_screen),
            cache_dir=Path(cache_dir).expanduser().resolve(),
            key_dir=key_dir,
            reset=reset,
            **compile_options,
        )
        self.environment = environment
        self.offer = offer
        self.canary_params = [dict(row) for row in canary_params]
        self.admitter = admitter
        self.person = person
        self.drop_fields = list(drop_fields)
        self.model_key = model_key
        self.python = python

    # -- which skill ---------------------------------------------------------

    def key(self, prompt: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Everything that names the task and its record, as stored with it."""

        verify = Path(self.common["verify"])
        return {
            "instruction": prompt,
            "params": sorted(params),
            "expect": {str(k): str(v) for k, v in self.common["expect"].items()},
            "on_screen": [str(s) for s in self.common["on_screen"]],
            "url": str(self.common["url"]),
            "verify_sha256": hashlib.sha256(verify.read_bytes()).hexdigest(),
            "policy": str(self.common.get("policy", "clinical-write")),
            "environment": self.environment,
        }

    def _compiler(self, prompt: str, params: Mapping[str, Any]) -> Any:
        from openadapt_flow.compile_agent import FirstRunCompiler

        key = self.key(prompt, params)
        digest = hashlib.sha256(
            json.dumps(key, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        base = self.common.get("task_id") or "skill"
        common = {k: v for k, v in self.common.items() if k not in ("reset", "task_id")}
        compiler = FirstRunCompiler(
            prompt, params=list(params), task_id=f"{base}-{digest}", **common
        )
        return compiler, key

    def server(self, prompt: str, params: Mapping[str, Any]) -> Optional[Path]:
        """The task's compiled MCP server, when it has an admitted one."""

        compiler, key = self._compiler(prompt, params)
        return self._server(compiler, key)

    def _server(self, compiler: Any, key: dict[str, Any]) -> Optional[Path]:
        stored = compiler.dir / _KEY_FILE
        if not compiler._jit().admitted or not stored.is_file():
            return None
        if json.loads(stored.read_text()) != key:
            # Never serve a program compiled for another task or record.
            return None
        emitted = compiler.out_dir / "emitted.json"
        if not emitted.is_file():
            compiler.emit()
        return Path(json.loads(emitted.read_text())["mcp_server"])

    def evict(self, prompt: str, params: Mapping[str, Any]) -> Optional[Path]:
        """Set the task's skill aside (kept as evidence); None if it had none."""

        compiler, _key = self._compiler(prompt, params)
        return _set_aside(compiler)

    # -- a task ----------------------------------------------------------------

    def run(self, prompt: str, params: Mapping[str, Any], agent: Any) -> SkillRun:
        started = time.monotonic()
        compiler, key = self._compiler(prompt, params)
        server = self._server(compiler, key)
        if server is not None:
            return self._skill_run(compiler, server, params, started)
        return self._first_run(compiler, key, prompt, params, agent, started)

    def _skill_run(
        self, compiler: Any, server: Path, params: Mapping[str, Any], started: float
    ) -> SkillRun:
        before = compiler.observer.system_of_record
        reply = asyncio.run(_call_skill(self.python, server, dict(params)))
        after = compiler.observer.system_of_record
        check = task_record_check(
            before,
            after,
            expect=self.common["expect"],
            values={k: str(v) for k, v in params.items()},
        )
        verified = bool(reply.get("verified")) and check.passed
        status = str(reply.get("status"))
        detail = reply.get("detail") or reply.get("halt_reason")
        if bool(reply.get("verified")) and not check.passed:
            status = "RECORD_CHECK_FAILED"
            detail = "; ".join(check.reasons)
        if not verified:
            # A halted or refuted skill is not served again: the next run of
            # this task goes to the agent and compiles afresh.
            _set_aside(compiler)
        return SkillRun(
            path="skill",
            status=status,
            verified=verified,
            model_calls=int(reply.get("model_calls") or 0),
            cost_usd=0.0,
            seconds=round(time.monotonic() - started, 3),
            detail=detail,
            replay=reply,
            record_check=check.to_dict(),
        )

    def _first_run(
        self,
        compiler: Any,
        key: dict[str, Any],
        prompt: str,
        params: Mapping[str, Any],
        agent: Any,
        started: float,
    ) -> SkillRun:
        from openadapt_flow.compile_agent import compile_first_run

        common = dict(self.common)
        common["task_id"] = compiler.task.task_id
        result = compile_first_run(
            prompt,
            params=dict(params),
            agent=agent,
            confirm_checks=self.offer,
            drop_fields=self.drop_fields,
            admitter=self.admitter,
            person=self.person,
            canary_params=self.canary_params,
            model_key=self.model_key,
            **common,
        )
        if result.admitted:
            (compiler.dir / _KEY_FILE).write_text(json.dumps(key, sort_keys=True))
        agent_run = result.run1.get("agent") or {}
        check = result.record_check or {}
        return SkillRun(
            path="agent",
            status=result.status,
            # Run 1's own write, read back by the record check.
            verified=bool(check.get("passed")),
            model_calls=int(agent_run.get("model_calls") or 0),
            cost_usd=float(agent_run.get("cost_usd") or 0.0),
            seconds=round(time.monotonic() - started, 3),
            detail=result.detail,
            wire_checked=self.common.get("write_adapter") is not None,
            compile=result.to_dict(),
            record_check=check,
        )


@dataclass
class TaskRecordCheck:
    """This task's write in the system of record, apart from other writers."""

    passed: bool
    reasons: list[str]
    #: Records of this task's record (they match ``expect``).
    created: int
    changed: int
    removed: int
    #: Writes to other records during the run (other users, other tasks).
    outside_run: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def task_record_check(
    before: Optional[list[dict[str, Any]]],
    after: Optional[list[dict[str, Any]]],
    *,
    expect: Mapping[str, Any],
    values: Mapping[str, str],
) -> TaskRecordCheck:
    """Whether the run wrote exactly this task's record, with its values.

    Only records that match every ``expect`` field belong to the task. It
    passes when exactly one such record appeared, it holds every value, and
    no existing record of the task's record changed or disappeared. Records
    of other records that appear or change during the run are another
    writer's (a shared system has other users) and are counted, not judged:
    a write the program itself made elsewhere is the program's own checks'
    job (the receipt's frame), and it leaves this task's record unwritten.
    """

    from openadapt_flow.record_sor import diff_snapshots

    if before is None or after is None:
        return TaskRecordCheck(
            False, ["the system of record could not be read"], 0, 0, 0
        )

    def mine(record: Optional[Mapping[str, Any]]) -> bool:
        return record is not None and all(
            str(record.get(name)) == str(wanted) for name, wanted in expect.items()
        )

    diff = diff_snapshots(before, after)
    created = [r for r in diff.created if mine(r)]
    changed = [c for c in diff.changed if mine(c.before) or mine(c.after)]
    removed = [r for r in diff.removed if mine(r)]
    reasons: list[str] = []
    if len(created) != 1:
        reasons.append(
            f"{len(created)} new records of the task's record appeared; the "
            "task makes one (wrong record, or not saved)"
        )
    if changed:
        reasons.append(f"{len(changed)} existing records of the task's record changed")
    if removed:
        reasons.append(f"{len(removed)} records of the task's record disappeared")
    if len(created) == 1:
        held = {str(v) for v in created[0].values() if v is not None}
        reasons += [
            f"input {name!r} is not in the new record"
            for name, value in values.items()
            if value not in held
        ]
    return TaskRecordCheck(
        passed=not reasons,
        reasons=reasons,
        created=len(created),
        changed=len(changed),
        removed=len(removed),
        outside_run={
            "created": len(diff.created) - len(created),
            "changed": len(diff.changed) - len(changed),
            "removed": len(diff.removed) - len(removed),
        },
    )


def _set_aside(compiler: Any) -> Optional[Path]:
    if not compiler.dir.exists():
        return None
    target = compiler.cache_dir / "evicted" / f"{compiler.dir.name}-{time.time_ns()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    compiler.dir.rename(target)
    return target


async def _call_skill(python: str, server: Path, params: dict[str, Any]) -> dict:
    """Call the compiled task's one tool over MCP (stdio), as a client would."""

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    target = StdioServerParameters(command=python, args=[str(server)])
    async with stdio_client(target) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            if len(tools) != 1:
                raise RuntimeError("a compiled skill's server has exactly one tool")
            result = await session.call_tool(tools[0].name, params)
    # mcp 2 names these fields in snake case, mcp 1 in camel case.
    if getattr(result, "is_error", None) or getattr(result, "isError", None):
        text = " ".join(getattr(c, "text", "") for c in result.content)
        return {"status": "TOOL_ERROR", "verified": False, "detail": text[:500]}
    payload = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if payload:
        return dict(payload.get("result", payload))
    return json.loads(getattr(result.content[0], "text", "{}"))
