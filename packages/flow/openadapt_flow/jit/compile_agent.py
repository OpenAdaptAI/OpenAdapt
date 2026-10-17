"""Compile a computer-use agent's run once, then run the program.

``compile_agent`` wraps a computer-use agent (CUA). The first run of a task is
a cache miss: the agent does the task while :class:`CaptureComputer` records
it, Flow compiles the recording, and the result is a CANDIDATE that waits for
a person. Later runs of an admitted task are cache hits: Flow runs the
compiled program under the standard profile, reads the system of record back
through the verifier, and makes no model calls.

What it never does:

* admit a candidate by itself. ``run`` returns ``needs_human_admit``; only
  :meth:`CompiledAgent.admit`, called by a person, admits it. The admission
  also confirms the effect contract the demonstration proposed, and the
  parameters lifted from the task's inputs.
* call the agent on a cache hit, or after a halt. A run that does not end
  ``VERIFIED`` goes to the exception inbox and back to the caller.
* match a task fuzzily. The cache key is the task id and the parameter schema.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol
from urllib.parse import urlsplit

from openadapt_flow.jit.adapters import CaptureComputer, UnsupportedAction
from openadapt_flow.jit.lift import lift_params
from openadapt_flow.jit.signature import TaskSignature

__all__ = [
    "AdmissionError",
    "CompiledAgent",
    "ComputerUseAgent",
    "JitResult",
    "TaskRequest",
    "WebApp",
    "compile_agent",
]

CANDIDATE_FILE = "candidate.json"
ADMIT_FILE = "admit.json"
NEEDS_HUMAN_ADMIT = "needs_human_admit"


class AdmissionError(RuntimeError):
    """The candidate cannot be admitted as it stands."""


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
    """

    url: str
    record_reader: Optional[Callable[[], Optional[list[dict[str, Any]]]]] = None
    reset: Optional[Callable[[], None]] = None
    headless: bool = True

    @property
    def origin(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"


@dataclass(frozen=True)
class TaskRequest:
    """What the agent is asked to do on a cache miss."""

    task_id: str
    instruction: str
    inputs: dict[str, Any]


class ComputerUseAgent(Protocol):
    """A CUA loop. ``dialect`` names its action format (see ``DIALECTS``).

    ``run`` drives the task to the end, sending every action through
    ``computer.execute`` and giving the model the returned observation.
    """

    dialect: str

    def run(self, request: TaskRequest, computer: CaptureComputer) -> None: ...


@dataclass
class JitResult:
    """The outcome of one :meth:`CompiledAgent.run`.

    ``status`` is ``needs_human_admit`` (a candidate waits for a person), the
    run's execution outcome (``VERIFIED``, ``HALTED``, ...), ``refused`` (the
    run gate or the cache check refused; nothing ran), ``unsupported`` (the
    agent used an action the recording can't hold), or ``capture_failed``.
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


@dataclass
class CompiledAgent:
    """A CUA wrapped in a compile-and-cache layer. Build with :func:`compile_agent`."""

    agent: ComputerUseAgent
    task: TaskSignature
    app: WebApp
    verifier: Any
    policy: str
    cache_dir: Path
    on_halt: Optional[Callable[[JitResult], None]] = None
    _sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    # -- layout --------------------------------------------------------------

    @property
    def task_dir(self) -> Path:
        return self.cache_dir / self.task.key

    @property
    def program_dir(self) -> Path:
        return self.task_dir / "program"

    @property
    def bundle_dir(self) -> Path:
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
        return (self.bundle_dir / ADMIT_FILE).is_file()

    # -- run -----------------------------------------------------------------

    def run(self, **params: Any) -> JitResult:
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
                detail="a candidate is waiting for a person to admit or reject it",
            )
        return self._explore(params, values)

    # -- cache miss ----------------------------------------------------------

    def _explore(self, params: Mapping[str, Any], values: dict[str, str]) -> JitResult:
        from openadapt_flow.authoring import AuthoringError, AuthoringSession
        from openadapt_flow.backends.playwright_backend import PlaywrightBackend

        program = self.program_dir
        if program.exists():
            shutil.rmtree(program)
        program.mkdir(parents=True)
        request = TaskRequest(
            task_id=self.task.task_id,
            instruction=self.task.render_instruction(params),
            inputs=dict(params),
        )
        if self.app.reset is not None:
            self.app.reset()
        backend, close = PlaywrightBackend.launch(
            self.app.url, headless=self.app.headless
        )
        session = AuthoringSession(
            backend,
            program / "recording",
            backend_kind="web",
            app_url=self.app.url,
            system_of_record_reader=self.app.record_reader,
        )
        computer = CaptureComputer(
            session, dialect=self.agent.dialect, page=backend.page, sleep=self._sleep
        )
        try:
            session.start_record()
            try:
                self.agent.run(request, computer)
            except UnsupportedAction as exc:
                session.halt()
                return self._capture_refused("unsupported", str(exc), computer)
            except Exception as exc:  # noqa: BLE001 - the agent's own failure
                session.halt()
                return self._capture_refused(
                    "capture_failed", f"{type(exc).__name__}: {exc}", computer
                )
            if computer.actions == 0:
                session.halt()
                return self._capture_refused(
                    "capture_failed", "the agent performed no action", computer
                )
            try:
                compiled = session.compile(
                    self.bundle_dir,
                    name=self.task.task_id,
                    distill=True,
                    mine_effects=self.app.record_reader is not None,
                )
            except AuthoringError as exc:
                return self._capture_refused("capture_failed", str(exc), computer)
        finally:
            close()
        lifted = lift_params(self.bundle_dir, values)
        candidate = {
            "status": NEEDS_HUMAN_ADMIT,
            "task": self.task.to_dict(),
            "workflow_id": compiled["workflow_id"],
            "demonstrated_by": "agent",
            "dialect": self.agent.dialect,
            "recording": str(session.recording_dir),
            "compiled_recording": str(session.compiled_recording_dir),
            "bundle": str(self.bundle_dir),
            "distilled": compiled.get("distilled"),
            "params": lifted.to_dict(),
            "effects": _effect_summary(self.bundle_dir),
            "agent_actions": computer.actions,
        }
        (program / "capture.json").write_text(json.dumps(computer.log, indent=2) + "\n")
        (program / CANDIDATE_FILE).write_text(json.dumps(candidate, indent=2) + "\n")
        return JitResult(
            status=NEEDS_HUMAN_ADMIT,
            task_key=self.task.key,
            cache="miss",
            agent_called=True,
            candidate=candidate,
        )

    def _capture_refused(
        self, status: str, detail: str, computer: CaptureComputer
    ) -> JitResult:
        program = self.program_dir
        (program / "capture.json").write_text(json.dumps(computer.log, indent=2) + "\n")
        (program / "refused.json").write_text(
            json.dumps({"status": status, "detail": detail}, indent=2) + "\n"
        )
        kept = self.task_dir / f"refused-{_stamp()}"
        program.rename(kept)
        return JitResult(
            status=status,
            task_key=self.task.key,
            cache="miss",
            agent_called=True,
            detail=f"{detail} (capture kept at {kept})",
        )

    # -- admission (a person) ------------------------------------------------

    def admit(
        self,
        *,
        accept_params: list[str],
        confirm: object = None,
        operator: Optional[str] = None,
    ) -> dict[str, Any]:
        """Admit the waiting candidate. Called by a person, never by ``run``.

        Args:
            accept_params: The proposals to make parameters, by name. They
                must be exactly the task's parameters.
            confirm: Empty, ``ok``, ``yes`` or ``True``, as for authoring admit.
            operator: Who admits; defaults to the local OS user.

        The confirmed parameters are applied by recompiling the recording
        through :func:`~openadapt_flow.compiler.param_confirm.apply_decisions`.
        The admitted bundle must then carry a bound effect contract and pass
        the policy. The admission record pins the bundle's content digest.

        Raises:
            AdmissionError: no candidate, a refused confirm, parameters that do
                not match the task, a placeholder effect, or a policy failure.
        """

        from openadapt_flow.authoring import _confirm_is_ok, _local_operator
        from openadapt_flow.compiler.param_confirm import (
            apply_decisions,
            decisions_from_accept_list,
            load_proposals,
        )
        from openadapt_flow.ir import Workflow
        from openadapt_flow.policy import evaluate_policy, load_policy

        candidate = self.candidate()
        if candidate is None:
            raise AdmissionError("no candidate to admit")
        if self.admitted:
            raise AdmissionError("this task already has an admitted program")
        if not _confirm_is_ok(confirm):
            raise AdmissionError("admit accepts empty, ok, yes, or True")
        if sorted(accept_params) != sorted(self.task.params):
            raise AdmissionError(
                f"accept_params {sorted(accept_params)} must be the task's "
                f"parameters {sorted(self.task.params)}"
            )
        try:
            decisions = decisions_from_accept_list(
                load_proposals(self.bundle_dir), accept_params
            )
        except ValueError as exc:
            raise AdmissionError(str(exc)) from exc
        if decisions:
            apply_decisions(
                candidate["compiled_recording"],
                self.bundle_dir,
                name=self.task.task_id,
                decisions=decisions,
                mine_effects=self.app.record_reader is not None,
                bind_effect_params=True,
            )
        workflow = Workflow.load(self.bundle_dir)
        if sorted(workflow.params) != sorted(self.task.params):
            raise AdmissionError(
                f"the program's parameters {sorted(workflow.params)} do not match "
                f"the task's {sorted(self.task.params)}"
            )
        placeholders = [
            step.id
            for step in workflow.steps
            for effect in step.effects
            if effect.needs_operator_confirmation
        ]
        if placeholders:
            raise AdmissionError(
                "the demonstration showed a write with no system-of-record delta to "
                f"bind on {sorted(set(placeholders))}; there is nothing to verify"
            )
        report = evaluate_policy(
            workflow, load_policy(self.policy), require_current_risk_certification=True
        )
        if not report.passed:
            raise AdmissionError(
                f"not certified under {self.policy!r}:\n{report.render()}"
            )
        digest = workflow.manifest.content_digest if workflow.manifest else None
        if not digest:
            raise AdmissionError("the compiled bundle carries no content digest")
        record = {
            "status": "accepted",
            "kind": "local_operator_accept",
            "workflow_id": candidate["workflow_id"],
            "operator": operator or _local_operator(),
            "digest": digest,
            "task": self.task.to_dict(),
            "params": sorted(accept_params),
            "policy": self.policy,
            "effects": _effect_summary(self.bundle_dir),
            "admitted_at": _stamp(),
        }
        (self.bundle_dir / ADMIT_FILE).write_text(json.dumps(record, indent=2) + "\n")
        return record

    def reject(self, reason: str = "") -> Path:
        """Set the waiting candidate aside; the next run explores again."""

        if self.candidate() is None:
            raise AdmissionError("no candidate to reject")
        if self.admitted:
            raise AdmissionError("an admitted program is not a candidate")
        (self.program_dir / "rejected.json").write_text(
            json.dumps({"reason": reason, "rejected_at": _stamp()}, indent=2) + "\n"
        )
        kept = self.task_dir / f"rejected-{_stamp()}"
        self.program_dir.rename(kept)
        return kept

    # -- cache hit -----------------------------------------------------------

    def _run_admitted(self, values: dict[str, str]) -> JitResult:
        from openadapt_flow.bundle_validation import BundleIntegrityError
        from openadapt_flow.ir import Workflow

        record = json.loads((self.bundle_dir / ADMIT_FILE).read_text())

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
            workflow = Workflow.load(self.bundle_dir)
        except BundleIntegrityError as exc:
            return refused(f"the admitted bundle failed its integrity check: {exc}")
        digest = workflow.manifest.content_digest if workflow.manifest else None
        if not digest:
            return refused("the admitted bundle has no content digest")
        if record.get("status") != "accepted" or record.get("digest") != digest:
            return refused("the bundle is not the one a person admitted")
        if (record.get("task") or {}).get("key") != self.task.key:
            return refused("the admission is for a different task signature")
        if sorted(workflow.params) != sorted(self.task.params):
            return refused("the admitted program's parameters do not match the task")
        run_dir = self.runs_dir / f"{_stamp()}-{uuid.uuid4().hex[:8]}"
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        gate_detail, report = self._run_governed(workflow, digest, values, run_dir)
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
        if result.status != "VERIFIED":
            result = self._to_inbox(result)
        return result

    def _run_governed(
        self, workflow: Any, digest: str, values: dict[str, str], run_dir: Path
    ) -> tuple[Optional[str], Any]:
        """The standard-profile run: gate, authorization, Replayer, verifier."""

        from openadapt_flow.backends.playwright_backend import PlaywrightBackend
        from openadapt_flow.deployment import DeploymentConfig, PolicySection
        from openadapt_flow.execution_profiles import (
            ExecutionProfile,
            execution_profile_contract,
        )
        from openadapt_flow.run_gate import (
            build_runtime_authorization,
            evaluate_run_gate,
        )
        from openadapt_flow.runtime import Replayer

        gate = evaluate_run_gate(
            workflow,
            bundle_dir=self.bundle_dir,
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
            workflow, gate, approval_source="openadapt-flow-jit", params=values
        )
        if self.app.reset is not None:
            self.app.reset()
        backend, close = PlaywrightBackend.launch(
            self.app.url, headless=self.app.headless
        )
        try:
            report = Replayer(
                backend,
                effect_verifier=self.verifier,
                governed_authorization=authorization,
                durable=True,
                require_settled=True,
            ).run(
                workflow.model_copy(deep=True),
                params=values,
                bundle_dir=self.bundle_dir,
                run_dir=run_dir,
                execution_target_kind="web",
                execution_origin=self.app.origin,
                execution_entry_url=self.app.url,
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
    agent: ComputerUseAgent,
    *,
    task: TaskSignature,
    app: WebApp,
    verifier: Any,
    policy: str,
    cache_dir: Path | str = ".openadapt-jit",
    on_halt: Optional[Callable[[JitResult], None]] = None,
) -> CompiledAgent:
    """Wrap ``agent`` so a task is explored once and then run as a program.

    Args:
        agent: The computer-use agent, used on a cache miss only.
        task: The task id, the instruction, and the typed parameter schema.
        app: The web app, with the read-only system-of-record snapshot used
            while the agent's run is recorded.
        verifier: The effect verifier a run reads the system of record with,
            such as :class:`~openadapt_flow.runtime.effects.RestRecordVerifier`.
        policy: The policy the program must pass to be admitted and run, such
            as ``"clinical-write"``.
        cache_dir: Where candidates, admitted programs, runs and the exception
            inbox live.
        on_halt: Called with every run that does not end ``VERIFIED``, after it
            is written to the inbox.
    """

    if getattr(agent, "dialect", None) is None:
        raise ValueError("agent must declare its action dialect")
    return CompiledAgent(
        agent=agent,
        task=task,
        app=app,
        verifier=verifier,
        policy=policy,
        cache_dir=Path(cache_dir),
        on_halt=on_halt,
    )
