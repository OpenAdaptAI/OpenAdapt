"""Teach tools: an MCP agent turns a task it did once into a workflow it calls.

``openadapt-agent serve --teach VERIFY_YAML`` adds these tools to the server:

``teach_start``
    Opens the operator's app (``--url`` or ``--config``) and starts run 1 of a
    task the agent names, with its inputs, the record fields the save must
    produce, and the record's identity as the screen shows it.
``teach_observe``, ``teach_act``, ``teach_screen``
    The agent does the task one action at a time. Every action goes through
    the compile-agent harness (:mod:`openadapt_flow.compile_agent.harness`):
    on the web it checks that the right record is on screen before each save;
    on Windows, macOS, Linux, RDP and Citrix it can't, so a person approves
    each save in the client's form.
``teach_finish``
    Ends run 1. Flow compiles it, reads the system of record back, and reports
    the draft: its steps, its parameters, the proposed record check, and what
    still needs a person.
``teach_confirm_checks``
    A person confirms the proposed record check in the client's form. An
    agent can't: the form asks the person for their name.
``teach_admit``
    Runs the canaries and admits the draft under the JIT rules
    (``packages/flow/docs/JIT_ADMISSION.md``): the agent admits only as the
    registered agent the operator configured, under a policy a person signed.
    It records as that agent, so admitting is self-admission, which the policy
    must allow (``allow_self_admission``). Without a key and a policy, a
    person admits at a terminal.

An admitted program joins ``list_workflows`` and ``run_workflow`` at once, and
gets its own ``taught_<name>`` tool; the server tells clients that support it
that its tool list changed. Each run executes the program with no model calls
and reads every save back.

The pipeline is :class:`~openadapt_flow.compile_agent.FirstRunCompiler`. This
module only adapts it to MCP calls; it decides nothing about admission.
"""

from __future__ import annotations

import base64
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from openadapt_flow.agent.bridge import BridgeError, ToolSpec

__all__ = ["TEACH_TOOLS", "TaughtEngine", "TeachBridge", "TeachConfig"]

TEACH_TOOLS = (
    "teach_start",
    "teach_observe",
    "teach_act",
    "teach_screen",
    "teach_finish",
    "teach_abort",
    "teach_status",
    "teach_confirm_checks",
    "teach_admit",
)
#: Tools that change state only after a person answers the client's form.
TEACH_PERSON_TOOLS = frozenset({"teach_confirm_checks"})
TAUGHT_PREFIX = "taught_"

_TASK_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
#: An operator task file's id (``openadapt.guard-task/v1``).
_TASK_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_ACTS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}
_LOCAL = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_TASK_KEY_PROP = {
    "type": "string",
    "description": "task_key from teach_start or teach_finish.",
}


@dataclass
class TeachConfig:
    """What the operator fixed at server start. The agent changes none of it."""

    verify: Path
    cache_dir: Path
    url: Optional[str] = None
    #: A deployment ``backend:`` section for a native or remote app.
    surface: Any = None
    key_dir: Optional[Path] = None
    #: A registered agent principal under a signed admission policy, or None.
    admitter: Any = None
    #: Ask a person before every save on the web too (always on elsewhere).
    hold: bool = False
    policy: str = "clinical-write"
    headless: bool = True
    #: Whether the agent sees the screen (``teach_screen``): ``off``,
    #: ``scrubbed`` (identifiers scrubbed; nothing is shared if scrubbing
    #: fails) or ``unscrubbed``. Off unless the operator turns it on.
    screens: str = "off"
    #: Tests only: the screen scrubber (default: openadapt-privacy's).
    scrubber_factory: Optional[Callable[[], Any]] = None
    #: Tests only: reset a demo app before every run.
    reset: Optional[Callable[[], None]] = None
    #: Tests only: build the live backend for a surface.
    open_backend: Optional[Callable[[Any], Any]] = None
    #: How long a taught run waits for run 1 or a canary to finish.
    app_wait_s: float = 600.0
    #: Operator task files (``openadapt.guard-task/v1``). When set,
    #: ``teach_start`` names one, and run 1's record and inputs come from it:
    #: the agent can't declare them, so page text read before a later
    #: ``teach_start`` can't change them either.
    tasks_dir: Optional[Path] = None
    #: More origins run 1's browser may reach besides the app's.
    allow_origins: tuple[str, ...] = ()
    #: The app's write adapter: run 1's writes are read before they are sent.
    write_adapter: Any = None

    #: One writer at a time on the app: run 1, the canaries and taught runs
    #: would otherwise count each other's records in their checks.
    app_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir).resolve()
        if self.screens not in ("off", "scrubbed", "unscrubbed"):
            raise ValueError("screens is off, scrubbed or unscrubbed")
        if (self.url is None) == (self.surface is None):
            raise ValueError("teach needs the app: a --url, or a --config backend")
        if self.admitter is not None:
            if getattr(self.admitter, "kind", None) != "agent" or (
                getattr(self.admitter, "policy", None) is None
            ):
                raise ValueError(
                    "the teach server admits only as a registered agent under a "
                    "signed admission policy (--teach-key with --teach-policy); "
                    "an admitter key admits from CI with openadapt-flow jit admit"
                )


def _off_thread(fn: Callable[[], Any]) -> Any:
    """Run ``fn`` on a fresh thread: browsers and native backends it opens
    live and close there, away from the server's worker pool."""

    box: dict[str, Any] = {}

    def main() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=main, name="openadapt-teach", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


class TaughtEngine:
    """Runs an admitted taught program for :class:`RunService`: no model
    calls, every save read back, a verify-run receipt."""

    def __init__(self, task_dir: Path, config: TeachConfig) -> None:
        self.task_dir = Path(task_dir)
        self.config = config

    def execute(
        self, entry: Any, run_id: str, inputs: dict[str, str], raw: Mapping[str, Any]
    ) -> Any:
        from openadapt_flow.agent.service import EngineResult
        from openadapt_flow.compile_agent import FirstRunCompiler

        compiler = FirstRunCompiler.load(
            self.task_dir,
            reset=self.config.reset,
            open_backend=self.config.open_backend,
        )
        if not self.config.app_lock.acquire(timeout=self.config.app_wait_s):
            # A run 1 or a canary is still writing to the app. Nothing ran.
            return EngineResult(reason="not_ready_to_run")
        try:
            replay = compiler.run(**inputs)
        finally:
            self.config.app_lock.release()
        if replay.verified:
            reason = "saved_and_checked"
        elif replay.status == "not_admitted":
            reason = "workflow_unavailable"
        elif replay.status == "refused":
            reason = "not_ready_to_run"
        elif replay.receipt_outcome == "HALTED_BEFORE_EFFECT":
            reason = "stopped_for_review"
        else:
            # Anything else may have written: a person checks the record.
            reason = "save_not_confirmed"
        return EngineResult(
            reason=reason,
            execution_outcome=replay.status if replay.status.isupper() else None,
            transaction_outcome=replay.receipt_outcome,
            model_calls=replay.model_calls,
            seconds=replay.seconds,
            run_dir=str(replay.run_dir) if replay.run_dir else None,
            bundle_dir=str(self.task_dir / "program" / "bundle"),
        )

    def refresh(self, entry: Any, run_id: str) -> Any:
        return None


class TeachBridge:
    """The teach tools over :class:`FirstRunCompiler`, one run 1 at a time."""

    def __init__(self, config: TeachConfig, workflows: Any = None) -> None:
        self.config = config
        self.workflows = workflows
        self.config.cache_dir.mkdir(parents=True, exist_ok=True)
        self._run: Any = None
        self._compiler: Any = None
        self._lock = threading.Lock()
        #: taught tool name -> catalog workflow name
        self.taught: dict[str, str] = {}
        for task_dir in sorted(self.config.cache_dir.iterdir()):
            if self._is_admitted(task_dir):
                self._register(task_dir)

    # -- tool surface ----------------------------------------------------------

    def handles(self, name: str) -> bool:
        if name == "teach_screen":
            return self.config.screens != "off"
        return name in TEACH_TOOLS or name in self.taught

    @property
    def backend_kind(self) -> str:
        if self.config.surface is None:
            return "web"
        from openadapt_flow.jit.compile_agent import SurfaceApp

        return SurfaceApp(self.config.surface).kind

    def list_tool_specs(self) -> list[ToolSpec]:
        on_web = self.backend_kind == "web"
        specs = [
            ToolSpec(
                name="teach_start",
                description=(
                    "Teach OpenAdapt a task by doing it once. OpenAdapt opens the "
                    f"app this server was set up for ({self.backend_kind}) and "
                    "records what you do; you then call teach_observe and "
                    "teach_act until the task is done, and teach_finish. This "
                    "run makes a real entry in that app, so use test records. "
                    "task is the instruction with {name} for each input; inputs "
                    "are this run's values; record_fields are fields the saved "
                    "record must have (such as its patient id); on_screen is "
                    "the record's identity as the screen shows it (a name, an "
                    "MRN), checked before every save. If the task was already "
                    "taught, nothing runs and you get its status."
                ),
                input_schema=_obj(
                    {
                        "task": {"type": "string", "minLength": 4, "maxLength": 500},
                        "inputs": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "record_fields": {
                            "type": "object",
                            "minProperties": 1,
                            "additionalProperties": {"type": "string"},
                        },
                        "on_screen": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "minLength": 1},
                        },
                        "name": {
                            "type": "string",
                            "pattern": _NAME.pattern,
                            "description": "Short name for the workflow (a-z, 0-9, _).",
                        },
                        **(
                            {
                                "task_file": {
                                    "type": "string",
                                    "pattern": _TASK_FILE.pattern,
                                    "description": (
                                        "The operator's task file for this run; "
                                        "its record and inputs are used."
                                    ),
                                }
                            }
                            if self.config.tasks_dir is not None
                            else {}
                        ),
                    },
                    (
                        ["task", "task_file", "on_screen"]
                        if self.config.tasks_dir is not None
                        else ["task", "inputs", "record_fields", "on_screen"]
                    ),
                ),
                annotations=_ACTS,
            ),
            ToolSpec(
                name="teach_observe",
                description=(
                    "Read the app again. "
                    + (
                        "You get the page as short lines: headings, text, table "
                        "rows, and every control with a ref such as [e3]."
                        if on_web
                        else "You get one line per control with a ref such as "
                        "[n_1a2b3c4d]; a remote session has none, so use "
                        "teach_screen and click x,y."
                    )
                    + " Refs change after every action."
                ),
                input_schema=_obj({}, []),
                annotations=_READ_ONLY,
            ),
            ToolSpec(
                name="teach_act",
                description=(
                    "Do one action in run 1: click (ref, or x and y), type "
                    "(text, or input to type a task input's value; ref to "
                    "focus a field first), press (key: Enter, Tab, ...) or "
                    "hotkey (key and modifiers). Returns the app's new state. "
                    + (
                        "Before any save the harness checks that the record's "
                        "identity is shown next to the control; a save on the "
                        "wrong record is refused and ends run 1."
                        if on_web and not self.config.hold
                        else "A person approves every save in your client's form "
                        "before it happens; a save they don't approve ends run 1."
                    )
                ),
                input_schema=_obj(
                    {
                        "action": {
                            "type": "string",
                            "enum": ["click", "type", "press", "hotkey"],
                        },
                        "ref": {"type": "string", "maxLength": 64},
                        "x": {"type": "integer", "minimum": 0},
                        "y": {"type": "integer", "minimum": 0},
                        "text": {"type": "string", "maxLength": 4000},
                        "input": {"type": "string", "maxLength": 64},
                        "key": {"type": "string", "maxLength": 32},
                        "modifiers": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 16},
                            "maxItems": 4,
                        },
                    },
                    ["action"],
                ),
                annotations=_ACTS,
            ),
            ToolSpec(
                name="teach_finish",
                description=(
                    "End run 1 when the task is done. OpenAdapt compiles what you "
                    "did, reads the app's records to check that exactly the "
                    "record the task named was saved, and returns the draft: "
                    "its steps, its parameters, the proposed record check, and "
                    "what still needs a person."
                ),
                input_schema=_obj({}, []),
                annotations=_LOCAL,
            ),
            ToolSpec(
                name="teach_abort",
                description="Stop run 1 now. Nothing is compiled.",
                input_schema=_obj({}, []),
                annotations=_LOCAL,
            ),
            ToolSpec(
                name="teach_status",
                description=(
                    "The draft of a taught task: steps, parameters, the proposed "
                    "record check, and what still needs a person."
                ),
                input_schema=_obj({"task_key": _TASK_KEY_PROP}, ["task_key"]),
                annotations=_READ_ONLY,
            ),
            ToolSpec(
                name="teach_confirm_checks",
                description=(
                    "Ask a person to confirm the proposed record check. Your "
                    "client shows them a form with the check, and only their "
                    "answer there confirms it; there is no argument you can "
                    "send to confirm it. drop_fields are fields that change on "
                    "every save (an id, a time stamp) to leave out of the check."
                ),
                input_schema=_obj(
                    {
                        "task_key": _TASK_KEY_PROP,
                        "drop_fields": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 64},
                            "maxItems": 20,
                        },
                    },
                    ["task_key"],
                ),
                annotations=_LOCAL,
            ),
            ToolSpec(
                name="teach_admit",
                description=(
                    "Admit the draft so it can run. OpenAdapt runs canaries (real "
                    "saves with canary_inputs, on the record the task names, so "
                    "use values that are safe there) and admits only under a "
                    "signed admission policy the operator set up for you. "
                    "Without one, it tells you the command a person runs at a "
                    "terminal. Once admitted, the workflow is in list_workflows "
                    "and has its own taught_<name> tool."
                ),
                input_schema=_obj(
                    {
                        "task_key": _TASK_KEY_PROP,
                        "canary_inputs": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 10,
                            "items": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                        },
                    },
                    ["task_key", "canary_inputs"],
                ),
                annotations=_ACTS,
            ),
        ]
        if self.config.screens != "off":
            specs.insert(
                2,
                ToolSpec(
                    name="teach_screen",
                    description=(
                        "The app's screen as a PNG"
                        + (
                            ", with identifiers scrubbed (nothing is shared if "
                            "scrubbing fails)"
                            if self.config.screens == "scrubbed"
                            else ""
                        )
                        + ". Use it when teach_observe is not enough (a remote "
                        "session has no control tree)."
                    ),
                    input_schema=_obj({}, []),
                    annotations=_READ_ONLY,
                ),
            )
        for tool, workflow in sorted(self.taught.items()):
            specs.append(self._taught_spec(tool, workflow))
        return specs

    def _taught_spec(self, tool: str, workflow: str) -> ToolSpec:
        from openadapt_flow.agent.cards import inputs_schema

        entry = self.workflows.catalog[workflow]
        return ToolSpec(
            name=tool,
            description=(
                f"Run the taught workflow {workflow}: {entry.card.purpose} It "
                "runs a compiled program with no model calls and reads every "
                "save back. Same outcomes as run_workflow; reuse request_id on "
                "a retry."
            ),
            input_schema=_obj(
                {
                    "inputs": inputs_schema(entry.card),
                    "request_id": {"type": "string", "minLength": 4, "maxLength": 128},
                },
                ["inputs", "request_id"],
            ),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": True,
                "idempotentHint": True,
                "openWorldHint": True,
            },
        )

    # -- what the MCP layer asks a person ------------------------------------------

    def hold_needed(self, arguments: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """For ``teach_act``: when a person must approve this action first,
        what it may commit, a description, and the binding the approval
        covers (this run, this action, the app as it is now). Else None."""

        run = self._active()
        if not run.person_approves_saves:
            return None
        answer = self._run_call("preview", arguments)
        if "error" in answer or not answer.get("commit"):
            return None  # teach_act reports any error, performing nothing
        return answer

    def hold_message(self, hold: Mapping[str, Any]) -> str:
        commit = str(hold.get("commit"))
        task = self._compiler.task if self._compiler is not None else None
        identity = ", ".join(self._compiler.on_screen) if self._compiler else ""
        return (
            "The agent is teaching OpenAdapt a task"
            + (f" ({task.task_id})" if task is not None else "")
            + f" and is about to {hold.get('action')} ({commit}), which may "
            f"save a record. Look at the app: is the record {identity} on "
            "screen, and should this happen? It is a real entry in this app. "
            "Your answer covers only this action on the screen as it is now."
        )

    def confirm_checks_message(self, arguments: Mapping[str, Any]) -> str:
        compiler = self._load(arguments.get("task_key"))
        proposed = compiler.proposed()
        drop = set(arguments.get("drop_fields") or [])
        lines = []
        for effect in proposed["effects"]:
            fields = ", ".join(
                f"{name} = {value}"
                for name, value in effect["match"].items()
                if name not in drop
            )
            lines.append(f"{effect['kind']}: {fields}")
        params = ", ".join(proposed["params"]) or "none"
        return (
            "Every later run of this taught workflow must pass this record "
            "check, read from the app's records: "
            + "; ".join(lines)
            + f". Inputs that change on each run: {params}."
            + (f" Left out: {', '.join(sorted(drop))}." if drop else "")
            + " Confirm only if this is the record the task should write. "
            "Enter your own name; an agent or model name is refused."
        )

    # -- dispatch -----------------------------------------------------------------

    def dispatch(
        self,
        name: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        held_by_person: Optional[str] = None,
        person: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run one teach tool.

        ``held_by_person`` (the control a person approved for this
        ``teach_act``) and ``person`` (the name a person gave to confirm the
        record check) are set only by the MCP layer after a person answered
        the client's form; no argument stands in for them.
        """

        args = dict(arguments or {})
        if name in self.taught:
            return self._run_taught(name, args)
        handlers = {
            "teach_start": self._start,
            "teach_observe": lambda a: self._observe(),
            "teach_screen": lambda a: self._screen(),
            "teach_finish": lambda a: self._finish(),
            "teach_abort": lambda a: self._abort(),
            "teach_status": lambda a: self._status(a.get("task_key")),
            "teach_admit": self._admit,
        }
        if name == "teach_act":
            return self._act(args, held_by_person=held_by_person or None)
        if name == "teach_confirm_checks":
            return self._confirm_checks(args, person)
        if name not in handlers or not self.handles(name):
            raise BridgeError("unknown tool name")
        return handlers[name](args)

    # -- run 1 ----------------------------------------------------------------------

    def _active(self) -> Any:
        run = self._run
        if run is None or run.done.is_set():
            raise BridgeError(
                "no run 1 is in progress; call teach_start (or teach_finish to "
                "see how the last one ended)"
            )
        return run

    def _run_call(self, kind: str, payload: Optional[Mapping[str, Any]] = None) -> dict:
        from openadapt_flow.compile_agent.driven import RunEnded

        run = self._active()
        try:
            return run.call(kind, payload or {})
        except RunEnded as exc:
            raise BridgeError(f"{exc}; call teach_finish for the result") from None

    def _declared(
        self, args: Mapping[str, Any]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Run 1's inputs and record fields: the operator's task file when
        the server has ``tasks_dir``, else what the agent declared at
        ``teach_start``, before it read the app."""

        inputs = {str(k): str(v) for k, v in (args.get("inputs") or {}).items()}
        record = {str(k): str(v) for k, v in (args.get("record_fields") or {}).items()}
        if self.config.tasks_dir is None:
            if args.get("task_file"):
                raise BridgeError("this server has no operator task files")
            return inputs, record
        from openadapt_flow.guard import GuardRefused, load_task

        task_id = str(args.get("task_file") or "")
        if not _TASK_FILE.fullmatch(task_id):
            raise BridgeError(
                "this server takes run 1's record and inputs from the operator's "
                "task files: name one with task_file"
            )
        try:
            task = load_task(self.config.tasks_dir / f"{task_id}.json")
        except GuardRefused as exc:
            raise BridgeError(f"task_file {task_id}: {exc.message}") from None
        if task.task_id != task_id:
            raise BridgeError("the task file names a different task_id")
        filed_inputs = {k: str(v) for k, v in task.intended_fields.items()}
        filed_record = {k: str(v) for k, v in task.expected_record.items()}
        if (inputs and inputs != filed_inputs) or (record and record != filed_record):
            raise BridgeError(
                "inputs and record_fields come from the operator's task file; "
                "leave them out or send exactly its values"
            )
        return filed_inputs, filed_record

    def _compiler_for(self, args: Mapping[str, Any]) -> Any:
        from openadapt_flow.compile_agent import FirstRunCompiler

        inputs, record = self._declared(args)
        for key in inputs:
            if not _NAME.fullmatch(key):
                raise BridgeError(f"input names are a-z, 0-9 and _ ({key!r})")
        name = args.get("name")
        try:
            return FirstRunCompiler(
                str(args["task"]),
                params=sorted(inputs),
                url=self.config.url,
                surface=self.config.surface,
                open_backend=self.config.open_backend,
                verify=self.config.verify,
                expect=record,
                on_screen=list(args["on_screen"]),
                task_id=f"taught.{name}" if name else None,
                policy=self.config.policy,
                cache_dir=self.config.cache_dir,
                key_dir=self.config.key_dir,
                reset=self.config.reset,
                headless=self.config.headless,
                on_warning=lambda text: None,
                allow_origins=self.config.allow_origins,
                write_adapter=self.config.write_adapter,
            ), inputs
        except ValueError as exc:
            raise BridgeError(str(exc)) from None

    def _start(self, args: Mapping[str, Any]) -> dict[str, Any]:
        from openadapt_flow.compile_agent.driven import DrivenRun
        from openadapt_flow.jit.compile_agent import RECORDING_WARNING

        with self._lock:
            if self._run is not None and not self._run.done.is_set():
                raise BridgeError(
                    "run 1 of another task is in progress; teach_finish or "
                    "teach_abort it first"
                )
            compiler, inputs = self._compiler_for(args)
            jit = compiler._jit()
            if jit.admitted or jit.candidate() is not None:
                return {"started": False, **self._status(compiler.task.key)}
            if not self.config.app_lock.acquire(blocking=False):
                raise BridgeError(
                    "a canary or a taught run is writing to the app; try "
                    "teach_start again when it is done"
                )
            admitter = self.config.admitter
            run = DrivenRun(
                compiler,
                inputs,
                principal_id=admitter.principal_id if admitter is not None else None,
                hold=self.config.hold,
                on_done=self.config.app_lock.release,
            )
            # Kept even if it fails to start: until its thread ends, no other
            # run 1 may start on the app.
            self._run, self._compiler = run, compiler
            try:
                run.start()
            except Exception as exc:
                # A run that opens late must not wait for actions forever.
                run.abort()
                raise BridgeError(
                    f"run 1 did not start: {exc}; it is being stopped"
                ) from None
        observation = self._run_call("observe")
        return {
            "started": True,
            "status": "recording",
            "task_key": compiler.task.key,
            "surface": compiler.backend_kind,
            "person_approves_saves": run.person_approves_saves,
            "warning": RECORDING_WARNING,
            "observation": observation.get("observation"),
            "next": "teach_act until the task is done, then teach_finish",
        }

    def _observe(self) -> dict[str, Any]:
        return {"observation": self._run_call("observe").get("observation")}

    def _screen(self) -> dict[str, Any]:
        png = self._run_call("screenshot").get("png") or b""
        scrubbed = False
        if self.config.screens == "scrubbed":
            from openadapt_flow.agent.repair import default_scrubber, scrub_png

            try:
                png, _evidence = scrub_png(
                    png, self.config.scrubber_factory or default_scrubber
                )
            except Exception:  # noqa: BLE001 - fail closed: share nothing
                raise BridgeError(
                    "the screen could not be scrubbed, so no screen was shared"
                ) from None
            scrubbed = True
        return {
            "scrubbed": scrubbed,
            "screen_png_base64": base64.b64encode(png).decode("ascii"),
        }

    def _act(
        self, args: Mapping[str, Any], *, held_by_person: Optional[str]
    ) -> dict[str, Any]:
        run = self._active()
        payload = dict(args)
        if held_by_person:
            payload["_held_by_person"] = held_by_person
        answer = self._run_call("act", payload)
        if "refused" in answer:
            run.done.wait(120)
            return {
                "status": "refused",
                "detail": answer["refused"],
                "run_over": True,
                "next": "teach_finish shows the result; nothing was saved or compiled",
            }
        if "error" in answer:
            return {
                "status": "error",
                "error": answer["error"],
                "detail": answer.get("detail"),
                "performed": False,
            }
        return {"status": "ok", "observation": answer.get("observation")}

    def _abort(self) -> dict[str, Any]:
        run = self._active()
        run.abort()
        run.done.wait(120)
        return {"status": "aborted", "compiled": False}

    def _finish(self) -> dict[str, Any]:
        run = self._run
        if run is None:
            raise BridgeError("no run 1 to finish; call teach_start")
        try:
            result = run.finish()
        except Exception as exc:  # noqa: BLE001 - reported to the agent
            return {
                "status": "capture_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        report = self._report(self._compiler, result.status, result.detail)
        report["record_check"] = result.record_check
        report["save_gate"] = [
            {"control": g.get("control"), "decision": g.get("decision")}
            for g in (result.run1.get("gates") or [])
        ]
        return report

    # -- the draft ---------------------------------------------------------------------

    def _load(self, task_key: Any) -> Any:
        from openadapt_flow.compile_agent import FirstRunCompiler

        if not isinstance(task_key, str) or not _TASK_KEY.fullmatch(task_key):
            raise BridgeError("unknown task_key")
        task_dir = self.config.cache_dir / task_key
        if not (task_dir / "compile_agent.json").is_file():
            raise BridgeError("unknown task_key")
        return FirstRunCompiler.load(
            task_dir, reset=self.config.reset, open_backend=self.config.open_backend
        )

    def _status(self, task_key: Any) -> dict[str, Any]:
        compiler = self._load(task_key)
        jit = compiler._jit()
        if jit.admitted:
            status = "admitted"
        elif jit.candidate() is not None:
            status = "draft"
        else:
            status = "no_draft"
        return self._report(compiler, status, None)

    def _report(
        self, compiler: Any, status: str, detail: Optional[str]
    ) -> dict[str, Any]:
        from openadapt_flow.compile_agent.pipeline import CONFIRMED_FILE

        jit = compiler._jit()
        report: dict[str, Any] = {
            "status": status,
            "task_key": compiler.task.key,
            "surface": compiler.backend_kind,
        }
        if detail:
            report["detail"] = detail
        if status == "admitted" or jit.admitted:
            report["status"] = "admitted"
            report["workflow"] = self._workflow_name(compiler.dir)
            report["needs_person"] = []
            return report
        candidate = jit.candidate()
        if candidate is None:
            report["needs_person"] = []
            if status not in ("draft", "no_draft"):
                report["next"] = (
                    "nothing can be admitted from this run; fix the cause and "
                    "call teach_start again"
                )
            return report
        lifted = candidate.get("params") or {}
        report["steps"] = [
            {"id": s["id"], "action": s["action"]} for s in jit.status()["steps"]
        ]
        report["parameters"] = {
            "inputs": sorted(compiler.task.params),
            "lifted": [p.get("name") for p in lifted.get("lifted") or []],
            "not_lifted": dict(lifted.get("unlifted") or {}),
        }
        try:
            proposed = compiler.proposed()
            report["proposed_record_check"] = proposed["effects"]
        except (OSError, KeyError):
            report["proposed_record_check"] = None
        confirmed = (compiler.out_dir / CONFIRMED_FILE).is_file()
        report["record_check_confirmed"] = confirmed
        needs: list[str] = []
        if not confirmed:
            needs.append(
                "A person confirms the proposed record check: teach_confirm_checks "
                "asks them in your client's form, or they run "
                f"`{self._terminal_command(compiler)}` at a terminal."
            )
        if self.config.admitter is None:
            needs.append(
                "A person admits the program at a terminal: "
                f"`{self._terminal_command(compiler)}`. This server has no "
                "admission policy, so teach_admit can't admit it."
            )
        report["needs_person"] = needs
        report["next"] = (
            "teach_confirm_checks"
            if not confirmed
            else (
                "teach_admit" if self.config.admitter is not None else "a person admits"
            )
        )
        return report

    def _terminal_command(self, compiler: Any) -> str:
        params = " ".join(
            f"--canary-param {p}=<safe value>" for p in sorted(compiler.task.params)
        )
        return (
            f"openadapt-flow compile-agent admit {compiler.dir} --admit-as <your name> "
            + params
        ).strip()

    def _confirm_checks(self, args: Mapping[str, Any], person: Optional[str]) -> dict:
        from openadapt_flow.jit.lifecycle import AdmissionError

        if not person:
            raise BridgeError(
                "a person confirms the record check in the client's form; "
                "nothing was confirmed"
            )
        compiler = self._load(args.get("task_key"))
        try:
            compiler.confirm_checks(
                principal_id=person, drop_fields=list(args.get("drop_fields") or [])
            )
        except (AdmissionError, ValueError, OSError) as exc:
            raise BridgeError(f"not confirmed: {exc}") from None
        return {**self._report(compiler, "draft", None), "confirmed_by": person}

    def _admit(self, args: Mapping[str, Any]) -> dict[str, Any]:
        from openadapt_flow.jit.lifecycle import AdmissionError

        compiler = self._load(args.get("task_key"))
        if self.config.admitter is None:
            return {
                **self._report(compiler, "draft", None),
                "status": "needs_person",
                "detail": (
                    "this server has no admission policy, so an agent can't "
                    "admit; a person admits at a terminal"
                ),
            }
        canaries = [
            {str(k): str(v) for k, v in row.items()}
            for row in args.get("canary_inputs") or []
        ]

        def admit_and_emit() -> Any:
            if not self.config.app_lock.acquire(blocking=False):
                raise BridgeError(
                    "run 1 or a taught run is writing to the app; admit when it is done"
                )
            try:
                result = compiler.admit(
                    admitter=self.config.admitter, canary_params=canaries
                )
            finally:
                self.config.app_lock.release()
            if result.admitted:
                result.emitted = compiler.emit()
            return result

        try:
            result = _off_thread(admit_and_emit)
        except (AdmissionError, ValueError) as exc:
            raise BridgeError(f"not admitted: {exc}") from None
        out: dict[str, Any] = {
            "status": result.status,
            "task_key": compiler.task.key,
            "detail": result.detail,
            "canaries": [
                {
                    "status": c.get("status"),
                    "receipt": c.get("receipt_outcome"),
                    "model_calls": c.get("model_calls"),
                }
                for c in (result.admission or {}).get("canaries") or []
            ],
        }
        if result.admitted:
            tool = self._register(compiler.dir)
            out.update(
                {
                    "workflow": self.taught[tool],
                    "tool": tool,
                    "mode": (result.admission or {}).get("mode"),
                    "admitted_by": (result.admission or {}).get("admitted_by"),
                    "skill": (result.emitted or {}).get("skill"),
                    "next": f"call {tool}, or run_workflow with this workflow",
                }
            )
        return out

    def close(self, timeout_s: float = 30.0) -> None:
        """Stop a run 1 the agent never finished (server exit). No draft."""

        run = self._run
        if run is not None and not run.done.is_set():
            run.abort()
            run.done.wait(timeout_s)

    # -- admitted programs as workflows ---------------------------------------------------

    @staticmethod
    def _is_admitted(task_dir: Path) -> bool:
        return (task_dir / "compile_agent.json").is_file() and (
            task_dir / "program" / "bundle" / "admission.json"
        ).is_file()

    def _workflow_name(self, task_dir: Path) -> Optional[str]:
        for workflow in self.taught.values():
            entry = self.workflows.catalog.get(workflow) if self.workflows else None
            engine = getattr(entry, "engine", None)
            if isinstance(engine, TaughtEngine) and engine.task_dir == task_dir:
                return workflow
        return None

    def _register(self, task_dir: Path) -> str:
        """Add an admitted program to the workflow catalog; returns its tool."""

        import json

        from openadapt_flow.agent.cards import (
            DEFAULT_DONE_MEANS,
            WorkflowCard,
            _check_text,
            _default_description,
            default_purpose,
        )
        from openadapt_flow.agent.service import CatalogEntry

        if self.workflows is None:
            raise BridgeError("this server has no workflow tools to add the program to")
        existing = self._workflow_name(task_dir)
        if existing is not None:
            return next(t for t, w in self.taught.items() if w == existing)
        spec = json.loads((task_dir / "compile_agent.json").read_text(encoding="utf-8"))
        task = spec["task"]
        base = re.sub(r"[^a-z0-9]+", "_", str(task["task_id"]).lower()).strip("_")
        base = (
            base.removeprefix("taught_").removeprefix("compile_agent_")[:40] or "task"
        )
        name = base
        suffix = 2
        while name in self.workflows.catalog or TAUGHT_PREFIX + name in self.taught:
            name = f"{base}_{suffix}"
            suffix += 1
        try:
            purpose = _check_text("purpose", f"Taught: {task['instruction']}")
        except Exception:  # noqa: BLE001 - an unusable instruction gets the default
            purpose = default_purpose()
        params = sorted(task.get("params") or [])
        card = WorkflowCard(
            name=name,
            purpose=purpose,
            done_means=DEFAULT_DONE_MEANS,
            inputs={
                p: {"type": "string", "description": _default_description(p)}
                for p in params
            },
            required=params,
            changes_records=True,
        )
        self.workflows.add_catalog_entry(
            CatalogEntry(card=card, engine=TaughtEngine(task_dir, self.config))
        )
        tool = TAUGHT_PREFIX + name
        self.taught[tool] = name
        return tool

    def _run_taught(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        return self.workflows.dispatch(
            "run_workflow",
            {
                "workflow": self.taught[tool],
                "inputs": args.get("inputs") or {},
                "request_id": args.get("request_id"),
            },
        )
