"""Compile a computer-use agent's run once, then run the program.

``compile_agent`` wraps a computer-use agent (CUA). The first run of a task is
a cache miss: the agent does the task while :class:`CaptureComputer` records
it, Flow compiles the recording, and the result is a DRAFT bundle that waits
for a person. :meth:`CompiledAgent.recording` is the same capture as a
context manager, for a loop you keep in your own code. Later runs of an
admitted task are cache hits: Flow runs the compiled program under the
standard profile, reads the system of record back through the verifier, and
makes no model calls.

A draft becomes ADMITTED through :meth:`~CompiledAgent.confirm_params`,
:meth:`~CompiledAgent.review`, :meth:`~CompiledAgent.canary` and
:meth:`~CompiledAgent.admit`, each by a named person (see
:mod:`openadapt_flow.jit.lifecycle`).

What it never does:

* admit a draft by itself. ``run`` returns ``needs_human_admit`` and never
  runs a draft; only the attended canary does.
* record a model-driven run on a key that is not the customer's. An agent
  that calls a model needs a :class:`~openadapt_flow.jit.keys.CustomerModelKey`.
* call the agent on a cache hit, or after a halt. A run that does not end
  ``VERIFIED`` goes to the exception inbox and back to the caller.
* match a task fuzzily. The cache key is the task id and the parameter schema.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Union,
)
from urllib.parse import urlsplit

from openadapt_flow.jit import lifecycle
from openadapt_flow.jit.adapters import CaptureComputer, UnsupportedAction, dialects
from openadapt_flow.jit.keys import CustomerModelKey
from openadapt_flow.jit.lifecycle import AdmissionError
from openadapt_flow.jit.lift import lift_params
from openadapt_flow.jit.signature import TaskSignature

__all__ = [
    "AdmissionError",
    "CompiledAgent",
    "ComputerUseAgent",
    "JitResult",
    "App",
    "RECORDING_WARNING",
    "Recording",
    "RecordingDispatcher",
    "SURFACE_KINDS",
    "SurfaceApp",
    "TaskRequest",
    "WebApp",
    "compile_agent",
]

CANDIDATE_FILE = lifecycle.CANDIDATE_FILE
ADMIT_FILE = lifecycle.ADMISSION_FILE
NEEDS_HUMAN_ADMIT = "needs_human_admit"
#: The agent went outside the task's declared teach scope; no draft.
SCOPE_REFUSED = "scope_refused"
#: The system of record did not show exactly the declared write; no draft.
READBACK_REFUSED = "readback_refused"

if TYPE_CHECKING:
    # openadapt_flow.teach_scope imports this package's adapters, so it is
    # imported where it is used, not at module load.
    from openadapt_flow.teach_scope import TeachGuard, TeachScope

#: A teach scope, or a function from the recording's inputs to one.
TeachScopeSource = Union["TeachScope", Callable[[Mapping[str, Any]], "TeachScope"]]

#: Printed to stderr before every recording starts, before the app opens.
#: An ``on_warning`` callback gets it too; it can't replace the print.
RECORDING_WARNING = (
    "Before you record: the recording keeps every screen the agent sees and "
    "every value it types, and the trace may contain sensitive data such as "
    "patient or customer records, names, and account numbers. Use a sandbox "
    "or test environment and test data when you can."
)


def _reuse_refusal(
    record: Mapping[str, Any], workflow: Any, task: TaskSignature, app: Any
) -> Optional[str]:
    """Why an admitted program may not be reused for this task here, or None.

    Reuse is bound to intent, not just to the task id and parameter schema:
    the record check must be the one admitted (and the one the caller pinned,
    if any), and the app environment must be the one the canary ran in (and
    the one the caller pinned, if any). Admissions from before these pins
    existed carry neither; then only the caller's pins are checked.
    """

    from openadapt_flow.jit.signature import record_check_digest

    current = record_check_digest(workflow)
    admitted = record.get("record_check_digest")
    if admitted is not None and admitted != current:
        return (
            f"the program's record check ({current[:12]}) is not the one admitted "
            f"({str(admitted)[:12]}); admit it again"
        )
    if task.record_check is not None and task.record_check != current:
        return (
            f"the task pins record check {task.record_check[:12]}, the admitted "
            f"program checks {current[:12]}; a changed record check is a new task"
        )
    try:
        environment = app.environment
    except Exception as exc:  # noqa: BLE001 - an unknown environment never matches
        return f"the app's environment could not be read ({type(exc).__name__})"
    canary_env = (record.get("canary") or {}).get("environment")
    if canary_env is not None and canary_env != environment:
        return (
            f"the program was admitted in environment {canary_env!r}, this run is "
            f"in {environment!r}; run a canary in this environment"
        )
    if task.environment is not None and task.environment != environment:
        return (
            f"the task is pinned to environment {task.environment!r}, the app "
            f"reports {environment!r}"
        )
    return None


def _print_warning(text: str) -> None:
    print(f"WARNING: {text}", file=sys.stderr)


@dataclass(frozen=True)
class WebApp:
    """The web app the task runs in.

    Args:
        url: The entry URL. The agent starts here, and so does every run.
        record_reader: A read-only snapshot of the system of record, taken
            after each recorded step so the compiler can propose the effect
            contract. Without it the candidate has no effect contract and no
            run can reach ``VERIFIED``.
        reset: Called before every capture and every run. For demo and test
            apps only.
        headless: Run the browser headless.
        setup: Called with the Playwright page right after the browser opens
            at ``url``, before every capture and every run: unmeasured setup
            such as signing in with a test account and opening a blank form.
            Nothing it does is recorded or replayed. It must leave the page
            at ``url``, where the program starts.
        record_video_dir: OPT-IN. Each capture and run records a WebM of its
            browser page into this directory (for demo media). ``None``
            records nothing.
        capture_settle_timeout_s: How long the capture waits for the page to
            settle after each agent action before it takes the after-frame
            the compiler reads. ``None`` keeps the authoring default. Raise
            it for an app whose lookups take seconds, or the program
            inherits checks compiled from a still-loading screen.
        environment_id: Optional. What identifies this app environment
            beyond its origin, such as the app's version or build
            (``"7.0.2"``), or a callable that reads it from the app before
            each canary and run. A program admitted under one environment is
            refused under another (see :attr:`environment`).
    """

    url: str
    record_reader: Optional[Callable[[], Optional[list[dict[str, Any]]]]] = None
    reset: Optional[Callable[[], None]] = None
    headless: bool = True
    setup: Optional[Callable[[Any], None]] = None
    record_video_dir: Optional[str] = None
    capture_settle_timeout_s: Optional[float] = None
    environment_id: Optional[str | Callable[[], str]] = None

    def open(
        self, *, before_goto: Optional[Callable[[Any], None]] = None
    ) -> tuple[Any, Callable[[], None]]:
        """Open the browser at ``url`` and run :attr:`setup`; returns ``(backend, close)``.

        ``before_goto`` gets the page before it loads ``url`` (a teach
        scope's request filter).
        """

        from openadapt_flow.backends.playwright_backend import PlaywrightBackend

        backend, close = PlaywrightBackend.launch(
            self.url,
            headless=self.headless,
            record_video_dir=self.record_video_dir,
            before_goto=before_goto,
        )
        if self.setup is not None:
            try:
                self.setup(backend.page)
            except BaseException as error:
                from openadapt_flow.backends._owned import close_owned

                close_owned((close,), primary=error)
                raise
        return backend, close

    @property
    def origin(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def kind(self) -> str:
        return "web"

    @property
    def environment(self) -> str:
        """The app environment's identity: the origin, plus ``#<id>``."""

        return _environment(self.origin, self.environment_id)

    @property
    def entry_url(self) -> Optional[str]:
        return self.url

    @property
    def backend_hints(self) -> Optional[dict[str, Any]]:
        return None

    @property
    def deployment_surface(self) -> Optional[str]:
        return None


#: Backend kinds :class:`SurfaceApp` drives. ``web`` is :class:`WebApp`.
SURFACE_KINDS = ("windows", "macos", "linux", "rdp", "citrix")


def _surface_kind(kind: Optional[str]) -> str:
    k = (kind or "").strip().lower().replace("_", "-")
    return "rdp" if k == "remote-display" else k


@dataclass(frozen=True)
class SurfaceApp:
    """A native or remote-display app the task runs in.

    The app is named the way a deployment file names it: the ``backend``
    section of a ``deploy.yaml`` (``kind`` plus that kind's target). The agent
    drives it through the same :class:`~openadapt_flow.authoring.AuthoringSession`
    and :class:`CaptureComputer` the MCP recording tools use, and every canary
    and run opens a fresh backend from the same section.

    ============  =======================================================
    ``kind``      Target the section must name
    ============  =======================================================
    ``windows``   ``agent_url`` (the in-guest agent; UI Automation)
    ``macos``     ``macos_app`` (Accessibility; ``macos_window_title``)
    ``linux``     ``linux_app`` and ``linux_window_title`` (AT-SPI);
                  agent clicks need ``linux_allow_physical_input``
    ``rdp``       ``rdp_host`` (network RDP) or ``rdp_window`` (local client)
    ``citrix``    nothing (the host's Citrix Workspace window), or
                  ``rdp_window``; runs need ``rdp_readiness_text``
    ============  =======================================================

    RDP and Citrix are screen-only: the agent works from pixels and keys.

    Args:
        backend: The deployment ``backend`` section, as a
            :class:`~openadapt_flow.deployment.BackendConfig` or a mapping.
        record_reader: As for :class:`WebApp`.
        reset: As for :class:`WebApp`.
        open_backend: Builds the live backend from ``backend``. Defaults to
            :func:`openadapt_flow.backends.factory.build_backend`. Pass one to
            inject a client (for example a test fixture).
        environment_id: As for :class:`WebApp`.
    """

    backend: Any
    record_reader: Optional[Callable[[], Optional[list[dict[str, Any]]]]] = None
    reset: Optional[Callable[[], None]] = None
    open_backend: Optional[Callable[[Any], Any]] = field(default=None, repr=False)
    environment_id: Optional[str | Callable[[], str]] = None

    def __post_init__(self) -> None:
        from openadapt_flow.deployment import BackendConfig

        cfg = self.backend
        if not isinstance(cfg, BackendConfig):
            cfg = BackendConfig.model_validate(dict(cfg))
            object.__setattr__(self, "backend", cfg)
        kind = _surface_kind(cfg.kind)
        if kind == "web":
            raise ValueError("backend.kind 'web' is a browser app: use WebApp(url=...)")
        if kind not in SURFACE_KINDS:
            raise ValueError(
                f"unknown backend.kind {cfg.kind!r} (expected one of "
                f"{', '.join(SURFACE_KINDS)})"
            )
        missing = {
            "windows": None if cfg.agent_url else "backend.agent_url",
            "macos": None if cfg.macos_app else "backend.macos_app",
            "linux": (
                None
                if cfg.linux_app and cfg.linux_window_title
                else "backend.linux_app and backend.linux_window_title"
            ),
            "rdp": (
                None
                if bool(cfg.rdp_host) != bool(cfg.rdp_window)
                else "exactly one of backend.rdp_host or backend.rdp_window"
            ),
            "citrix": (
                "no backend.rdp_host (Citrix drives a local Workspace window)"
                if cfg.rdp_host
                else None
            ),
        }[kind]
        if missing:
            raise ValueError(f"backend.kind {kind!r} needs {missing}")
        self.origin  # noqa: B018 - refuses a credential in agent_url now

    @classmethod
    def from_deployment(cls, path: Path | str, **kwargs: Any) -> "SurfaceApp":
        """The app named by the ``backend`` section of a deployment file."""

        from openadapt_flow.deployment import load_deployment

        return cls(load_deployment(path).backend, **kwargs)

    @property
    def kind(self) -> str:
        return _surface_kind(self.backend.kind)

    @property
    def url(self) -> Optional[str]:
        return None

    @property
    def entry_url(self) -> Optional[str]:
        return None

    @property
    def origin(self) -> str:
        """``<kind>://<target>``, from
        :func:`~openadapt_flow.surface_selection.surface_target_origin`. A
        canary admits the program on this origin only. An admission policy
        names it in ``allowed_hosts`` the same way (``macos://textedit``); a
        bare host there names a web app only."""

        from openadapt_flow.surface_selection import surface_target_origin

        origin = surface_target_origin(self.backend)
        assert origin is not None
        return origin

    @property
    def environment(self) -> str:
        """As for :attr:`WebApp.environment`."""

        return _environment(self.origin, self.environment_id)

    @property
    def backend_hints(self) -> Optional[dict[str, Any]]:
        if self.kind not in ("rdp", "citrix"):
            return None
        return {
            "rdp_window": self.backend.rdp_window,
            "rdp_window_title": self.backend.rdp_window_title,
            "rdp_readiness_text": self.backend.rdp_readiness_text,
        }

    @property
    def deployment_surface(self) -> Optional[str]:
        return self.backend.deployment_surface

    def run_refusal(self) -> Optional[str]:
        """Why a governed canary or run can't start here, or None."""

        if (
            self.kind == "citrix"
            and not str(self.backend.rdp_readiness_text or "").strip()
        ):
            return (
                "governed Citrix execution needs a stable current-frame "
                "readiness marker before any action: set backend.rdp_readiness_text"
            )
        return None

    def open(self) -> tuple[Any, Callable[[], None]]:
        """Build a fresh backend from the section. Returns it and its close."""

        if self.open_backend is not None:
            backend = self.open_backend(self.backend)
        else:
            from openadapt_flow.backends.factory import build_backend

            backend = build_backend(self.backend)
        close = getattr(backend, "close", None)
        return backend, (close if callable(close) else (lambda: None))


#: What :func:`compile_agent` runs a task in.
App = WebApp | SurfaceApp


def _environment(origin: str, environment_id: Any) -> str:
    """``origin`` or ``origin#id``. A callable id is read now; one that fails
    or returns nothing raises, so an unknown environment never matches."""

    value = environment_id() if callable(environment_id) else environment_id
    if value is None:
        return origin
    text = str(value).strip()
    if not text:
        raise ValueError("the app's environment_id is empty")
    return f"{origin}#{text}"


@dataclass(frozen=True)
class TaskRequest:
    """What the agent is asked to do on a cache miss."""

    task_id: str
    instruction: str
    inputs: dict[str, Any]
    #: The customer's key for the agent's model client, when the agent calls
    #: a model. Never an OpenAdapt-held key.
    model_key: Optional[CustomerModelKey] = field(default=None, repr=False)


class ComputerUseAgent(Protocol):
    """A CUA loop. ``dialect`` names its action format (see ``DIALECTS``).

    ``run`` drives the task to the end, sending every action through
    ``computer.execute`` and giving the model the returned observation.

    An agent that makes no model calls (a recorded trajectory) sets
    ``requires_model_key = False``. Any other agent is recorded only with
    the customer's own key. An agent may set ``principal_id``; that identity
    can never review or admit what it recorded.
    """

    dialect: str

    def run(self, request: TaskRequest, computer: CaptureComputer) -> None: ...


@dataclass
class JitResult:
    """The outcome of one :meth:`CompiledAgent.run`.

    ``status`` is ``needs_human_admit`` (a candidate waits for a person), the
    run's execution outcome (``VERIFIED``, ``HALTED``, ...), ``refused`` (the
    run gate or the cache check refused; nothing ran), ``unsupported`` (the
    agent used an action the recording can't hold), ``scope_refused`` (the
    agent went outside the task's declared teach scope), ``readback_refused``
    (after the agent's run, the system of record did not show exactly the
    declared write), or ``capture_failed``.
    """

    status: str
    task_key: str
    cache: str  # "miss" | "pending" | "hit"
    agent_called: bool
    model_calls: Optional[int] = None
    run_dir: Optional[Path] = None
    report: Any = None
    candidate: Optional[dict[str, Any]] = None
    inbox_item: Optional[Path] = None
    detail: Optional[str] = None
    halt_reason: Optional[str] = None

    @property
    def verified(self) -> bool:
        return self.status == "VERIFIED"


class Recording:
    """One capture, as a context manager. Get it from
    :meth:`CompiledAgent.recording`.

    Inside the ``with`` block, send each action to ``dispatcher.execute`` and
    give the agent the observation. The dispatcher is the action boundary for
    external agents; capture controllers may register per-action and
    pre-finish checks on it. On exit the recording is compiled into a
    DRAFT bundle, and :attr:`result` says what happened. An
    :class:`UnsupportedAction` raised in the block ends the capture with no
    draft and is not re-raised; any other exception is recorded as
    ``capture_failed`` and re-raised.
    """

    def __init__(
        self,
        owner: "CompiledAgent",
        params: Mapping[str, Any],
        values: dict[str, str],
        dialect: str,
    ) -> None:
        self._owner = owner
        self._params = dict(params)
        self._values = values
        self._dialect = dialect
        self._close: Optional[Callable[[], None]] = None
        self._session: Any = None
        self.computer: Optional[CaptureComputer] = None
        self.request: Optional[TaskRequest] = None
        self.result: Optional[JitResult] = None
        #: The teach scope's guard, when the task declared one.
        self.guard: Optional["TeachGuard"] = None
        self.dispatcher: Optional[RecordingDispatcher] = None
        self._baseline: Optional[list[dict[str, Any]]] = None

    def __enter__(self) -> "Recording":
        from openadapt_flow.authoring import AuthoringSession
        from openadapt_flow.backends._owned import close_owned

        owner = self._owner
        if owner.admitted or owner.candidate() is not None:
            raise AdmissionError(
                "this task already has a draft or an admitted program; reject the "
                "draft to record again"
            )
        owner._check_model_key()
        _print_warning(RECORDING_WARNING)
        if owner.on_warning is not None:
            owner.on_warning(RECORDING_WARNING)
        program = owner.program_dir
        if program.exists():
            shutil.rmtree(program)
        program.mkdir(parents=True)
        self.request = TaskRequest(
            task_id=owner.task.task_id,
            instruction=owner.task.render_instruction(self._params),
            inputs=dict(self._params),
            model_key=owner.model_key,
        )
        try:
            scope = owner._teach_scope(self._params)
        except Exception:
            # An authorization that does not verify or fit leaves no draft.
            shutil.rmtree(program, ignore_errors=True)
            raise
        if scope is not None:
            from openadapt_flow.teach_scope import TeachGuard

            self.guard = TeachGuard(scope)
        if owner.app.reset is not None:
            owner.app.reset()
        settle: dict[str, Any] = {}
        capture_settle = getattr(owner.app, "capture_settle_timeout_s", None)
        if capture_settle is not None:
            settle["settle_timeout_s"] = capture_settle
        if self.guard is not None and isinstance(owner.app, WebApp):
            backend, self._close = owner.app.open(before_goto=self.guard.install)
        else:
            backend, self._close = owner.app.open()
        try:
            if owner.before_capture is not None:
                owner.before_capture(backend)
            if self.guard is not None and owner.app.record_reader is not None:
                self._baseline = owner.app.record_reader()
            self._session = AuthoringSession(
                backend,
                program / "recording",
                backend_kind=owner.app.kind,
                app_url=owner.app.url,
                backend_hints=owner.app.backend_hints,
                system_of_record_reader=owner.app.record_reader,
                **settle,
            )
            self.computer = CaptureComputer(
                self._session,
                dialect=self._dialect,
                page=getattr(backend, "page", None)
                if owner.app.kind == "web"
                else None,
                sleep=owner._sleep,
                guard=self.guard,
                after_action=self._dispatch_after_action,
            )
            self.dispatcher = RecordingDispatcher(self)
            self.computer.dispatcher = self.dispatcher
            self._session.start_record()
        except BaseException as error:
            callbacks = [self._close]
            if self.dispatcher is not None:
                callbacks.insert(0, self.dispatcher.close)
            close_owned(callbacks, primary=error)
            raise
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool:
        from openadapt_flow.backends._owned import close_owned, note_failure

        assert self.computer is not None and self._close is not None
        callbacks = [self._close]
        if self.dispatcher is not None:
            callbacks.insert(0, self.dispatcher.close)
        try:
            pending = self._finish(exc)
        except BaseException as error:
            primary = exc if exc is not None else error
            if primary is not error:
                note_failure(primary, error)
            self.result = None
            close_owned(
                [*callbacks, lambda: self._keep_failed_capture("finish", primary)],
                primary=primary,
            )
            raise primary
        suppress = isinstance(exc, UnsupportedAction)
        try:
            close_owned(callbacks, primary=None if suppress else exc)
        except BaseException as error:
            self.result = None
            cleanup_error = error

            def keep_failed_cleanup() -> None:
                self._keep_failed_capture("cleanup", cleanup_error)

            close_owned((keep_failed_cleanup,), primary=cleanup_error)
            raise
        try:
            # Compilation is staged; only completed owned cleanup permits
            # signing and publication as a discoverable normal candidate.
            self.result = pending() if callable(pending) else pending
        except BaseException as error:
            primary = exc if exc is not None else error
            if primary is not error:
                note_failure(primary, error)
            self.result = None
            close_owned(
                (lambda: self._keep_failed_capture("publication", primary),),
                primary=primary,
            )
            raise primary
        return suppress

    def _keep_failed_capture(self, stage: str, error: BaseException) -> None:
        """Retain failed staged/publication data outside normal discovery."""
        if self._owner.program_dir.exists():
            assert self.computer is not None
            self._owner._capture_refused(
                "capture_failed",
                f"capture_{stage}_failed:{type(error).__name__}",
                self.computer,
                guard=self.guard,
            )

    def _finish(
        self, exc: Optional[BaseException]
    ) -> Union[JitResult, Callable[[], JitResult]]:
        from openadapt_flow.authoring import AuthoringError
        from openadapt_flow.teach_scope import ScopeViolation

        owner, session, computer = self._owner, self._session, self.computer
        assert computer is not None
        guard = self.guard
        if exc is None and self.dispatcher is not None:
            try:
                self.dispatcher.finish()
            except Exception as finish_error:  # noqa: BLE001 - fail closed
                exc = finish_error
        if guard is not None and exc is None:
            # A request the page made after the agent's last action.
            try:
                guard.finish()
            except ScopeViolation as late:
                exc = late
        if guard is not None and exc is None and guard.violations:
            # The agent caught a refusal and went on. A run that tried to
            # leave its scope once never becomes a draft.
            first = guard.violations[0]
            exc = ScopeViolation(first["code"], first["detail"])
        if isinstance(exc, ScopeViolation):
            session.halt()
            return owner._capture_refused(
                SCOPE_REFUSED, str(exc), computer, guard=guard
            )
        if isinstance(exc, UnsupportedAction):
            session.halt()
            return owner._capture_refused("unsupported", str(exc), computer)
        if exc is not None:
            session.halt()
            return owner._capture_refused(
                "capture_failed", f"{type(exc).__name__}: {exc}", computer
            )
        if computer.actions == 0:
            session.halt()
            return owner._capture_refused(
                "capture_failed", "the agent performed no action", computer
            )
        readback = None
        if guard is not None:
            # Flow's own read-back, independent of anything the agent said.
            # A run the agent was told to "report as verified" is judged here.
            after = (
                owner.app.record_reader()
                if owner.app.record_reader is not None
                else None
            )
            readback = guard.check_delta(self._baseline, after)
            if not readback.verified and (
                owner.app.record_reader is not None or readback.status != "unreadable"
            ):
                session.halt()
                return owner._capture_refused(
                    READBACK_REFUSED,
                    f"the record read-back after the agent's run is {readback.status}"
                    + (
                        f" ({', '.join(readback.problems)})"
                        if readback.problems
                        else ""
                    )
                    + "; the declared write is not what the system of record shows",
                    computer,
                    guard=guard,
                    readback=readback,
                )
        try:
            compiled = session.compile(
                owner.bundle_dir,
                name=owner.task.task_id,
                distill=True,
                mine_effects=owner.app.record_reader is not None,
            )
        except AuthoringError as exc2:
            return owner._capture_refused("capture_failed", str(exc2), computer)
        compiled_dir = session.compiled_recording_dir or session.recording_dir
        if compiled_dir is not None:
            # The agent looked the record up after saving: propose that
            # look-up as the check, beside the recording that compiles (the
            # distilled copy, whose event numbers the recompile reads).
            from openadapt_flow.compiler.lookup_detect import propose_from_recording

            _proposal, _written, lines = propose_from_recording(
                compiled_dir, session_kind="web", values=self._values
            )
            for line in lines:
                if owner.on_warning is not None:
                    owner.on_warning(line)
        return lambda: owner._write_draft(
            session,
            compiled,
            computer,
            self._values,
            self._dialect,
            guard=guard,
            readback=readback,
        )

    def _dispatch_after_action(self, native: Mapping[str, Any]) -> None:
        if self.dispatcher is not None:
            self.dispatcher.after_action(native)


class RecordingDispatcher:
    """The capture-owned action interface for an external agent.

    Pass only this object to an external agent. Each call to :meth:`execute`
    goes through the recording computer and its pre-action teach guard. A
    capture controller can register request-pump and completion callbacks;
    those run before the observation is taken and before the recording is
    finalized, respectively.
    """

    def __init__(self, recording: Recording) -> None:
        self._recording = recording
        self._after_action: list[Callable[[Mapping[str, Any]], None]] = []
        self._finalizers: list[Callable[[], None]] = []
        self._closers: list[Callable[[], None]] = []
        self._finish_state: Literal["open", "running", "complete", "failed"] = "open"
        self._finish_error: Optional[BaseException] = None
        self._close_error: Optional[BaseException] = None
        self._closed = False

    @property
    def recording(self) -> Recording:
        """The owning capture, for trusted capture controllers."""

        return self._recording

    def execute(self, native: Mapping[str, Any]) -> Any:
        """Perform one action under the live recording's guard."""

        if (
            self._closed
            or self._finish_state != "open"
            or self._recording.computer is None
        ):
            raise RuntimeError("the recording action dispatcher is closed")
        return self._recording.computer.execute(native)

    def add_after_action(self, callback: Callable[[Mapping[str, Any]], None]) -> None:
        """Run a capture controller after each guarded operation."""

        if self._closed or self._finish_state != "open":
            raise RuntimeError("the recording action dispatcher is finishing")
        self._after_action.append(callback)

    def add_settle_callback(self, callback: Callable[[float], None]) -> None:
        """Pump a guard while the recorder samples an action's settled result."""

        if self._closed or self._finish_state != "open":
            raise RuntimeError("the recording action dispatcher is finishing")
        if self._recording._session is None:
            raise RuntimeError("the recording session has not started")
        self._recording._session.add_settle_callback(callback)

    def before_finish(self, callback: Callable[[], None]) -> None:
        """Require capture-side checks to finish before a draft is written."""

        if self._closed or self._finish_state != "open":
            raise RuntimeError("the recording action dispatcher is finishing")
        self._finalizers.append(callback)

    def on_close(self, callback: Callable[[], None]) -> None:
        """Release a capture controller on both success and refusal."""

        if self._closed:
            raise RuntimeError("the recording action dispatcher is closed")
        self._closers.append(callback)

    def after_action(self, native: Mapping[str, Any]) -> None:
        """Internal action boundary used by :class:`Recording`."""

        if self._closed or self._finish_state != "open":
            raise RuntimeError("the recording action dispatcher is closed")
        for callback in tuple(self._after_action):
            callback(native)

    def finish(self) -> None:
        """Complete checks once; a caught refusal never authorizes a draft."""

        if self._finish_state == "failed":
            assert self._finish_error is not None
            raise self._finish_error
        if self._finish_state == "complete":
            return
        if self._finish_state == "running" or self._closed:
            self._finish_error = RuntimeError(
                "recording completion was reentered or its dispatcher closed"
            )
            self._finish_state = "failed"
            raise self._finish_error
        self._finish_state = "running"
        try:
            for callback in tuple(self._finalizers):
                callback()
                # A callback may catch a reentrant finish refusal. It still
                # cannot turn that failed completion back into success.
                if self._finish_error is not None:
                    raise self._finish_error
                if self._closed:
                    raise RuntimeError(
                        "the recording dispatcher closed during completion"
                    )
        except BaseException as error:
            if self._finish_error is None:
                self._finish_error = error
            self._finish_state = "failed"
            raise self._finish_error
        self._finish_state = "complete"

    def close(self) -> None:
        if self._closed:
            if self._close_error is not None:
                raise self._close_error
            return
        self._closed = True
        from openadapt_flow.backends._owned import close_owned

        try:
            close_owned(reversed(self._closers))
        except BaseException as error:
            self._close_error = error
            raise


@dataclass
class CompiledAgent:
    """A CUA wrapped in a compile-and-cache layer. Build with :func:`compile_agent`."""

    agent: Optional[ComputerUseAgent]
    task: TaskSignature
    app: App
    verifier: Any
    policy: str
    cache_dir: Path
    dialect: str = "generic"
    on_halt: Optional[Callable[[JitResult], None]] = None
    model_key: Optional[CustomerModelKey] = field(default=None, repr=False)
    key_dir: Optional[Path] = None
    on_warning: Optional[Callable[[str], None]] = None
    require_distinct_reviewer_and_admitter: bool = False
    teach_scope: Optional[TeachScopeSource] = None
    #: A person's signed teach authorization
    #: (:class:`~openadapt_flow.teach_authorization.TeachAuthority`). With
    #: one, the recording's scope is bound from it; ``teach_scope`` may only
    #: narrow it. Without one the draft can't be admitted.
    teach_authorization: Any = None
    #: Trusted process-local preparation/preflight on the capture backend.
    #: Not a canary/replay hook, an approval or a serialized task option.
    before_capture: Optional[Callable[[Any], None]] = field(default=None, repr=False)
    _sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    # Enabled only by the compile-agent task loader, never a raw bundle path.
    _use_skill_repair: bool = field(default=False, init=False, repr=False)

    # -- layout --------------------------------------------------------------

    @property
    def task_dir(self) -> Path:
        return self.cache_dir / self.task.key

    @property
    def program_dir(self) -> Path:
        return self.task_dir / "program"

    @property
    def bundle_dir(self) -> Path:
        if self._use_skill_repair:
            from openadapt_flow.compile_agent.repair_selection import selected_bundle

            selected = selected_bundle(self)
            if selected is not None:
                return selected
        return self.program_dir / "bundle"

    @property
    def runs_dir(self) -> Path:
        return self.task_dir / "runs"

    @property
    def inbox_dir(self) -> Path:
        return self.cache_dir / "inbox"

    def candidate(self) -> Optional[dict[str, Any]]:
        path = self.program_dir / CANDIDATE_FILE
        return json.loads(path.read_text()) if path.is_file() else None

    @property
    def admitted(self) -> bool:
        """True when the bundle carries an admission manifest. A run still
        verifies its signature and digest before anything executes."""

        return (self.bundle_dir / ADMIT_FILE).is_file()

    def state(self) -> Optional[lifecycle.BundleState]:
        """DRAFT, ADMITTED, or ``None`` when nothing is recorded.

        Raises:
            AdmissionError: an admission manifest that does not verify.
        """

        return lifecycle.state_of(self.program_dir, key_dir=self.key_dir)

    def status(self) -> dict[str, Any]:
        """The draft's steps and which admission requirements it meets."""

        return lifecycle.admission_status(
            self.program_dir, key_dir=self.key_dir, trust=self.teach_authorization
        )

    # -- run -----------------------------------------------------------------

    def run(self, /, **params: Any) -> JitResult:
        """Run the task with ``params``.

        Raises:
            ParamError: ``params`` do not match the task's schema.
        """

        values = self.task.validate(params)
        if self.admitted:
            return self._run_admitted(values)
        if self.candidate() is not None:
            return JitResult(
                status=NEEDS_HUMAN_ADMIT,
                task_key=self.task.key,
                cache="pending",
                agent_called=False,
                candidate=self.candidate(),
                detail=(
                    "a DRAFT is waiting for a person; a draft never runs unattended "
                    "(review, canary, admit)"
                ),
            )
        if self.agent is None:
            return JitResult(
                status="needs_recording",
                task_key=self.task.key,
                cache="miss",
                agent_called=False,
                detail="no agent to record with; use recording() around your loop",
            )
        return self._explore(params, values)

    def recording(self, /, **params: Any) -> Recording:
        """Record your own CUA loop: ``with jit.recording(note="x") as rec:``.

        ``rec.request`` is what to ask the model, ``rec.computer.execute``
        performs an action, and ``rec.result`` holds the draft after the
        block. The task must have no draft and no admitted program.

        Raises:
            ParamError: ``params`` do not match the task's schema.
            MissingModelKey: a model-driven agent and no customer key.
        """

        values = self.task.validate(params)
        dialect = getattr(self.agent, "dialect", None) or self.dialect
        return Recording(self, params, values, dialect)

    # -- cache miss ----------------------------------------------------------

    def _teach_scope(self, params: Mapping[str, Any]) -> Optional[TeachScope]:
        """The scope this recording runs under, checked against the task."""

        from openadapt_flow.teach_scope import TeachScope

        source = self.teach_scope
        authority = self.teach_authorization
        if source is None and authority is None:
            return None
        authorized = None
        if authority is not None:
            from openadapt_flow import teach_authorization as ta

            # Verify the copy kept beside the draft, so it is the one admitted.
            kept = authority.kept_in(
                self.program_dir / lifecycle.TEACH_AUTHORIZATION_DIR
            )
            authorized = kept.scope(
                purpose="teach", task_id=self.task.task_id, params=params
            )
        if source is None:
            scope = authorized
        else:
            scope = source if isinstance(source, TeachScope) else source(dict(params))
        if not isinstance(scope, TeachScope):
            raise ValueError("teach_scope must be a TeachScope or return one")
        if authorized is not None and scope is not authorized:
            scope = ta.narrowed(scope, authorized)
        if scope.task_id != self.task.task_id:
            raise ValueError(
                f"the teach scope is for task {scope.task_id!r}, not "
                f"{self.task.task_id!r}"
            )
        # Every declared input is a value the scope declares, so the agent
        # can type it. Nothing else is typed.
        missing = sorted(
            name
            for name, value in params.items()
            if value is not None and str(value) not in scope.typeable
        )
        if missing:
            raise ValueError(
                "the teach scope does not declare the task's input(s) "
                + ", ".join(missing)
            )
        if self.app.record_reader is None and scope.write_adapter is None:
            raise ValueError(
                "a scoped recording needs the app's record_reader (for the "
                "read-back) or a write adapter (to check writes before they go)"
            )
        return scope

    def _check_model_key(self) -> None:
        from openadapt_flow.jit.keys import MissingModelKey

        if self.model_key is not None:
            return
        if (
            self.agent is not None
            and getattr(self.agent, "requires_model_key", True) is False
        ):
            return
        raise MissingModelKey(
            "recording a model-driven agent needs the customer's own model key: "
            "pass model_key=load_customer_model_key(...) to compile_agent. "
            "OpenAdapt does not supply one."
        )

    def _explore(self, params: Mapping[str, Any], values: dict[str, str]) -> JitResult:
        agent = self.agent
        assert agent is not None
        rec = self.recording(**params)
        try:
            with rec:
                assert rec.request is not None and rec.computer is not None
                agent.run(rec.request, rec.computer)
        except Exception:
            # The agent's own failure is a capture_failed result. A failure
            # before the capture started (no key, no browser) is raised.
            if rec.result is None:
                raise
        assert rec.result is not None
        return rec.result

    def _write_draft(
        self,
        session: Any,
        compiled: Mapping[str, Any],
        computer: CaptureComputer,
        values: dict[str, str],
        dialect: str,
        *,
        guard: Optional[TeachGuard] = None,
        readback: Any = None,
    ) -> JitResult:
        program = self.program_dir
        lifecycle.mark_draft(self.bundle_dir)
        lifted = lift_params(self.bundle_dir, values)
        candidate = {
            "state": lifecycle.BundleState.DRAFT.value,
            "status": NEEDS_HUMAN_ADMIT,
            "task": self.task.to_dict(),
            "workflow_id": compiled["workflow_id"],
            "demonstrated_by": "agent",
            "recorded_by": f"agent:{dialect}",
            "agent_principal_id": getattr(self.agent, "principal_id", None),
            "dialect": dialect,
            "policy": self.policy,
            "mine_effects": self.app.record_reader is not None,
            "model_key": self.model_key.provenance() if self.model_key else None,
            "sensitive_data_warning": {
                "text": RECORDING_WARNING,
                "shown_on": ["stderr"] + (["on_warning"] if self.on_warning else []),
            },
            "require_distinct_reviewer_and_admitter": (
                self.require_distinct_reviewer_and_admitter
            ),
            "recording": str(session.recording_dir),
            "compiled_recording": str(session.compiled_recording_dir),
            "bundle": str(self.bundle_dir),
            "distilled": compiled.get("distilled"),
            "params": lifted.to_dict(),
            "effects": _effect_summary(self.bundle_dir),
            "agent_actions": computer.actions,
        }
        if guard is not None:
            # What the agent was allowed to touch, what was blocked, and what
            # the system of record showed. Names and digests only.
            candidate["teach_scope"] = {
                **guard.report(),
                "readback": readback.to_dict() if readback is not None else None,
            }
            if guard.scope.authorization is not None:
                # Who signed the scope; the statement is kept beside the
                # draft (lifecycle.TEACH_AUTHORIZATION_DIR) for admission.
                candidate["teach_authorization"] = dict(guard.scope.authorization)
        # Who made the recording, signed with the bundle digest, so editing
        # candidate.json can't turn the recorder into someone else.
        candidate = lifecycle.sign_recorder(program, candidate, key_dir=self.key_dir)
        (program / "capture.json").write_text(json.dumps(computer.log, indent=2) + "\n")
        # Stage the signed record, then finish fallible retention before the
        # one atomic publication. A failed event never exposes a candidate.
        staged = program / f"candidate-pending-{uuid.uuid4().hex}.json"
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(candidate, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        result = JitResult(
            status=NEEDS_HUMAN_ADMIT,
            task_key=self.task.key,
            cache="miss",
            agent_called=self.agent is not None,
            candidate=candidate,
        )
        lifecycle._event(program, "draft", recorded_by=candidate["recorded_by"])
        staged.replace(program / CANDIDATE_FILE)
        return result

    def _capture_refused(
        self,
        status: str,
        detail: str,
        computer: CaptureComputer,
        *,
        guard: Optional[TeachGuard] = None,
        readback: Any = None,
    ) -> JitResult:
        program = self.program_dir
        (program / "capture.json").write_text(json.dumps(computer.log, indent=2) + "\n")
        refused: dict[str, Any] = {"status": status, "detail": detail}
        if guard is not None:
            refused["teach_scope"] = {
                **guard.report(),
                "readback": readback.to_dict() if readback is not None else None,
            }
        (program / "refused.json").write_text(json.dumps(refused, indent=2) + "\n")
        kept = self.task_dir / f"refused-{_stamp()}"
        program.rename(kept)
        return JitResult(
            status=status,
            task_key=self.task.key,
            cache="miss",
            agent_called=self.agent is not None,
            detail=f"{detail} (capture kept at {kept})",
        )

    # -- DRAFT to ADMITTED -------------------------------------------------------

    def confirm_params(
        self,
        *,
        principal_id: Any,
        accept_params: Sequence[str],
        confirm: object = None,
        idempotency_key: Optional[str | Sequence[str]] = None,
    ) -> dict[str, Any]:
        """Confirm the task's parameters before the review. Returns the step
        ids of the recompiled program. ``idempotency_key`` names the parameter
        that identifies one saved record, or a list of parameters that
        identify it together. See
        :func:`openadapt_flow.jit.lifecycle.confirm_params`."""

        return lifecycle.confirm_params(
            self.program_dir,
            principal_id=principal_id,
            accept_params=accept_params,
            confirm=confirm,
            key_dir=self.key_dir,
            idempotency_key=idempotency_key,
        )

    def review(
        self,
        *,
        principal_id: str,
        reviewed_steps: Sequence[str],
        confirm: object = None,
    ) -> dict[str, Any]:
        """Record that ``principal_id`` reviewed every step. See
        :func:`openadapt_flow.jit.lifecycle.record_review`."""

        return lifecycle.record_review(
            self.program_dir,
            principal_id=principal_id,
            reviewed_steps=reviewed_steps,
            confirm=confirm,
            key_dir=self.key_dir,
        )

    def canary(self, /, *, principal_id: Any, **params: Any) -> JitResult:
        """Run a canary with keyword data; use canary_params for control-name keys."""

        return self.canary_params(params, principal_id=principal_id)

    def canary_params(
        self, params: Mapping[str, Any], /, *, principal_id: Any
    ) -> JitResult:
        """Run the draft once on the target app, attended.

        ``principal_id`` is a person's name, or a registered
        :class:`~openadapt_flow.jit.authority.Principal`: an admitter key, or
        an agent under a signed admission policy. A person's canary needs
        their review of the current bundle first. An agent's canary needs the
        draft and this app inside its policy; otherwise the draft goes to the
        ``needs_person`` queue and nothing runs.

        Use a test record. The canary refuses to run unless every write step
        has an independent read-back through this agent's verifier. It passes
        only when the run ends ``VERIFIED`` with zero model calls. A failed
        canary goes to the inbox; the agent is not called.
        """

        from openadapt_flow.ir import Workflow
        from openadapt_flow.jit import authority

        values = self.task.validate(params)
        candidate = self.candidate()
        if candidate is None or self.admitted:
            raise AdmissionError("a canary runs a DRAFT; there is none")
        if (
            self.task.environment is not None
            and self.task.environment != self.app.environment
        ):
            raise AdmissionError(
                f"the task is pinned to environment {self.task.environment!r}; "
                f"this app reports {self.app.environment!r}"
            )
        _, principal = lifecycle.resolve_actor(
            principal_id, self.program_dir, key_dir=self.key_dir
        )
        workflow = Workflow.load(self.bundle_dir)
        digest = lifecycle._digest(workflow)
        review = lifecycle._evidence_at(
            self.program_dir / lifecycle.REVIEW_FILE, digest
        )
        if principal is None and review is None:
            raise AdmissionError("review the draft before its canary")
        if principal is not None and principal.kind == "agent":
            assert principal.policy is not None
            out = authority.scope_problems(
                principal.policy.policy, workflow, candidate, self.app.origin
            )
            if out:
                lifecycle._write(
                    self.program_dir / lifecycle.NEEDS_PERSON_FILE,
                    {
                        "at": lifecycle._now(),
                        "digest": digest,
                        "agent_principal_id": principal.principal_id,
                        "policy_id": principal.policy.policy.policy_id,
                        "policy_digest": principal.policy.policy.digest(),
                        "reasons": out,
                    },
                )
                raise AdmissionError(
                    "outside the agent's admission policy; queued for a person: "
                    + "; ".join(out)
                )
        readback = lifecycle.readback_coverage(workflow, self.verifier)
        problems = lifecycle._readback_problems(readback)

        def finish(
            report: Any, run_dir: Optional[Path], detail: Optional[str]
        ) -> JitResult:
            record = lifecycle.record_canary(
                self.program_dir,
                principal_id=principal_id,
                digest=digest,
                report=report,
                readback=readback,
                target_origin=self.app.origin,
                environment=self.app.environment,
                run_dir=run_dir,
                key_dir=self.key_dir,
            )
            result = JitResult(
                status=record["outcome"],
                task_key=self.task.key,
                cache="canary",
                agent_called=False,
                model_calls=record["model_calls"],
                run_dir=run_dir,
                report=report,
                detail=detail,
                halt_reason=(
                    report.halt.reason
                    if report is not None and report.halt is not None
                    else None
                ),
            )
            if not record["passed"]:
                result = self._to_inbox(result)
            return result

        if problems:
            return finish(
                None,
                None,
                "no canary ran: a write step lacks an independent read-back: "
                + "; ".join(problems),
            )
        run_dir = self.runs_dir / f"canary-{_stamp()}-{uuid.uuid4().hex[:8]}"
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        gate_detail, report = self._run_governed(
            workflow, digest, values, run_dir, attended_by=principal_id
        )
        return finish(report, run_dir if report is not None else None, gate_detail)

    def admit(self, *, principal_id: str, confirm: object = None) -> dict[str, Any]:
        """A person admits the draft at an interactive terminal; headless, it
        raises (use :meth:`admit_with_key`). Never called by ``run``. See
        :func:`openadapt_flow.jit.lifecycle.admit`."""

        return lifecycle.admit(
            self.program_dir,
            principal_id=principal_id,
            confirm=confirm,
            key_dir=self.key_dir,
            require_distinct_reviewer_and_admitter=(
                self.require_distinct_reviewer_and_admitter
            ),
            trust=self.teach_authorization,
        )

    def admit_with_key(self, principal: Any) -> dict[str, Any]:
        """Admit with a registered admitter key, or as an agent under a signed
        admission policy. Returns ``{"outcome": "admitted" | "needs_person"}``.
        See :func:`openadapt_flow.jit.lifecycle.admit_with_key`."""

        return lifecycle.admit_with_key(
            self.program_dir,
            principal=principal,
            key_dir=self.key_dir,
            # The roster pin the teach authorization was checked under.
            trust=self.teach_authorization,
        )

    def reject(self, *, principal_id: Any, reason: str = "") -> Path:
        """Set the draft aside; the next run records again."""

        return lifecycle.reject(
            self.program_dir,
            principal_id=principal_id,
            reason=reason,
            key_dir=self.key_dir,
        )

    # -- cache hit -----------------------------------------------------------

    def _run_admitted(
        self, values: dict[str, str], *, bundle_dir: Optional[Path] = None
    ) -> JitResult:
        from openadapt_flow.ir import Workflow

        def bundle() -> Path:
            return self.bundle_dir if bundle_dir is None else Path(bundle_dir)

        def refused(detail: str) -> JitResult:
            return self._to_inbox(
                JitResult(
                    status="refused",
                    task_key=self.task.key,
                    cache="hit",
                    agent_called=False,
                    detail=detail,
                )
            )

        try:
            record = lifecycle.verify_admission(bundle(), key_dir=self.key_dir)
            workflow = Workflow.load(bundle())
        except Exception as exc:  # noqa: BLE001 - any doubt is a refusal
            return refused(f"the admission does not hold: {exc}")
        if bundle_dir is not None and record.get("admission_mode") != "repair":
            return refused("an explicit repair canary requires normal repair admission")
        # Pin what was admitted, not what is on disk now: the bundle is loaded
        # again for the run, and must still be the admitted one.
        digest = str(record["digest"])
        if lifecycle._digest(workflow) != digest:
            return refused("the bundle changed after its admission was checked")
        if (record.get("task") or {}).get("key") != self.task.key:
            return refused("the admission is for a different task signature")
        if sorted(workflow.params) != sorted(self.task.params):
            return refused("the admitted program's parameters do not match the task")
        if record.get("policy") != self.policy:
            return refused(
                f"admitted under policy {record.get('policy')!r}, asked to run "
                f"under {self.policy!r}"
            )
        canary_origin = (record.get("canary") or {}).get("target_origin")
        if canary_origin != self.app.origin:
            return refused(
                f"the canary ran on {canary_origin}, not on {self.app.origin}; "
                "admit a canary on this app"
            )
        bound = _reuse_refusal(record, workflow, self.task, self.app)
        if bound is not None:
            return refused(bound)
        problems = lifecycle._readback_problems(
            lifecycle.readback_coverage(workflow, self.verifier)
        )
        if problems:
            return refused(
                "this verifier gives a write step no independent read-back: "
                + "; ".join(problems)
            )
        run_dir = self.runs_dir / f"{_stamp()}-{uuid.uuid4().hex[:8]}"
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        if bundle_dir is None:
            gate_detail, report = self._run_governed(workflow, digest, values, run_dir)
        else:
            gate_detail, report = self._run_governed(
                workflow, digest, values, run_dir, bundle_dir=bundle_dir
            )
        if report is None:
            return refused(gate_detail or "the run gate refused")
        result = JitResult(
            status=str(report.execution_outcome),
            task_key=self.task.key,
            cache="hit",
            agent_called=False,
            model_calls=int(report.model_calls),
            run_dir=run_dir,
            report=report,
            halt_reason=report.halt.reason if report.halt is not None else None,
        )
        if result.model_calls:
            result.detail = (
                f"the replay made {result.model_calls} model calls; a healthy replay "
                "makes none"
            )
            result.status = "needs_review"
        if result.status != "VERIFIED":
            result = self._to_inbox(result)
        return result

    def _run_governed(
        self,
        workflow: Any,
        digest: str,
        values: dict[str, str],
        run_dir: Path,
        *,
        attended_by: Any = None,
        bundle_dir: Optional[Path] = None,
    ) -> tuple[Optional[str], Any]:
        """The standard-profile run: gate, authorization, Replayer, verifier.

        Runs an admitted bundle, or a draft only when ``attended_by`` names
        the person attending its canary.
        """

        from openadapt_flow.deployment import DeploymentConfig, PolicySection
        from openadapt_flow.execution_profiles import (
            ExecutionProfile,
            execution_profile_contract,
        )
        from openadapt_flow.jit import authority
        from openadapt_flow.run_gate import (
            build_runtime_authorization,
            evaluate_run_gate,
        )
        from openadapt_flow.runtime import Replayer

        def bundle() -> Path:
            return self.bundle_dir if bundle_dir is None else Path(bundle_dir)

        if attended_by is None:
            try:
                lifecycle.verify_admission(bundle(), key_dir=self.key_dir)
            except AdmissionError as exc:
                return f"a DRAFT never runs unattended: {exc}", None
        refusal = getattr(self.app, "run_refusal", lambda: None)()
        if refusal:
            return f"refused before any action: {refusal}", None
        gate = evaluate_run_gate(
            workflow,
            bundle_dir=bundle(),
            deployment=DeploymentConfig(policy=PolicySection(policy=self.policy)),
            effect_verifier=self.verifier,
            profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
            effective_durable=True,
            effective_require_settled=True,
            pinned_content_digest=digest,
        )
        if not gate.passed:
            return f"the standard run gate refused; nothing ran:\n{gate.render()}", None
        authorization = build_runtime_authorization(
            workflow,
            gate,
            approval_source=(
                f"openadapt-flow-jit-canary:{authority.describe(attended_by)}"
                if attended_by
                else "openadapt-flow-jit"
            ),
            params=values,
        )
        if self.app.reset is not None:
            self.app.reset()
        backend, close = self.app.open()
        try:
            report = Replayer(
                backend,
                effect_verifier=self.verifier,
                governed_authorization=authorization,
                durable=True,
                require_settled=True,
                attended_draft_by=attended_by,
                draft_key_dir=self.key_dir,
                deployment_surface=self.app.deployment_surface,
            ).run(
                workflow.model_copy(deep=True),
                params=values,
                bundle_dir=bundle(),
                run_dir=run_dir,
                execution_target_kind=self.app.kind,
                execution_origin=self.app.origin if self.app.kind == "web" else None,
                execution_entry_url=self.app.entry_url,
                execution_target_origin=(
                    None if self.app.kind == "web" else self.app.origin
                ),
            )
        finally:
            close()
        return None, report

    def _to_inbox(self, result: JitResult) -> JitResult:
        """Hand a non-VERIFIED run to a person. The agent is not called."""

        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        item = self.inbox_dir / f"{_stamp()}-{uuid.uuid4().hex[:8]}.json"
        item.write_text(
            json.dumps(
                {
                    "task": self.task.to_dict(),
                    "status": result.status,
                    "halt_reason": result.halt_reason,
                    "detail": result.detail,
                    "run_dir": str(result.run_dir) if result.run_dir else None,
                    "attend": (
                        f"openadapt-flow console --runs {self.runs_dir} "
                        f"--bundles {self.program_dir} --attend"
                    ),
                },
                indent=2,
            )
            + "\n"
        )
        result.inbox_item = item
        if self.on_halt is not None:
            self.on_halt(result)
        return result


def _effect_summary(bundle_dir: Path) -> list[dict[str, Any]]:
    from openadapt_flow.ir import Workflow

    workflow = Workflow.load(bundle_dir)
    return [
        {
            "step_id": step.id,
            "kind": effect.kind.value,
            "needs_operator_confirmation": bool(effect.needs_operator_confirmation),
        }
        for step in workflow.steps
        for effect in step.effects
    ]


def _stamp() -> str:
    return (
        time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"{time.time_ns() % 10**9:09d}"
    )


def compile_agent(
    agent: Optional[ComputerUseAgent] = None,
    *,
    task: TaskSignature,
    app: App,
    verifier: Any,
    policy: str,
    cache_dir: Path | str = ".openadapt-jit",
    on_halt: Optional[Callable[[JitResult], None]] = None,
    dialect: Optional[str] = None,
    model_key: Optional[CustomerModelKey] = None,
    key_dir: Optional[Path | str] = None,
    on_warning: Optional[Callable[[str], None]] = None,
    require_distinct_reviewer_and_admitter: bool = False,
    teach_scope: Optional[TeachScopeSource] = None,
    teach_authorization: Any = None,
    before_capture: Optional[Callable[[Any], None]] = None,
) -> CompiledAgent:
    """Wrap ``agent`` so a task is explored once and then run as a program.

    Args:
        agent: The computer-use agent, used on a cache miss only. Leave it
            out to drive your own loop with :meth:`CompiledAgent.recording`.
        task: The task id, the instruction, and the typed parameter schema.
        app: The app: :class:`WebApp` for a browser app, or
            :class:`SurfaceApp` for a Windows, macOS, Linux, RDP or Citrix
            app named by a deployment ``backend`` section. Either carries the
            read-only system-of-record snapshot used while the agent's run is
            recorded.
        verifier: The effect verifier a run reads the system of record with,
            such as :class:`~openadapt_flow.runtime.effects.RestRecordVerifier`.
        policy: The policy the program must pass to be admitted and run, such
            as ``"clinical-write"``.
        cache_dir: Where candidates, admitted programs, runs and the exception
            inbox live.
        on_halt: Called with every run that does not end ``VERIFIED``, after it
            is written to the inbox.
        dialect: The action format when there is no ``agent`` object
            (default ``generic``). An agent declares its own.
        model_key: The customer's key for the recording run, from
            :func:`~openadapt_flow.jit.keys.load_customer_model_key`. Needed
            unless the agent sets ``requires_model_key = False``.
        key_dir: Where the admission signing key lives (default
            :func:`~openadapt_flow.jit.lifecycle.default_key_dir`).
        on_warning: Also receives the pre-recording warning, for a UI. The
            warning is printed to stderr either way.
        require_distinct_reviewer_and_admitter: Refuse an admission by the
            principal who reviewed the draft. Off by default.
        before_capture: Trusted Python callback receiving the exact owned
            backend after guarded opening and before recording or actor entry.
            Prepare and check the state before reserving a model operation.
            A failure closes the backend. This capture-only callback is not
            persisted and does not run on canaries, replay or cache hits.
        teach_scope: What the recording run may do, declared before the agent
            reads any page: a :class:`~openadapt_flow.teach_scope.TeachScope`,
            or a function from the run's inputs to one. Typed text, links,
            browser requests and writes are checked against it, and the
            system of record is read back after the run. A violation ends the
            capture as ``scope_refused`` or ``readback_refused`` with no
            draft. See ``docs/THREAT_MODEL.md``.
        teach_authorization: A person's signed teach authorization
            (:class:`~openadapt_flow.teach_authorization.TeachAuthority`).
            The recording's scope is bound from it, and ``teach_scope`` may
            only narrow it. Admission refuses an agent-taught draft without
            one.
    """

    if before_capture is not None and not callable(before_capture):
        raise TypeError("before_capture must be callable")
    if agent is not None:
        dialect = getattr(agent, "dialect", None)
        if dialect is None:
            raise ValueError("agent must declare its action dialect")
    dialect = dialect or "generic"
    if dialect not in dialects():
        raise ValueError(f"unknown dialect {dialect!r} (expected one of {dialects()})")
    if dialect == "playwright_mcp" and app.kind != "web":
        raise ValueError(
            "the playwright_mcp dialect targets CSS selectors in a browser page; "
            f"a {app.kind} app has none. Use anthropic, openai or generic "
            "(coordinates and keys)"
        )
    return CompiledAgent(
        agent=agent,
        task=task,
        app=app,
        verifier=verifier,
        policy=policy,
        cache_dir=Path(cache_dir),
        dialect=dialect,
        on_halt=on_halt,
        model_key=model_key,
        key_dir=Path(key_dir) if key_dir is not None else None,
        on_warning=on_warning,
        require_distinct_reviewer_and_admitter=require_distinct_reviewer_and_admitter,
        teach_scope=teach_scope,
        teach_authorization=teach_authorization,
        before_capture=before_capture,
    )
