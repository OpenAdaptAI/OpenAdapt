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
import math
import os
import queue
import sys
import tempfile
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
        ledger.usage_known = False
        self.last_ledger = ledger
        tools = BrowserTools(harness)
        try:
            task = f"Task: {request.instruction}"
            usage = tools.serve_while(lambda: self._run(task, tools))
        finally:
            ledger.wall_seconds = time.monotonic() - ledger.started_at
        if usage is None:
            usage_calls: Sequence[Mapping[str, Any]] = ()
        elif isinstance(usage, Mapping) and isinstance(usage.get("calls"), Sequence):
            usage_calls = usage["calls"]
        else:
            raise ValueError("framework usage must contain a calls sequence")
        if any(not isinstance(call, Mapping) for call in usage_calls):
            raise ValueError("framework usage calls must be objects")
        ledger.model_requests_started = len(usage_calls)
        for turn, call in enumerate(usage_calls, start=1):
            ledger.calls.append(_model_call(turn, call))
        ledger.usage_known = usage is not None
        ledger.model_seconds = sum(c.seconds for c in ledger.calls)
        ledger.tool_errors = tools.errors
        if tools.stopped is not None:
            raise tools.stopped
        ledger.summary = tools.summary
        ledger.finished = tools.summary is not None
        return ledger


def _model_call(turn: int, call: Mapping[str, Any]) -> ModelCall:
    def nonnegative_int(name: str) -> int:
        value = call.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"framework usage {name} must be a non-negative integer")
        return value

    seconds = call.get("seconds", 0.0)
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, (int, float))
        or not math.isfinite(float(seconds))
        or seconds < 0
    ):
        raise ValueError("framework usage seconds must be finite and non-negative")
    return ModelCall(
        turn=turn,
        input_tokens=nonnegative_int("input_tokens"),
        output_tokens=nonnegative_int("output_tokens"),
        cache_creation_input_tokens=nonnegative_int("cache_creation_input_tokens"),
        cache_read_input_tokens=nonnegative_int("cache_read_input_tokens"),
        seconds=float(seconds),
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
    model_calls: Optional[int]
    cost_usd: Optional[float]
    seconds: float
    detail: Optional[str] = None
    wire_checked: bool = False
    compile: dict[str, Any] = field(default_factory=dict)
    replay: dict[str, Any] = field(default_factory=dict)
    record_check: dict[str, Any] = field(default_factory=dict)
    usage: Optional[list[dict[str, Any]]] = None
    usage_known: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


#: Where skills are kept unless ``cache_dir`` says otherwise: per user.
DEFAULT_CACHE_DIR = "~/.openadapt/skills"
_KEY_FILE = "skill-key.json"
_STOP_FILE = "skill-stop.json"


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
    other records during the run are counted, not refused. A failed or interrupted
    skill run stays held; a later call never falls back to the full agent task.
    A proved pre-effect target drift can be handed explicitly to the existing
    one-click repair workflow with :meth:`open_repair`.
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

    def task_id(self, prompt: str, params: Mapping[str, Any]) -> str:
        """The task id this prompt compiles under.

        A ``teach_authorization`` (passed through to the compiler) must name
        exactly this task, or run 1 is refused before the agent acts.
        """

        key = self.key(prompt, params)
        digest = hashlib.sha256(
            json.dumps(key, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return f"{self.common.get('task_id') or 'skill'}-{digest}"

    def _compiler(self, prompt: str, params: Mapping[str, Any]) -> Any:
        from openadapt_flow.compile_agent import FirstRunCompiler

        key = self.key(prompt, params)
        common = {
            k: v
            for k, v in self.common.items()
            if k not in ("reset", "task_id", "review", "hold")
        }
        compiler = FirstRunCompiler(
            prompt, params=list(params), task_id=self.task_id(prompt, params), **common
        )
        return compiler, key

    def server(self, prompt: str, params: Mapping[str, Any]) -> Optional[Path]:
        """The task's compiled MCP server, when it has an admitted one."""

        compiler, key = self._compiler(prompt, params)
        from openadapt_flow.compile_agent.repair_selection import RepairSelectionError

        try:
            return self._server(compiler, key)
        except RepairSelectionError:
            return None

    def _server(self, compiler: Any, key: dict[str, Any]) -> Optional[Path]:
        if (compiler.dir / _STOP_FILE).exists() or (
            compiler.dir / _STOP_FILE
        ).is_symlink():
            return None
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
        if (compiler.dir / _STOP_FILE).exists() or (
            compiler.dir / _STOP_FILE
        ).is_symlink():
            return self._held(compiler, started)
        from openadapt_flow.compile_agent.repair_selection import RepairSelectionError

        try:
            server = self._server(compiler, key)
            if server is not None:
                return self._skill_run(compiler, server, params, started)
        except RepairSelectionError:
            compiler.dir.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(
                    compiler.dir / _STOP_FILE,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(
                        {"phase": "halted", "reason": "repair_selection_unavailable"},
                        stream,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
            return self._held(compiler, started)
        return self._first_run(compiler, key, prompt, params, agent, started)

    def _held(self, compiler: Any, started: float) -> SkillRun:
        try:
            path = compiler.dir / _STOP_FILE
            if path.is_symlink():
                raise ValueError("unreadable hold")
            stopped = json.loads(path.read_text())
            replay = stopped.get("replay", {}) if isinstance(stopped, dict) else {}
        except (OSError, ValueError):
            replay = {}
        return SkillRun(
            path="skill",
            status="CACHE_HALTED",
            verified=False,
            model_calls=0,
            cost_usd=0.0,
            usage=[],
            usage_known=True,
            seconds=round(time.monotonic() - started, 3),
            detail="A previous cached attempt is held. Reconcile it or request an "
            "explicit repair; the agent task was not rerun.",
            replay=replay if isinstance(replay, dict) else {},
        )

    def open_repair(
        self, prompt: str, params: Mapping[str, Any], *, repair_desk: Any
    ) -> dict[str, Any]:
        """Offer the existing one-click repair for a proved pre-effect drift.

        This neither runs the agent task nor clears the cache hold. The repair
        desk retains its existing screen-access, one-use demonstration,
        compiler, approval and staging rules. Unknown or possibly sent writes
        are reconciled instead of repaired.
        """
        from openadapt_flow.agent.repair import RepairError
        from openadapt_flow.ir import RunReport, Workflow
        from openadapt_flow.jit import lifecycle
        from openadapt_flow.repair.self_repair import _repairable
        from openadapt_flow.transaction import steps_requiring_reconciliation

        compiler, _key = self._compiler(prompt, params)
        try:
            stop = compiler.dir / _STOP_FILE
            if stop.is_symlink():
                raise ValueError("unreadable hold")
            stopped = json.loads(stop.read_text())
            if not isinstance(stopped, dict) or stopped.get("phase") != "halted":
                raise ValueError("no terminal cached halt")
            replay = stopped["replay"]
            run_dir = Path(replay["run_dir"]).resolve(strict=True)
            jit = compiler._jit()
            if not run_dir.is_relative_to(jit.runs_dir.resolve(strict=True)):
                raise ValueError("foreign run")
            report_path = run_dir / "report.json"
            if report_path.is_symlink():
                raise ValueError("foreign report")
            workflow = Workflow.load(jit.bundle_dir)
            admission = lifecycle.verify_admission(
                jit.bundle_dir, key_dir=compiler.key_dir
            )
            report = RunReport.model_validate_json(report_path.read_text())
            if not (
                report.workflow_name == workflow.name
                and report.bundle_content_digest == stopped["bundle_digest"]
                and report.bundle_content_digest == admission["digest"]
                and report.execution_outcome == "HALTED"
                and report.transaction_outcome == "HALTED_BEFORE_EFFECT"
                and not steps_requiring_reconciliation(report)
            ):
                raise ValueError("not a proved before-effect halt")
            found = _repairable(report, workflow)
            if isinstance(found, str) or found[1] != "replace" or report.halt is None:
                raise ValueError("not a one-step target replacement")
        except (OSError, ValueError, KeyError, TypeError, lifecycle.AdmissionError):
            raise RepairError(
                "This cached attempt has no proved one-step drift repair."
            ) from None
        return repair_desk.open(
            run_id=run_dir.name,
            workflow=workflow.name,
            step_id=found[0].id,
            halt_reason=report.halt.reason,
            bundle_dir=jit.bundle_dir,
            run_dir=run_dir,
        )

    def adopt_repair(
        self,
        prompt: str,
        params: Mapping[str, Any],
        *,
        repair_desk: Any,
        candidate_id: str,
        verified_run_dir: Union[str, Path],
        receipt: Union[str, Path],
    ) -> Path:
        """Select an already promoted, normally admitted one-step repair.

        The supplied paths name an actual normal compiled canary's report and
        independent verify-run receipt, not an approval callback. No program
        runs here. A new explicit request may use the selection only while its
        ordinary ACTIVE, admission and evidence checks still hold.
        """
        from openadapt_flow.agent.repair import RepairError
        from openadapt_flow.compile_agent.repair_selection import (
            SELECTION_FILE,
            RepairSelectionError,
            validate_selection,
        )
        from openadapt_flow.ir import Workflow

        compiler, key = self._compiler(prompt, params)
        try:
            stop = compiler.dir / _STOP_FILE
            if stop.is_symlink():
                raise ValueError("unreadable hold")
            stopped = json.loads(stop.read_text())
            if not isinstance(stopped, dict) or stopped.get("phase") != "halted":
                raise ValueError("no terminal halt")
            if json.loads((compiler.dir / _KEY_FILE).read_text()) != key:
                raise ValueError("another task")
            jit = compiler._jit()
            workflow = Workflow.load(jit.bundle_dir)
            halted_run = Path(stopped["replay"]["run_dir"]).resolve(strict=True)
            runs = jit.runs_dir.resolve(strict=True)
            if halted_run.parent != runs:
                raise ValueError("foreign halt")
            from openadapt_flow.ir import RunReport

            halted = RunReport.model_validate_json(
                (halted_run / "report.json").read_text()
            )
            if halted.halt is None or workflow.manifest is None:
                raise ValueError("no target halt")
            if workflow.manifest.content_digest != stopped["bundle_digest"]:
                raise ValueError("another bundle")
            selected = {
                "version": 1,
                "store": str(
                    Path(repair_desk.store_for(workflow.name)).resolve(strict=True)
                ),
                "candidate_id": candidate_id,
                "prior_digest": stopped["bundle_digest"],
                "halted_run": halted_run.name,
                "step_id": halted.halt.state_id,
                "verified_run": str(
                    Path(verified_run_dir).resolve(strict=True).relative_to(runs)
                ),
                "receipt": str(
                    Path(receipt)
                    .resolve(strict=True)
                    .relative_to((compiler.dir / "receipts").resolve(strict=True))
                ),
            }
            target = validate_selection(jit, selected)
            proof = RunReport.model_validate_json(
                (Path(verified_run_dir) / "report.json").read_text()
            )
            if {k: str(v) for k, v in proof.params.items()} != {
                k: str(v) for k, v in params.items()
            }:
                raise ValueError("proof used other values")
            # A fresh independent read must still find the synthetic canary's
            # unique task record and all input values. It is a state check,
            # not a new causal write claim; the normal receipt is separate.
            rows = compiler.observer.system_of_record
            values = set(map(str, params.values()))
            matching = (
                []
                if rows is None
                else [
                    row
                    for row in rows
                    if all(
                        str(row.get(k)) == str(v)
                        for k, v in self.common["expect"].items()
                    )
                    and values.issubset({str(v) for v in row.values() if v is not None})
                ]
            )
            if len(matching) != 1:
                raise ValueError("fresh record check failed")
            archive = compiler.dir / f"skill-stop-repaired-{candidate_id}.json"
            if archive.exists() or archive.is_symlink():
                raise ValueError("repair already consumed")
            descriptor, name = tempfile.mkstemp(
                dir=compiler.dir, prefix=".skill-repair-"
            )
            try:
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(selected, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, compiler.dir / SELECTION_FILE)
            finally:
                Path(name).unlink(missing_ok=True)
            # Retain the exact old halt; adoption does not erase its evidence.
            stop.rename(archive)
            return target
        except (OSError, ValueError, KeyError, TypeError, RepairSelectionError):
            raise RepairError(
                "This promoted repair has no matching verified cache handoff."
            ) from None

    def complete_repair(
        self, prompt: str, params: Mapping[str, Any], *, repair_desk: Any,
        candidate_id: str, canary_params: Sequence[Mapping[str, Any]],
    ) -> Any:
        """Run an approved repair on explicit synthetic canaries, then select it."""
        from openadapt_flow.compile_agent.repair_bridge import complete_approved_repair

        return complete_approved_repair(
            self, prompt, params, repair_desk=repair_desk,
            candidate_id=candidate_id, canary_params=canary_params,
        )

    def _skill_run(
        self, compiler: Any, server: Path, params: Mapping[str, Any], started: float
    ) -> SkillRun:
        from openadapt_flow.ir import Workflow

        stop = compiler.dir / _STOP_FILE
        workflow = Workflow.load(compiler._jit().bundle_dir)
        if workflow.manifest is None:
            raise ValueError("The compiled bundle has no verified manifest.")
        digest = workflow.manifest.content_digest
        try:
            descriptor = os.open(stop, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return self._held(compiler, started)
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"phase": "running", "bundle_digest": digest}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        before = compiler.observer.system_of_record
        if before is None:
            reply = {
                "status": "BASELINE_UNAVAILABLE",
                "verified": False,
                "model_calls": 0,
                "detail": "The independent baseline is unavailable; nothing ran.",
            }
            after = None
        else:
            reply = asyncio.run(_call_skill(self.python, server, dict(params)))
            after = compiler.observer.system_of_record
        check = task_record_check(
            before,
            after,
            expect=self.common["expect"],
            values={k: str(v) for k, v in params.items()},
        )
        verified = (
            reply.get("verified") is True
            and type(reply.get("model_calls")) is int
            and reply["model_calls"] == 0
            and reply.get("receipt_outcome") == "VERIFIED"
            and check.passed
        )
        status = str(reply.get("status"))
        detail = reply.get("detail") or reply.get("halt_reason")
        if reply.get("verified") is True and reply.get("receipt_outcome") != "VERIFIED":
            status, detail = (
                "RECEIPT_NOT_VERIFIED",
                "The normal verify-run receipt is not VERIFIED.",
            )
        elif reply.get("verified") is True and (
            type(reply.get("model_calls")) is not int or reply["model_calls"] != 0
        ):
            status, detail = (
                "MODEL_CALLS_NOT_ZERO",
                "The compiled reply did not prove zero model calls.",
            )
        if bool(reply.get("verified")) and not check.passed:
            status = "RECORD_CHECK_FAILED"
            detail = "; ".join(check.reasons)
        if verified:
            stop.unlink()
        else:
            # Keep the original cache and halt evidence. A lost reply, drift,
            # or unverified write must never turn into an automatic agent miss.
            descriptor, name = tempfile.mkstemp(dir=compiler.dir, prefix=".skill-stop-")
            try:
                with os.fdopen(descriptor, "w") as stream:
                    json.dump(
                        {"phase": "halted", "bundle_digest": digest, "replay": reply},
                        stream,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, stop)
            finally:
                Path(name).unlink(missing_ok=True)
        model_calls, cost_usd, usage, usage_known = _skill_usage(reply)
        return SkillRun(
            path="skill",
            status=status,
            verified=verified,
            model_calls=model_calls,
            cost_usd=cost_usd,
            seconds=round(time.monotonic() - started, 3),
            detail=detail,
            replay=reply,
            record_check=check.to_dict(),
            usage=usage,
            usage_known=usage_known,
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
        usage_known = bool(agent_run.get("usage_known", False))
        model_calls = agent_run.get("model_calls")
        if isinstance(model_calls, bool) or not isinstance(model_calls, int):
            model_calls = None
            usage_known = False
        raw_cost = agent_run.get("cost_usd")
        if (
            isinstance(raw_cost, bool)
            or not isinstance(raw_cost, (int, float))
            or not math.isfinite(float(raw_cost))
            or raw_cost < 0
        ):
            cost_usd: Optional[float] = None
            usage_known = False
        else:
            cost_usd = float(raw_cost) if usage_known else None
        usage = (
            [{"calls": model_calls, "usd": cost_usd}]
            if model_calls is not None
            else None
        )
        return SkillRun(
            path="agent",
            status=result.status,
            # Run 1's own write, read back by the record check.
            verified=bool(check.get("passed")),
            model_calls=model_calls,
            cost_usd=cost_usd,
            seconds=round(time.monotonic() - started, 3),
            detail=result.detail,
            wire_checked=self.common.get("write_adapter") is not None,
            compile=result.to_dict(),
            record_check=check,
            usage=usage,
            usage_known=usage_known,
        )


def _skill_usage(
    reply: Mapping[str, Any],
) -> tuple[Optional[int], Optional[float], Optional[list[dict[str, Any]]], bool]:
    """Read explicit skill/fallback model usage without inferring from path."""

    raw_calls = reply.get("model_calls")
    model_calls = (
        raw_calls
        if isinstance(raw_calls, int)
        and not isinstance(raw_calls, bool)
        and raw_calls >= 0
        else None
    )
    usage_known = reply.get("usage_known") is True
    raw_usage = reply.get("usage")
    if not isinstance(raw_usage, list):
        return model_calls, None, None, False

    rows: list[dict[str, Any]] = []
    total_calls = 0
    total_cost = 0.0
    all_costs_known = usage_known
    for row in raw_usage:
        if not isinstance(row, Mapping):
            return model_calls, None, None, False
        count = row.get("calls")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return model_calls, None, None, False
        total_calls += count
        raw_cost = row.get("usd")
        if (
            isinstance(raw_cost, bool)
            or not isinstance(raw_cost, (int, float))
            or not math.isfinite(float(raw_cost))
            or raw_cost < 0
        ):
            all_costs_known = False
            cost: Optional[float] = None
        else:
            cost = float(raw_cost)
            total_cost += cost
        rows.append({"calls": count, "usd": cost})

    if model_calls is not None and model_calls != total_calls:
        return model_calls, None, rows, False
    model_calls = total_calls
    if not usage_known or not all_costs_known:
        return model_calls, None, rows, False
    if model_calls == 0 and total_cost == 0:
        return 0, 0.0, [], True
    return model_calls, total_cost, rows, True


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
