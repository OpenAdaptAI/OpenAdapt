"""Opt-in governed fallback from a compiled program to the agent.

By default a compiled program that stops is held: the agent task is never
rerun (see :class:`~openadapt_flow.integrations.agents.skills.SkillCache`).
:class:`FallbackPolicy` is the opt-in alternative for one task. It wraps the
cache and hands the task back to the agent only when the program's stop is
proved to have written nothing:

* the cache's hold is a terminal halt whose report says ``HALTED`` and
  ``HALTED_BEFORE_EFFECT``, with no step that needs reconciliation
  (:func:`openadapt_flow.transaction.steps_requiring_reconciliation`), bound
  to the admitted bundle's digest;
* the halted reply proved zero model calls;
* the cache's own record check of the halted run saw no new, changed or
  removed record of the task's record.

It never falls back on ``RECONCILIATION_REQUIRED``, a lost reply (the hold
stays ``running``), ``RECORD_CHECK_FAILED``, a receipt that is not
``VERIFIED``, or a model call during replay.

The fallback run is a run 1 through compile-agent's harness in its own
directory: the right-record check and a mandatory :class:`PreCommitGate`
before every save, the app's origin fence, and a read-back of every save
from the system of record. Its draft is never confirmed or admitted. A
verified fallback opens a repair candidate through
:meth:`SkillCache.open_repair` when a repair desk is configured; a person
still approves the repair, and :meth:`SkillCache.complete_repair` runs its
canaries afterwards. Nothing here admits a program or clears a hold.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from openadapt_flow.compile_agent.harness import GateRequest
from openadapt_flow.integrations.agents.skills import SkillRun
from openadapt_flow.wrap import fallback_marker
from openadapt_flow.wrap.compiled_only import NO_ADMITTED_PROGRAM, compiled_only

#: ``SkillRun.path`` of a run the agent did after a program's drift.
FALLBACK_PATH = "agent_fallback"

#: Program outcomes that never fall back, whatever their evidence says.
NEVER_FALL_BACK = frozenset(
    {
        "VERIFIED",
        "RECONCILIATION_REQUIRED",
        "RECORD_CHECK_FAILED",
        "RECEIPT_NOT_VERIFIED",
        "MODEL_CALLS_NOT_ZERO",
        "BASELINE_UNAVAILABLE",
    }
)

_ACTIVE = fallback_marker.ACTIVE
#: Fallback statuses that already say a save went wrong (a check caught it):
#: kept as they are when the final read also shows a change.
_WROTE_WRONG = frozenset(
    {
        "GATE_NOT_PROVED",
        "record_check_failed",
        "readback_wrong_record",
        "readback_refused",
    }
)
_HALT = "halt-evidence.json"
_RESULT = "result.json"
_REQUEST_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def valid_request_id(value: Any) -> bool:
    """A fallback request id: a plain file name of 1 to 128 safe characters."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and set(value) <= _REQUEST_CHARS
        and not value.startswith(".")
    )


class PreCommitGate(Protocol):
    """Asked before every save in a fallback run; True lets that save through.

    It sees compile-agent's :class:`GateRequest` (the control, the record's
    identity on screen, the URL and the gate screenshot) after the harness's
    right-record check passed.
    """

    def __call__(self, request: GateRequest) -> bool: ...


class TerminalGate:
    """The default gate: compile-agent's terminal confirmation.

    A person at the terminal types ``y`` for each save. With no terminal it
    refuses every save, and :class:`FallbackPolicy` does not start the agent.
    """

    def __init__(
        self,
        ask: Callable[[str], str] = input,
        attended: Optional[Callable[[], bool]] = None,
    ) -> None:
        from openadapt_flow.compile_agent.cli import _interactive, _terminal_hold

        self._hold = _terminal_hold(ask)
        self._attended = attended or _interactive

    @property
    def attended(self) -> bool:
        return bool(self._attended())

    def __call__(self, request: GateRequest) -> bool:
        if not self.attended:
            return False
        return bool(self._hold(request))


@dataclass(frozen=True)
class Eligibility:
    """Whether a held program may hand this task to the agent, and why."""

    eligible: bool
    reason: str
    run_dir: Optional[Path] = None
    bundle_digest: Optional[str] = None


def _int_zero(value: Any) -> bool:
    return type(value) is int and value == 0


def _nothing_written(check: Any) -> bool:
    """Both reads of the record check succeeded and saw no change at all.

    An unreadable store also reports zero counts; without ``readable`` (an
    older check) nothing is proved. A change to another record during the
    run (``outside_run``) may be the run's own write elsewhere, so it counts
    as something written too, as does a check without ``outside_run``.
    """
    if not (
        isinstance(check, Mapping)
        and check.get("readable") is True
        and _int_zero(check.get("created"))
        and _int_zero(check.get("changed"))
        and _int_zero(check.get("removed"))
    ):
        return False
    outside = check.get("outside_run")
    return isinstance(outside, Mapping) and all(
        _int_zero(outside.get(key)) for key in ("created", "changed", "removed")
    )


def proved_before_effect(
    cache: Any, prompt: str, params: Mapping[str, Any]
) -> Eligibility:
    """Read the cache's hold and the halted run's report from disk.

    The checks are those :meth:`SkillCache.open_repair` applies before it
    offers a repair, without its extra one-step-replacement requirement. It
    reads only; a cache that is not a SkillCache is never eligible.
    """
    from openadapt_flow.integrations.agents.skills import _STOP_FILE
    from openadapt_flow.ir import RunReport, Workflow
    from openadapt_flow.jit import lifecycle
    from openadapt_flow.transaction import steps_requiring_reconciliation

    compiler_for = getattr(cache, "_compiler", None)
    if not callable(compiler_for):
        return Eligibility(False, "not_a_skill_cache")
    try:
        compiler, _key = compiler_for(prompt, params)
        stop = compiler.dir / _STOP_FILE
        if stop.is_symlink() or not stop.is_file():
            return Eligibility(False, "no_hold")
        stopped = json.loads(stop.read_text(encoding="utf-8"))
        if not isinstance(stopped, dict) or stopped.get("phase") != "halted":
            # A ``running`` hold is a lost reply: a write may exist.
            return Eligibility(False, "hold_not_terminal")
        replay = stopped.get("replay")
        if not isinstance(replay, dict):
            return Eligibility(False, "no_halted_run")
        if replay.get("status") in NEVER_FALL_BACK or replay.get("verified"):
            return Eligibility(False, "outcome_never_falls_back")
        if not _int_zero(replay.get("model_calls")):
            return Eligibility(False, "model_calls_not_zero")
        jit = compiler._jit()
        run_dir = Path(replay["run_dir"]).resolve(strict=True)
        if run_dir.parent != Path(jit.runs_dir).resolve(strict=True):
            return Eligibility(False, "foreign_run")
        report_path = run_dir / "report.json"
        if report_path.is_symlink():
            return Eligibility(False, "foreign_report")
        workflow = Workflow.load(jit.bundle_dir)
        admission = lifecycle.verify_admission(jit.bundle_dir, key_dir=compiler.key_dir)
        report = RunReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError, lifecycle.AdmissionError):
        return Eligibility(False, "unreadable_evidence")
    if not (
        report.workflow_name == workflow.name
        and report.bundle_content_digest == stopped.get("bundle_digest")
        and report.bundle_content_digest == admission.get("digest")
    ):
        return Eligibility(False, "evidence_not_bound_to_admission")
    if report.execution_outcome != "HALTED":
        return Eligibility(False, "not_halted")
    if report.transaction_outcome != "HALTED_BEFORE_EFFECT":
        return Eligibility(False, "not_before_effect")
    if steps_requiring_reconciliation(report):
        return Eligibility(False, "steps_require_reconciliation")
    return Eligibility(
        True, "halted_before_effect", run_dir, report.bundle_content_digest
    )


def _cap(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError("the fallback needs a positive, finite max_cost_usd")
    return float(value)


def _spent(agent: Any) -> Optional[float]:
    """What the agent has spent so far, or None when it is not known."""
    ledger = getattr(agent, "last_ledger", None)
    if ledger is None:
        return 0.0 if not getattr(agent, "requires_model_key", True) else None
    if not ledger.model_calls:
        return 0.0
    if not getattr(ledger, "usage_known", False):
        return None
    try:
        spent = float(ledger.cost_usd())
    except Exception:  # noqa: BLE001 - an unpriceable run is an unknown spend
        return None
    return spent if math.isfinite(spent) and spent >= 0 else None


def _write(path: Path, data: Mapping[str, Any], *, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, sort_keys=True, default=str)
        stream.flush()
        os.fsync(stream.fileno())


def fallback_root(cache: Any, prompt: str, params: Mapping[str, Any]) -> Path:
    """Where this task's fallback runs are kept.

    Beside the task's cache directory, not inside it, so an eviction (which
    moves the task directory aside) keeps the fallback evidence in place.
    """
    task_dir = Path(cache.receipt_root(prompt, params))
    return task_dir.parent / "fallback" / task_dir.name


def keep_halt_evidence(
    cache: Any, prompt: str, params: Mapping[str, Any], halted: SkillRun
) -> Eligibility:
    """After a program run that just stopped: keep its record check beside
    the halt when it proves nothing was written. Returns that halt's
    eligibility (without the kept-evidence check of :func:`halt_evidence`)."""

    found = proved_before_effect(cache, prompt, params)
    if not found.eligible:
        return found
    if not (_int_zero(halted.model_calls) and _nothing_written(halted.record_check)):
        return Eligibility(False, "record_changed_or_unproved")
    root = fallback_root(cache, prompt, params)
    root.mkdir(parents=True, exist_ok=True)
    _write(
        root / _HALT,
        {
            "run_dir": str(found.run_dir),
            "bundle_digest": found.bundle_digest,
            "record_check": halted.record_check,
        },
    )
    return found


def halt_evidence(cache: Any, prompt: str, params: Mapping[str, Any]) -> Eligibility:
    """Whether the task's hold is proved to have written nothing.

    :func:`proved_before_effect`, plus the record check kept beside that
    same halt (:func:`keep_halt_evidence`): both reads succeeded and found
    no change to the task's record. A halt nobody kept a clean check for is
    not proved. The fallback, ``wrap-dispatch status`` and ``release`` all
    use this one rule.
    """

    found = proved_before_effect(cache, prompt, params)
    if not found.eligible:
        return found
    try:
        halt = json.loads(
            (fallback_root(cache, prompt, params) / _HALT).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        halt = None
    if not (
        isinstance(halt, dict)
        and halt.get("run_dir") == str(found.run_dir)
        and halt.get("bundle_digest") == found.bundle_digest
        and _nothing_written(halt.get("record_check"))
    ):
        return Eligibility(False, "no_record_check_for_halt")
    return found


class FallbackPolicy:
    """A TaskCache that may hand a proved pre-effect drift to the agent.

    Args:
        cache: The task's cache (normally a SkillCache). Everything other
            than :meth:`run` is the cache's own.
        max_cost_usd: The fallback run's model spend cap. Required; it is
            separate from any cap on a first run.
        gate: Asked before every save in the fallback run. Default: a
            :class:`TerminalGate`.
        request_id: The fallback run's own id. Default: a new one per run.
        repair_desk: When given, a verified fallback opens a repair
            candidate with :meth:`SkillCache.open_repair`.
        agent_factory: Builds the fallback agent. Called only after the halt
            is proved before effect and a person is at the gate; then the
            agent :meth:`run` was given is ignored.
        parent_request_id: The dispatch request this fallback answers,
            recorded with the fallback's result.
    """

    def __init__(
        self,
        cache: Any,
        *,
        max_cost_usd: float,
        gate: Optional[PreCommitGate] = None,
        request_id: Optional[str] = None,
        repair_desk: Any = None,
        agent_factory: Optional[Callable[[], Any]] = None,
        parent_request_id: Optional[str] = None,
    ) -> None:
        self.cache = cache
        self.max_cost_usd = _cap(max_cost_usd)
        self.gate: PreCommitGate = gate if gate is not None else TerminalGate()
        if request_id is not None and not valid_request_id(request_id):
            raise ValueError("a fallback request id is 1 to 128 of [A-Za-z0-9._-]")
        self.request_id = request_id
        self.repair_desk = repair_desk
        #: Builds the fallback agent, called only once a fallback may start.
        self.agent_factory = agent_factory
        self.parent_request_id = parent_request_id
        #: What the last :meth:`run` did about a fallback (None: not tried).
        self.last: Optional[dict[str, Any]] = None

    def __getattr__(self, name: str) -> Any:
        # Only called for names this object lacks: the cache's own surface
        # (key, held, server, receipt_root, evict, open_repair ...).
        return getattr(self.__dict__["cache"], name)

    def key(self, prompt: str, params: Mapping[str, Any]) -> dict[str, Any]:
        return self.cache.key(prompt, params)

    # -- the policy ------------------------------------------------------------

    def run(self, prompt: str, params: Mapping[str, Any], agent: Any) -> SkillRun:
        self.last = None
        # Compiled replay only: a miss never reaches the agent. A lost reply
        # raises here and stays held; it never falls back.
        first = compiled_only(self.cache, prompt, params)
        if (
            first.path != "skill"
            or first.verified
            or first.status in NEVER_FALL_BACK
            or first.status == NO_ADMITTED_PROGRAM
        ):
            return first
        root = fallback_root(self.cache, prompt, params)
        found = Eligibility(True, "")
        if first.status != "CACHE_HALTED":
            # The run that just halted: keep its record check with the halt,
            # so a later held call can rely on it too.
            found = keep_halt_evidence(self.cache, prompt, params, first)
        if found.eligible:
            found = halt_evidence(self.cache, prompt, params)
        if not found.eligible:
            self.last = {"fallback": False, "reason": found.reason}
            return first
        return self._fallback(prompt, params, agent, first, root)

    def _refused(
        self, first: SkillRun, status: str, detail: str, started: float, **last: Any
    ) -> SkillRun:
        self.last = {"fallback": False, "reason": status.lower(), **last}
        return SkillRun(
            path=FALLBACK_PATH,
            status=status,
            verified=False,
            model_calls=0,
            cost_usd=0.0,
            usage=[],
            usage_known=True,
            seconds=round(time.monotonic() - started, 3),
            detail=detail,
            replay={"program": first.replay, "fallback": self.last},
        )

    def _fallback(
        self,
        prompt: str,
        params: Mapping[str, Any],
        agent: Any,
        first: SkillRun,
        root: Path,
    ) -> SkillRun:
        started = time.monotonic()
        if getattr(self.gate, "attended", True) is False:
            return self._refused(
                first,
                "FALLBACK_GATE_UNATTENDED",
                "No person is at the pre-commit gate; the agent was not started.",
                started,
            )
        if self.agent_factory is not None:
            try:
                agent = self.agent_factory()
            except (Exception, SystemExit):  # noqa: BLE001 - nothing ran yet
                agent = None
        if agent is None:
            return self._refused(
                first, "FALLBACK_NO_AGENT", "No fallback agent was given.", started
            )
        model_free = not getattr(agent, "requires_model_key", True)
        own_cap = getattr(agent, "max_cost_usd", None)
        if not model_free and (
            isinstance(own_cap, bool) or not isinstance(own_cap, (int, float))
        ):
            return self._refused(
                first,
                "FALLBACK_COST_UNBOUNDED",
                "The fallback agent has no spend cap this policy can set.",
                started,
            )
        request_id = self.request_id or f"fallback-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        marker = fallback_marker.Marker(root, request_id)
        try:
            marker.claim()
        except FileExistsError:
            # Another fallback is running, or one ended without proving what
            # it wrote. A person reconciles it before another one starts.
            return self._refused(
                first,
                "RECONCILIATION_REQUIRED",
                "An earlier fallback of this task has not been reconciled.",
                started,
            )
        run_root = root / request_id
        try:
            try:
                run_root.mkdir()
            except FileExistsError:
                marker.clear()
                return self._refused(
                    first,
                    "FALLBACK_REQUEST_REUSED",
                    "This fallback request id already ran.",
                    started,
                    request_id=request_id,
                )
            decisions: list[str] = []
            # Recorded before the agent starts: if anything below raises, the
            # dispatch result still names this fallback, as a possible write.
            self.last = {
                "fallback": True,
                "request_id": request_id,
                "parent_request_id": self.parent_request_id,
                "status": "RECONCILIATION_REQUIRED",
                "run_status": None,
                "decisions": decisions,
                "admitted": False,
                "error": None,
            }
            try:
                bind = getattr(self.gate, "bind", None)
                if callable(bind):
                    # A gate that keeps its own evidence learns its run.
                    bind(run_root, request_id)
                return self._run_agent(
                    prompt, params, agent, first, marker, run_root, started, decisions
                )
            except BaseException as exc:
                self._crashed(first, run_root, started, exc)
                raise
        finally:
            # A run that did not prove what it wrote (or raised) keeps the
            # marker, marked ended, for a person; a cleared one is gone.
            marker.end()

    def _run_agent(
        self,
        prompt: str,
        params: Mapping[str, Any],
        agent: Any,
        first: SkillRun,
        marker: fallback_marker.Marker,
        run_root: Path,
        started: float,
        decisions: list[str],
    ) -> SkillRun:
        from openadapt_flow.compile_agent import FirstRunCompiler

        request_id = run_root.name
        cap = self.max_cost_usd

        def hold(request: GateRequest) -> bool:
            spent = _spent(agent)
            if spent is None or spent > cap:
                decisions.append("refused_cost_cap")
                return False
            approved = bool(self.gate(request))
            decisions.append("approved" if approved else "refused_by_gate")
            return approved

        common = {
            k: v
            for k, v in self.cache.common.items()
            if k not in ("reset", "task_id", "review", "hold", "cache_dir")
        }
        compiler = FirstRunCompiler(
            prompt,
            params=list(params),
            task_id=self.cache.task_id(prompt, params),
            cache_dir=run_root / "run",
            model_key=getattr(self.cache, "model_key", None),
            before_capture=getattr(self.cache, "before_capture", None),
            **common,
        )
        restore = getattr(agent, "max_cost_usd", None)
        capped = isinstance(restore, (int, float)) and not isinstance(restore, bool)
        if isinstance(restore, (int, float)) and capped:
            agent.max_cost_usd = min(float(restore), cap)
        try:
            result = compiler.record(agent, dict(params), hold=hold)
        finally:
            if capped:
                agent.max_cost_usd = restore
        # The cache's own record check, on reads independent of the run's
        # report: the baseline taken as the run started, and a fresh read.
        from openadapt_flow.integrations.agents.skills import task_record_check

        try:
            # A sign-in value is never saved in the record.
            withheld = set(compiler._secret_params())
        except Exception:  # noqa: BLE001 - no bundle: no input is known secret
            withheld = set()
        written = task_record_check(
            compiler._baseline,
            compiler.observer.system_of_record,
            expect=self.cache.common["expect"],
            values={k: str(v) for k, v in params.items() if k not in withheld},
        ).to_dict()
        run1 = result.run1 or {}
        ledger = run1.get("agent") or {}
        model_calls = ledger.get("model_calls")
        if isinstance(model_calls, bool) or not isinstance(model_calls, int):
            # A run that stopped at the gate may return no ledger; an agent
            # with no model behind it still made no calls.
            model_calls = None if getattr(agent, "requires_model_key", True) else 0
        raw_cost = ledger.get("cost_usd")
        cost: Optional[float] = (
            float(raw_cost)
            if isinstance(raw_cost, (int, float))
            and not isinstance(raw_cost, bool)
            and math.isfinite(float(raw_cost))
            and raw_cost >= 0
            else None
        )
        if model_calls == 0:
            cost = 0.0
        usage_known = model_calls is not None and cost is not None
        gates = [g.get("decision") for g in run1.get("gates") or []]
        check = result.record_check or {}
        passed = bool(check.get("passed"))
        status, detail = result.status, result.detail
        if result.status == "draft" and passed:
            status, detail = "VERIFIED", "The agent's save was read back."
        if (
            "refused_cost_cap" in decisions
            or (cost is not None and cost > cap)
            or (model_calls and cost is None)
        ):
            status = "COST_CAP_EXCEEDED"
            detail = (
                f"The fallback's model spend passed or could not be held to ${cap:.2f}."
            )
        elif "refused_by_gate" in decisions:
            status, detail = "GATE_REFUSED", "The person at the gate refused the save."
        verified = (
            status == "VERIFIED"
            and bool(gates)
            and all(g == "approved_by_hold" for g in gates)
        )
        if status == "VERIFIED" and not verified:
            status, detail = "GATE_NOT_PROVED", "A save did not pass the gate."
        # The final, task-scoped read decides: a success it doesn't confirm,
        # or a stop it can't show wrote nothing, needs a person. Both
        # readings are kept for that reconciliation.
        confirmed = written.get("readable") is True and written.get("passed") is True
        unchanged = _nothing_written(written)
        run_status = status
        if verified and not confirmed:
            verified = False
            status = "RECONCILIATION_REQUIRED"
            detail = "The final read of the record does not confirm the save."
        elif not verified and not unchanged and status not in _WROTE_WRONG:
            status = "RECONCILIATION_REQUIRED"
            detail = "The final read of the record can't show that nothing was saved."
        repair = self._open_repair(prompt, params, run_root) if verified else None
        self.last = {
            "fallback": True,
            "request_id": request_id,
            "parent_request_id": self.parent_request_id,
            "status": status,
            "run_status": run_status,
            "readings_agree": verified or (unchanged and not check.get("passed")),
            "gates": gates,
            "decisions": decisions,
            "approvals": decisions.count("approved"),
            "repair": repair,
            "admitted": False,
            "task_record_check": written,
        }
        answers = getattr(self.gate, "decisions", None)
        if isinstance(answers, list):
            # A gate that keeps its own evidence (ItemApprovalGate): which
            # request each save asked and what answered it. PHI-free.
            self.last["gate_answers"] = [dict(row) for row in answers]
        run = SkillRun(
            path=FALLBACK_PATH,
            status=status,
            verified=verified,
            model_calls=model_calls,
            cost_usd=cost,
            seconds=round(time.monotonic() - started, 3),
            detail=detail,
            wire_checked=self.cache.common.get("write_adapter") is not None,
            compile=result.to_dict(),
            replay={"program": first.replay, "fallback": self.last},
            record_check=check,
            usage=[{"calls": model_calls, "usd": cost}]
            if model_calls is not None
            else None,
            usage_known=usage_known,
        )
        _write(run_root / _RESULT, run.to_dict())
        # The marker goes only when what the run did is known (a save the
        # final read confirmed, or a final read that found the task's record
        # unchanged), and only while it is still this run's.
        if verified or unchanged:
            marker.clear()
        return run

    def _crashed(
        self, first: SkillRun, run_root: Path, started: float, exc: BaseException
    ) -> None:
        """A fallback that raised: keep what is known, as a possible write."""
        assert self.last is not None
        self.last.update(
            status="RECONCILIATION_REQUIRED",
            error=type(exc).__name__,
            gate_answers=[
                dict(row) for row in getattr(self.gate, "decisions", None) or []
            ],
        )
        run = SkillRun(
            path=FALLBACK_PATH,
            status="RECONCILIATION_REQUIRED",
            verified=False,
            model_calls=None,
            cost_usd=None,
            seconds=round(time.monotonic() - started, 3),
            detail="The fallback run raised; a save may exist.",
            replay={"program": first.replay, "fallback": self.last},
            usage=None,
            usage_known=False,
        )
        try:
            _write(run_root / _RESULT, run.to_dict())
        except OSError:
            pass  # The marker stays; a person reconciles without the file.

    def _open_repair(
        self, prompt: str, params: Mapping[str, Any], run_root: Path
    ) -> dict[str, Any]:
        if self.repair_desk is None:
            return {"opened": False, "reason": "no_repair_desk"}
        from openadapt_flow.agent.repair import RepairError

        try:
            reply = self.cache.open_repair(prompt, params, repair_desk=self.repair_desk)
        except RepairError:
            return {"opened": False, "reason": "no_one_step_repair"}
        except Exception as exc:  # noqa: BLE001 - the verified save stands
            return {"opened": False, "reason": f"repair_failed:{type(exc).__name__}"}
        # The repair token is a one-use capability for the person who
        # demonstrates the fix; it stays in a private local file.
        _write(run_root / "repair.json", reply)
        return {
            "opened": True,
            "step_id": reply.get("step_id"),
            "file": str(run_root / "repair.json"),
        }


def fallback_runs(cache: Any, prompt: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """This task's fallback runs and whether one is unreconciled."""
    root = fallback_root(cache, prompt, params)
    runs: list[dict[str, Any]] = []
    if root.is_dir():
        for path in sorted(root.iterdir()):
            result = path / _RESULT
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                data = json.loads(result.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            fallback = (data.get("replay") or {}).get("fallback") or {}
            runs.append(
                {
                    "request_id": path.name,
                    "status": data.get("status", "UNFINISHED"),
                    "verified": data.get("verified") is True,
                    "repair": fallback.get("repair"),
                }
            )
    marker = fallback_marker.read(root)
    return {
        "dir": str(root),
        "in_flight": marker is not None,
        "in_flight_request_id": (marker or {}).get("request_id"),
        # running or unknown: a save may still happen; release refuses.
        "in_flight_owner": fallback_marker.owner_state(marker),
        "runs": runs,
    }


def release_in_flight(
    cache: Any, prompt: str, params: Mapping[str, Any]
) -> dict[str, Any]:
    """A person reconciled a stuck fallback: drop its marker (evidence stays).

    Only a marker whose owner ended is dropped; a live or unprovable owner
    keeps it (``owner`` says which).
    """
    return fallback_marker.release(fallback_root(cache, prompt, params))


def clear_in_flight(cache: Any, prompt: str, params: Mapping[str, Any]) -> bool:
    """:func:`release_in_flight`, True when a marker was dropped."""
    return bool(release_in_flight(cache, prompt, params)["cleared"])
