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

Every later call with the same task (``prompt`` and ``task_id``) runs that
tool: no model calls, each save read back, and a verify-run receipt. ::

    cache = SkillCache(url=..., verify="verify.yaml", expect=..., on_screen=...,
                       offer=ask_a_person, admitter=..., canary_params=[...])
    agent = FrameworkAgent(run_my_agent, principal_id="agent:intake")
    first = cache.run("Register {fname} {lname}", {"fname": ..., ...}, agent)
    later = cache.run("Register {fname} {lname}", {"fname": ..., ...}, agent)
    assert later.path == "skill" and later.model_calls == 0
"""

from __future__ import annotations

import asyncio
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

__all__ = ["BrowserTools", "FrameworkAgent", "SkillCache", "SkillRun", "Usage"]

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
    when the system of record shows the task's record.
    """

    path: str
    status: str
    verified: bool
    model_calls: int
    cost_usd: float
    seconds: float
    detail: Optional[str] = None
    compile: dict[str, Any] = field(default_factory=dict)
    replay: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class SkillCache:
    """Compile a framework agent's first verified run; call the skill after.

    The arguments are :func:`~openadapt_flow.compile_agent.compile_first_run`'s:
    ``url``, ``verify`` (the ``verify.yaml`` that reads the system of record),
    ``expect`` and ``on_screen`` (the record the task names), ``admitter`` or
    ``person``, ``canary_params``, ``cache_dir``, ``key_dir``, ``model_key``
    (the customer's key the framework's model runs on) and ``reset`` (tests
    only). ``offer`` is ``confirm_checks``: it gets the proposed checks and
    returns who confirmed them, or None to keep the run a draft.
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
        cache_dir: Union[str, Path] = ".openadapt-skills",
        key_dir: Optional[Union[str, Path]] = None,
        model_key: Any = None,
        reset: Optional[Callable[[], None]] = None,
        python: str = sys.executable,
        **compile_options: Any,
    ) -> None:
        self.common: dict[str, Any] = dict(
            url=url,
            verify=verify,
            expect=expect,
            on_screen=list(on_screen),
            cache_dir=cache_dir,
            key_dir=key_dir,
            reset=reset,
            **compile_options,
        )
        self.offer = offer
        self.canary_params = [dict(row) for row in canary_params]
        self.admitter = admitter
        self.person = person
        self.drop_fields = list(drop_fields)
        self.model_key = model_key
        self.python = python

    def _compiler(self, prompt: str, params: Mapping[str, Any]) -> Any:
        from openadapt_flow.compile_agent import FirstRunCompiler

        common = {k: v for k, v in self.common.items() if k != "reset"}
        return FirstRunCompiler(prompt, params=list(params), **common)

    def server(self, prompt: str, params: Mapping[str, Any]) -> Optional[Path]:
        """The task's compiled MCP server, when it has an admitted one."""

        compiler = self._compiler(prompt, params)
        if not compiler._jit().admitted:
            return None
        emitted = compiler.out_dir / "emitted.json"
        if not emitted.is_file():
            compiler.emit()
        return Path(json.loads(emitted.read_text())["mcp_server"])

    def run(self, prompt: str, params: Mapping[str, Any], agent: Any) -> SkillRun:
        started = time.monotonic()
        server = self.server(prompt, params)
        if server is not None:
            reply = asyncio.run(_call_skill(self.python, server, dict(params)))
            return SkillRun(
                path="skill",
                status=str(reply.get("status")),
                verified=bool(reply.get("verified")),
                model_calls=int(reply.get("model_calls") or 0),
                cost_usd=0.0,
                seconds=round(time.monotonic() - started, 3),
                detail=reply.get("detail") or reply.get("halt_reason"),
                replay=reply,
            )
        return self._first_run(prompt, params, agent, started)

    def _first_run(
        self, prompt: str, params: Mapping[str, Any], agent: Any, started: float
    ) -> SkillRun:
        from openadapt_flow.compile_agent import compile_first_run

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
            **self.common,
        )
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
            compile=result.to_dict(),
        )


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
