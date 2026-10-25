"""Run 1 driven from outside: an agent that sends one action per call.

:class:`FirstRunCompiler` records an agent that owns its loop
(``drive(request, harness)``). An agent connected over MCP doesn't: it makes
one tool call per action. :class:`DrivenRun` turns those calls into an agent.
It runs :meth:`FirstRunCompiler.record` on a thread of its own, where the
browser or native backend lives, and hands each call's action to the harness
on that thread. When the caller finishes, the run is compiled and checked
exactly as for any other agent: the same save gate, the same record check,
the same draft.

The caller decides nothing about the gate. A gated action that a person must
approve is approved by :meth:`DrivenRun.act` only when the caller says a
person did (``held_by_person``); the MCP server sets that only after the
person answered the client's own form.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from openadapt_flow.compile_agent.agents import AgentLedger
from openadapt_flow.compile_agent.harness import GateRefused, GateRequest, StaleRef
from openadapt_flow.jit.adapters import UnsupportedAction
from openadapt_flow.jit.compile_agent import TaskRequest
from openadapt_flow.teach_scope import ScopeViolation

__all__ = ["DrivenAgent", "DrivenRun", "RunEnded", "ACTIONS", "SCRIPT_SURFACES"]

#: Actions a caller sends. ``ref`` names a control in the latest observation.
ACTIONS = ("click", "type", "press", "hotkey")

#: A code-action script sent with ``run`` (``teach_run``): web apps only.
SCRIPT_SURFACES = ("web",)

_STOP = object()


class RunEnded(RuntimeError):
    """Run 1 is over; no more actions go to it."""


@dataclass
class _Command:
    kind: str
    payload: Mapping[str, Any]
    future: Future


class DrivenAgent:
    """The agent :class:`FirstRunCompiler` records: actions arrive by queue.

    ``principal_id`` is the identity the recording is signed with. It must be
    the registered agent that may later admit the draft, so that admitting it
    is self-admission and needs a policy that allows it.
    """

    dialect = "generic"
    requires_model_key = False
    #: Drives the surface harness too (native windows, RDP, Citrix).
    drives_surfaces = True

    def __init__(self, principal_id: Optional[str] = None) -> None:
        self.principal_id = principal_id
        self.request: Optional[TaskRequest] = None
        self.harness: Any = None
        self.ready = threading.Event()
        self._inbox: "queue.Queue[Any]" = queue.Queue()
        self._approved: Optional[str] = None
        self.ledger = AgentLedger(agent="driven")
        #: This run, and how many actions it has performed: a person's
        #: approval is bound to both (see :meth:`binding`).
        self.run_id = uuid.uuid4().hex
        self.performed = 0
        self._tool: Any = None
        #: The code-action trace, saved with run 1 by the pipeline.
        self.last_trace: Any = None

    # -- called by the gate, on the run thread -----------------------------------

    def hold(self, request: GateRequest) -> bool:
        """True only for the action a person approved, once."""

        approved, self._approved = self._approved, None
        return approved is not None and approved == request.control

    # -- the run thread ------------------------------------------------------------

    def drive(self, request: TaskRequest, harness: Any) -> AgentLedger:
        self.request = request
        self.harness = harness
        self.ready.set()
        while True:
            command = self._inbox.get()
            if command is _STOP:
                raise RunEnded("run 1 was stopped before it finished")
            assert isinstance(command, _Command)
            if command.kind == "finish":
                command.future.set_result({"finished": True})
                break
            try:
                command.future.set_result(self._handle(command))
            except ScopeViolation as exc:
                # Outside the task's declared scope: run 1 ends with no draft,
                # like a refused save.
                command.future.set_result({"refused": str(exc)})
                raise
            except (StaleRef, UnsupportedAction, LookupError, ValueError) as exc:
                # Nothing was performed: report it and keep the run going.
                command.future.set_result(
                    {"error": type(exc).__name__, "detail": str(exc)}
                )
            except GateRefused as exc:
                command.future.set_result({"refused": str(exc)})
                raise
            except BaseException as exc:
                command.future.set_exception(exc)
                raise
        self.ledger.finished = True
        self.ledger.wall_seconds = time.monotonic() - self.ledger.started_at
        return self.ledger

    def _code_tool(self) -> Any:
        """The code-action tool over this run's harness (web only)."""

        if self._tool is None:
            from openadapt_flow.compile_agent.code_action import CodeActionTool, _watch
            from openadapt_flow.compile_agent.harness import Harness

            if not isinstance(self.harness, Harness):
                raise ValueError(
                    "scripts run on web apps only; on this app use teach_act"
                )
            self._tool = CodeActionTool(
                self.harness, watch=_watch(self.request, self.harness)
            )
            self.last_trace = self._tool.trace
        return self._tool

    @staticmethod
    def _first_line(script: str) -> str:
        return next(
            (
                ln.strip()
                for ln in str(script).splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ),
            "",
        )

    def script_binding(self, script: str, commit: Optional[str]) -> str:
        """What a person's approval of a script covers: this run, the actions
        performed so far, the script's first line, and the save it previews."""

        actions = self.harness.stats.actions if self.harness is not None else 0
        return json.dumps([self.run_id, actions, self._first_line(script), commit])

    def _run_script(self, command: _Command) -> dict[str, Any]:
        tool = self._code_tool()
        script = str(command.payload.get("script") or "")
        if command.kind == "run_preview":
            commit = tool.first_commit(script)
            return {
                "commit": commit,
                "binding": self.script_binding(script, commit) if commit else None,
                "action": tool._out(self._first_line(script)[:200]),
            }
        held = command.payload.get("_held_by_person")
        approved_commit: Optional[str] = None
        if held:
            # The approval covers the save the person was shown, on the app as
            # it is now: read it again and require the same.
            approved_commit = tool.first_commit(script)
            if approved_commit is None or held != self.script_binding(
                script, approved_commit
            ):
                raise ValueError(
                    "the app or the run changed after the person approved this "
                    "save, so nothing was done; call teach_run again"
                )
        person_approves = self.harness.gate.hold is not None
        approval = {"left": bool(held)}

        def approve(control: str) -> bool:
            if not person_approves:
                return True  # the gate's own right-record check decides
            # A person's approval covers one save: the one previewed, on the
            # script's first line.
            if (
                approval["left"]
                and tool.line_number == 1
                and control == approved_commit
            ):
                approval["left"] = False
                self._approved = control
                return True
            return False

        tool.approve = approve
        before = self.harness.stats.actions
        try:
            out = tool.run(script)
        finally:
            tool.approve = None
            self._approved = None
            self.performed += self.harness.stats.actions - before
        answer: dict[str, Any] = {"output": out.text, "committed": out.committed}
        if out.images:
            answer["output"] += "\n(screenshots are not shared here; use teach_screen)"
        if out.pending is not None:
            answer["pending"] = {
                "save": out.pending["control"],
                "script": "\n".join(out.pending["rest"]),
            }
        return answer

    def _handle(self, command: _Command) -> dict[str, Any]:
        h = self.harness
        if command.kind in ("run", "run_preview"):
            return self._run_script(command)
        if command.kind == "observe":
            obs = h.observe()
            return {"observation": obs.text}
        if command.kind == "screenshot":
            # The caller decides how to scrub it before it leaves the computer.
            h.stats.screenshots_requested += 1
            return {"png": h.computer.screenshot()}
        native, ref = self._native(command.payload)
        field = ref if native["kind"] == "type" else None
        if field is not None:
            # type with a ref clicks the field first. That click must commit
            # nothing, so it never needs its own approval.
            focus_click, _ = h.native_for({"ref": field})
            if h.commit_target(focus_click, field) is not None:
                raise ValueError(
                    f"{field!r} is not a text field; click it with its own teach_act"
                )
        commit = h.commit_target(native, ref)
        binding = self.binding(native, ref)
        if command.kind == "preview":
            return {
                "commit": commit,
                "binding": binding if commit else None,
                "action": describe(native),
            }
        if command.kind == "act":
            held = command.payload.get("_held_by_person")
            if held and held != binding:
                raise ValueError(
                    "the app or the run changed after the person approved this "
                    "action, so nothing was done; call teach_act again"
                )
            if field is not None:
                h.click(field)
                self.performed += 1
            # Only what the person was shown is approved, once.
            self._approved = commit if held else None
            try:
                h.act(native, ref)
            finally:
                self._approved = None
                self.performed += 1
            return {"observation": h.observe().text}
        raise ValueError(f"unknown command {command.kind!r}")

    def binding(self, native: Mapping[str, Any], ref: Optional[str]) -> str:
        """What a person's approval covers: this run, this exact action, and
        the app as it was after the last action performed."""

        return json.dumps(
            [self.run_id, self.performed, dict(native), ref], sort_keys=True
        )

    def _native(
        self, action: Mapping[str, Any]
    ) -> tuple[dict[str, Any], Optional[str]]:
        """A caller's action as the harness's generic action, and its ref: the
        control a click is on, or the field a ``type`` goes into (clicked
        first). Performs nothing."""

        kind = action.get("action")
        if kind not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}")
        if kind == "click":
            if action.get("ref") is not None:
                return self.harness.native_for({"ref": action["ref"]})
            if action.get("x") is None or action.get("y") is None:
                raise ValueError("click needs ref, or x and y")
            return {"kind": "click", "x": int(action["x"]), "y": int(action["y"])}, None
        if kind == "type":
            if action.get("input") is not None:
                inputs = (self.request.inputs if self.request else {}) or {}
                name = str(action["input"])
                if name not in inputs:
                    raise LookupError(f"no task input named {name!r}")
                text = str(inputs[name])
            elif action.get("text") is not None:
                text = str(action["text"])
            else:
                raise ValueError("type needs text or input")
            ref = action.get("ref")
            return {"kind": "type", "text": text}, (
                str(ref) if ref is not None else None
            )
        if kind == "press":
            return {"kind": "press", "key": str(action.get("key") or "")}, None
        return {
            "kind": "hotkey",
            "key": str(action.get("key") or ""),
            "modifiers": [str(m) for m in action.get("modifiers") or []],
        }, None

    # -- called from other threads ------------------------------------------------

    def submit(self, kind: str, payload: Mapping[str, Any]) -> Future:
        future: Future = Future()
        self._inbox.put(_Command(kind, dict(payload), future))
        return future

    def stop(self) -> None:
        self._inbox.put(_STOP)


class DrivenRun:
    """One run 1 of ``compiler``'s task, driven call by call.

    ``hold`` says whether every gated action needs a person (on the web the
    harness also checks the record's identity itself; on other surfaces it
    can't, so a person approves every gated action there whatever ``hold``
    says).
    """

    def __init__(
        self,
        compiler: Any,
        params: Mapping[str, Any],
        *,
        principal_id: Optional[str] = None,
        hold: bool = False,
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        self.on_done = on_done
        self.compiler = compiler
        self.params = dict(params)
        self.agent = DrivenAgent(principal_id=principal_id)
        self.hold = hold or compiler.backend_kind != "web"
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.done = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def person_approves_saves(self) -> bool:
        return self.hold

    def start(self, timeout_s: float = 300.0) -> None:
        """Open the app and start recording. Returns once the agent can act.

        Raises:
            Exception: whatever stopped the run before the first action
                (a draft already exists, the app did not open).
        """

        self._thread = threading.Thread(
            target=self._main, name="openadapt-teach-run", daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + timeout_s
        while not self.agent.ready.wait(0.05):
            if self.done.is_set() or time.monotonic() > deadline:
                break
        if self.error is not None:
            raise self.error
        if not self.agent.ready.is_set():
            if self.done.is_set():
                raise RunEnded(
                    "run 1 ended before the agent could act: "
                    + str(getattr(self.result, "detail", None) or "")
                )
            raise TimeoutError("the app did not open in time")

    def _main(self) -> None:
        try:
            self.result = self.compiler.record(
                self.agent,
                self.params,
                hold=self.agent.hold if self.hold else None,
                principal_id=self.agent.principal_id,
            )
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            self.error = exc
        finally:
            self.done.set()
            if self.on_done is not None:
                self.on_done()

    def call(
        self,
        kind: str,
        payload: Optional[Mapping[str, Any]] = None,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        """Send one command to the run thread and wait for its answer.

        Raises:
            RunEnded: run 1 is over (finished, refused, or failed).
        """

        if self.done.is_set():
            raise RunEnded("run 1 is over")
        future = self.agent.submit(kind, payload or {})
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                return future.result(timeout=0.05)
            except FutureTimeout:
                pass
            except Exception as exc:
                raise RunEnded(f"run 1 failed: {type(exc).__name__}: {exc}") from exc
            if self.done.is_set() and not future.done():
                raise RunEnded("run 1 is over")
            if time.monotonic() > deadline:
                # It may still run later; never let it run behind the
                # caller's back or twice after a retry. End run 1 instead.
                self.abort()
                raise RunEnded(
                    "the action did not finish in time, so run 1 was stopped; "
                    "check the app, then start again"
                )

    def finish(self, timeout_s: float = 600.0) -> Any:
        """End run 1, then wait for the compile and the record check."""

        if not self.done.is_set():
            self.agent.submit("finish", {})
        if not self.done.wait(timeout_s):
            raise TimeoutError("compiling run 1 did not finish in time")
        if self.error is not None:
            raise self.error
        return self.result

    def abort(self) -> None:
        """End run 1 now. Nothing more is performed and no draft is made."""

        self.agent.stop()


def describe(native: Mapping[str, Any]) -> str:
    """One line a person reads before approving ``native``. No typed text."""

    kind = native.get("kind")
    if kind == "click":
        return f"click at x={native.get('x')}, y={native.get('y')}"
    if kind == "type":
        text = str(native.get("text") or "")
        ending = " ending in a line break" if text.endswith(("\n", "\r")) else ""
        return f"type {len(text)} characters{ending}"
    if kind == "press":
        return f"press {native.get('key')}"
    if kind == "hotkey":
        return "press " + "+".join(
            [*native.get("modifiers", []), str(native.get("key"))]
        )
    return str(kind)
