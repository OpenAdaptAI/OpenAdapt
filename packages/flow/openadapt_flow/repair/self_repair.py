"""Self-repair: an agent proposes a fix for a halted step, a person accepts it.

A run halts before any effect when a vendor changes a screen: the save button
has a new label, a new place, a new DOM id, and nothing on the recorded
ladder finds it. Without self-repair the run stops and pages a person. With
it, :class:`SelfRepair` does this, in order:

1. **Halt.** The run halts as usual (``HALTED_BEFORE_EFFECT``; a run with
   no named profile records no outcome word, and the same evidence decides).
   Self-repair starts only when no input reached a consequential step, no
   effect was observed, and the halted step is a click whose target the
   deterministic ladder could not find (or found only by geometry). A
   wrong-record halt (identity mismatch) is never repaired.
2. **Propose.** A computer-use agent (any action dialect
   :mod:`openadapt_flow.jit.adapters` understands) is shown the halted step's
   intent, the recorded evidence (the target crop, its label, its place) and
   the live screen. It proposes one corrected action, or a bridge of up to
   three when the vendor added a required dialog or moved a control into a
   menu. Its model calls and their cost are recorded. An agent that calls a
   model runs only with the customer's own key.
3. **Patch.** In ``replace`` mode (the target is gone) the last action
   becomes the halted step's new target, built from the live screen with the
   heal machinery; identity evidence is copied unchanged. In ``after`` mode (a
   click acted but a new dialog appeared) the actions become new steps after
   it. Every new click gets a template, its DOM locator, a postcondition and
   an identity check that must name a record the recording verified. A
   proposal is refused when a new step would save, has no readable label,
   types a value that is neither a parameter nor on the screen, or when the
   re-targeted control reads as a different kind of action.
4. **Probe.** The patched bundle replays in a fresh session behind a gate
   that stops at the last point before the first save, after the target and
   the right-record check resolved. Nothing is written. The probe passes only
   when the deterministic ladder (not the agent) found the target, every
   identity check verified, each typed value is in the form, and the form is
   the same as when the run halted.
5. **Campaigns.** A passing probe becomes an ordinary repair candidate
   (source ``model_suggestion``, provenance ``self_repair``) in the
   :class:`~openadapt_flow.repair.lifecycle.RepairStore`. The replay and
   fault campaigns run before anyone is asked: the existing frame campaigns
   when the step's identity is readable from pixels, otherwise the live probe
   (healthy) and the operator's fault probes, each of which must stop before
   the save.
6. **Approve.** One person answers one request, on the phone (the decision
   portal) or with ``openadapt-flow item-approval approve``. That answer
   records the review and the approval in the store under that person's
   name. The agent
   can never approve its own patch, and no answer means nothing is saved.
7. **Resume.** The approved bundle is staged, enters its canary window, and
   re-runs the item from the start in a fresh session. At the save it checks
   the pre-save state again and proceeds only if it matches what the person
   approved. The record check runs as usual.

Every stage is written to ``<run_dir>/self-repair/self-repair.json`` and to the
``self_repair`` block of the final report. A healthy run records
``status="not_needed"``, zero model calls and zero cost; the agent is never
called.

Re-running from the start is safe only because the halt came before any
effect; :meth:`SelfRepair.run` refuses every other halt.
"""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Protocol, Sequence

from openadapt_flow.approval_mode.gate import ApprovalGate, GateVerdict, SaveContext
from openadapt_flow.approval_mode.model import ItemDecisionKind
from openadapt_flow.approval_mode.post import ItemDecision
from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Resolution,
    RunReport,
    SelfRepairEvidence,
    SelfRepairStepRecord,
    Step,
    Workflow,
)
from openadapt_flow.repair.candidate import (
    Actor,
    CampaignCaseResult,
    CampaignResult,
    RepairCandidate,
    RepairProvenance,
    ScreenScrubEvidence,
    ScreenShareMode,
    TaughtStep,
    sha256_hex,
)

if TYPE_CHECKING:
    from openadapt_flow.jit.keys import CustomerModelKey
    from openadapt_flow.repair.lifecycle import RepairStore

__all__ = [
    "AppSession",
    "ProbeOutcome",
    "RepairAgent",
    "RepairProposal",
    "RepairRequest",
    "ReplayTarget",
    "ScreenAccess",
    "SelfRepair",
    "SelfRepairRefused",
    "SelfRepairResult",
    "SelfRepairTarget",
]

#: Rungs the deterministic ladder trusts for a probe: the target was found
#: by the DOM locator, a template, or its label, never by geometry or a model.
TRUSTED_RUNGS = frozenset({"structural", "template", "template_global", "ocr"})
#: The most actions a bridge may have.
MAX_BRIDGE = 3
#: The automation identity that records campaign results. Never a reviewer.
AUTOMATION = Actor(kind="automation", id="openadapt-self-repair")
_POINTER_KINDS = {
    ActionKind.CLICK: "click",
    ActionKind.DOUBLE_CLICK: "double_click",
}
_STOP_REASON = "self_repair_probe"
#: The replayer's own wording for its two target-resolution refusals.
_NOT_FOUND = "Could not resolve target for step"
_BELOW_RUNG = "only resolved via the"
#: The replayer's wording when a step acted but its expected screen never came.
_EXPECTED_SCREEN = "Postconditions failed for step"
_UNTRUSTED_RUNGS = frozenset({"geometry", "grounder"})
#: A step that does not save may act on geometry; the structural safety check
#: then refuses a point where no actionable control is, before any input.
#: Only that refusal counts: an ambiguous locator or a target whose record
#: identity changed is a different structural refusal and is never repaired.
_NOT_A_CONTROL = "Structural safety refusal"
_NO_CONTROL_THERE = (
    "visual point is not an identity-bearing actionable DOM element — "
    "no action was admitted"
)


class SelfRepairRefused(RuntimeError):
    """Self-repair can't start or a proposal was refused. Nothing was written."""


# -- what the agent sees and returns -------------------------------------------


@dataclass(frozen=True)
class RepairRequest:
    """Everything the repair agent is shown for one halted step.

    ``live_png`` and ``recorded_crop_png`` are scrubbed first unless an
    operator set ``allow_unscrubbed``. Coordinates in the proposal are in the
    live screen's pixels.

    ``mode`` says what the bridge does:

    - ``replace``: the step's target is gone. The bridge's last action is the
      step's new target; any actions before it are new steps that run first
      (for example opening a menu).
    - ``after``: the step acted but its expected screen never came (a new
      dialog appeared). The bridge's actions are new steps after it, and the
      last one must reach ``expected_texts``.
    """

    step_id: str
    intent: str
    action: str
    halt_reason: str
    live_png: bytes
    viewport: tuple[int, int]
    recorded_label: Optional[str]
    recorded_region: tuple[int, int, int, int]
    recorded_click_point: tuple[int, int]
    recorded_crop_png: Optional[bytes]
    dialect: str
    attempt: int
    #: Why earlier proposals for this step were refused, oldest first.
    previous_refusals: tuple[str, ...] = ()
    #: The customer's key for the agent's model client. Never an OpenAdapt key.
    model_key: Optional["CustomerModelKey"] = field(default=None, repr=False)
    mode: str = "replace"
    #: The most actions a bridge may have.
    max_actions: int = 1
    #: Text the step's expected screen shows (``after`` mode).
    expected_texts: tuple[str, ...] = ()
    #: The recorded crop of the step's expected screen, when the bundle has one.
    expected_png: Optional[bytes] = None
    #: The item's parameters a bridge may type, by name. Secrets are left out.
    param_values: Mapping[str, str] = field(default_factory=dict, repr=False)
    #: The program's record check: each saving step's effect contracts, with
    #: literal values masked, and a digest of its record identity contract
    #: (:func:`openadapt_flow.jit.signature.record_check_of`). A fix never
    #: changes it; the probe and the resume check it again.
    record_check: Mapping[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class RepairProposal:
    """A fix in the agent's own dialect: one action, or a short bridge.

    ``action`` is a single native action (for example
    ``{"action": "left_click", "coordinate": [846, 457]}`` for ``anthropic``
    or ``{"kind": "click", "x": 846, "y": 457}`` for ``generic``).
    ``actions`` is a bridge of up to ``request.max_actions`` such actions,
    in order; give one or the other. Clicks may name a point or, where the
    dialect allows, a CSS selector. A bridge may type only a parameter's
    value or text the screen shows at that moment. The agent reports the
    model calls it made and their cost; Flow records them and can't measure
    them itself.
    """

    action: Optional[Mapping[str, Any]] = None
    model_calls: int = 0
    cost_usd: float = 0.0
    note: str = ""
    actions: Sequence[Mapping[str, Any]] = ()

    def bridge(self) -> list[Mapping[str, Any]]:
        """The proposal's actions, in order."""

        if self.actions and self.action is not None:
            raise SelfRepairRefused("give one action or a bridge of actions, not both")
        if self.actions:
            return list(self.actions)
        return [self.action] if self.action is not None else []


class RepairAgent(Protocol):
    """A computer-use agent that proposes one fix at a time.

    ``dialect`` names its action format (see
    :func:`openadapt_flow.jit.adapters.dialects`). ``agent_id`` is recorded on
    the candidate and can never review or approve it. An agent that makes no
    model calls sets ``requires_model_key = False``; any other agent runs only
    with the customer's key. ``propose`` returns ``None`` to give up.
    """

    dialect: str
    agent_id: str

    def propose(self, request: RepairRequest) -> Optional[RepairProposal]: ...


@dataclass(frozen=True)
class ScreenAccess:
    """Who let the agent see screens, and how.

    ``mode`` is the same setting ``openadapt-agent repair access`` uses:
    ``scrubbed_only`` (default; ``scrub`` must be given or openadapt-privacy
    installed) or ``allow_unscrubbed``. ``ask_each_time`` is not available
    for self-repair yet. ``scrub`` takes a PNG and returns the scrubbed PNG
    and the scrubber's evidence (``provider``, ``package_version``,
    ``policy_sha256``).
    """

    enabled_by: str
    mode: ScreenShareMode = "scrubbed_only"
    scrub: Optional[Callable[[bytes], tuple[bytes, Mapping[str, Any]]]] = None


# -- how self-repair reaches the app -------------------------------------------


@dataclass
class AppSession:
    """An open session of the app, at the item's entry point."""

    backend: Any
    close: Callable[[], None]


class SelfRepairTarget(Protocol):
    """Opens sessions of the app and runs the governed engine in them.

    ``open(fault=None)`` opens a fresh session at the item's entry point; a
    fault name opens a session that presents that fault (for the fault
    probes). ``execute`` admits ``workflow`` through the run gate and runs it
    with a fresh authorization; it must raise :class:`SelfRepairRefused` when
    the gate refuses.
    """

    def open(self, fault: Optional[str] = None) -> AppSession: ...

    def execute(
        self,
        workflow: Workflow,
        bundle_dir: Path,
        session: AppSession,
        run_dir: Path,
        *,
        approval_gate: Optional[ApprovalGate] = None,
    ) -> RunReport: ...


class ReplayTarget:
    """A :class:`SelfRepairTarget` over the shipped run gate and Replayer.

    Each execution evaluates :func:`~openadapt_flow.run_gate.evaluate_run_gate`
    for the exact bundle (the patched bundle has its own digest) and builds a
    fresh single-use authorization, then runs the
    :class:`~openadapt_flow.runtime.Replayer` durable, settled, with the
    deployment's effect verifier, under the named profile.

    Args:
        open_session: ``open_session(fault)`` returns an :class:`AppSession`.
        effect_verifier: The system-of-record verifier (the record check).
        policy: The policy the bundle is certified against.
        profile: The execution profile (default ``standard``).
        params: The item's runtime parameters.
        entry_url: Recorded on the report for web runs.
        execution_origin: Recorded on the report for web runs.
        replayer_kwargs: Extra keyword arguments for the Replayer.
    """

    def __init__(
        self,
        open_session: Callable[[Optional[str]], AppSession],
        *,
        effect_verifier: Any,
        policy: Optional[str] = None,
        profile: str = "standard",
        params: Optional[Mapping[str, Any]] = None,
        worklists: Optional[Mapping[str, list[dict[str, str]]]] = None,
        entry_url: Optional[str] = None,
        execution_origin: Optional[str] = None,
        execution_target_kind: Optional[str] = "web",
        replayer_kwargs: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._open = open_session
        self.effect_verifier = effect_verifier
        self.policy = policy
        self.profile = profile
        self.params = dict(params or {})
        self.worklists = dict(worklists or {})
        self.entry_url = entry_url
        self.execution_origin = execution_origin
        self.execution_target_kind = execution_target_kind
        self.replayer_kwargs = dict(replayer_kwargs or {})

    def open(self, fault: Optional[str] = None) -> AppSession:
        return self._open(fault)

    def execute(
        self,
        workflow: Workflow,
        bundle_dir: Path,
        session: AppSession,
        run_dir: Path,
        *,
        approval_gate: Optional[ApprovalGate] = None,
    ) -> RunReport:
        from openadapt_flow.deployment import DeploymentConfig, PolicySection
        from openadapt_flow.execution_profiles import (
            execution_profile_contract,
            resolve_execution_profile,
        )
        from openadapt_flow.run_gate import (
            build_runtime_authorization,
            evaluate_run_gate,
        )
        from openadapt_flow.runtime import Replayer

        profile = resolve_execution_profile(self.profile)
        gate = evaluate_run_gate(
            workflow,
            bundle_dir=bundle_dir,
            deployment=DeploymentConfig(policy=PolicySection(policy=self.policy)),
            effect_verifier=self.effect_verifier,
            profile_contract=execution_profile_contract(profile),
            effective_durable=True,
            effective_require_settled=True,
        )
        if not gate.passed:
            raise SelfRepairRefused(
                "the run gate refused the bundle; nothing was executed:\n"
                + gate.render()
            )
        authorization = build_runtime_authorization(
            workflow,
            gate,
            approval_source="openadapt-flow-self-repair",
            params=self.params,
            worklists=self.worklists,
        )
        replayer = Replayer(
            session.backend,
            effect_verifier=self.effect_verifier,
            governed_authorization=authorization,
            durable=True,
            require_settled=True,
            approval_gate=approval_gate,
            **self.replayer_kwargs,
        )
        return replayer.run(
            workflow.model_copy(deep=True),
            params=self.params,
            worklists=self.worklists or None,
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            execution_target_kind=self.execution_target_kind,  # type: ignore[arg-type]
            execution_origin=self.execution_origin,
            execution_entry_url=self.entry_url,
        )


# -- the probe gate --------------------------------------------------------------


@dataclass
class ProbeOutcome:
    """What a probe saw at the last point before the first save."""

    reached_save: bool = False
    step_id: Optional[str] = None
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    frame_png: Optional[bytes] = None
    form_state: Optional[tuple[str, str]] = None
    resolved_region: Optional[tuple[int, int, int, int]] = None
    identity_modes: dict[str, str] = field(default_factory=dict)
    report: Optional[RunReport] = None
    run_dir: Optional[Path] = None

    @property
    def passed(self) -> bool:
        return (
            self.reached_save
            and bool(self.checks)
            and all(ok for _, ok, _ in self.checks)
        )

    def failure(self) -> str:
        if not self.reached_save and self.checks:
            return "the probe could not run: " + "; ".join(
                f"{code} ({detail})" for code, _, detail in self.checks
            )
        if not self.reached_save:
            halt = self.report.halt if self.report is not None else None
            where = f" at {halt.state_id}" if halt is not None and halt.state_id else ""
            why = f": {halt.reason}" if halt is not None and halt.reason else ""
            return f"the probe stopped before reaching the save{where}{why}"
        failed = [f"{code} ({detail})" for code, ok, detail in self.checks if not ok]
        return "pre-save check failed: " + "; ".join(failed)


class _ProbeGate(ApprovalGate):
    """Stops at the last point before the first save and checks the state.

    In probe mode every save is refused. In resume mode the first save
    proceeds only when the same checks pass and the form is exactly the one
    the person approved; later saves of the approved bundle proceed.
    """

    def __init__(
        self,
        checks: Callable[[SaveContext], ProbeOutcome],
        *,
        resume_form_state: Optional[tuple[str, str]] = None,
    ) -> None:
        super().__init__("not_gated")
        self._checks = checks
        self._resume_form_state = resume_form_state
        self.outcome: Optional[ProbeOutcome] = None
        self._approved_result: Optional[int] = None

    def before_step(self, ctx: Any) -> GateVerdict:
        return GateVerdict(proceed=True, decision=None, checks=())

    def before_save(self, ctx: SaveContext) -> GateVerdict:
        if not self._is_save(ctx):
            return GateVerdict(proceed=True, decision=None, checks=())
        if self._approved_result is not None:
            # Another input edge of the approved save, or a later save of the
            # approved bundle.
            return GateVerdict(proceed=True, decision=None, checks=())
        if self.outcome is not None:
            return self._stop()
        try:
            self.outcome = self._checks(ctx)
        except Exception as exc:  # noqa: BLE001 - a broken check never saves
            self.outcome = ProbeOutcome(
                reached_save=True,
                step_id=ctx.step.id,
                checks=[("probe_error", False, type(exc).__name__)],
            )
        if (
            self._resume_form_state is not None
            and self.outcome.passed
            and self.outcome.form_state == self._resume_form_state
        ):
            self._approved_result = id(ctx.result)
            return GateVerdict(proceed=True, decision=None, checks=())
        if self._resume_form_state is not None and self.outcome.passed:
            self.outcome.checks.append(
                ("same_as_approved", False, "form_changed_since_approval")
            )
        return self._stop()

    @staticmethod
    def _stop() -> GateVerdict:
        decision = ItemDecision(
            kind=ItemDecisionKind.STOP_BEFORE_SAVING,
            reason=_STOP_REASON,
            needs_person=True,
            counts_toward_problems=False,
            switch_off=None,
        )
        return GateVerdict(proceed=False, decision=decision, checks=())


# -- results -----------------------------------------------------------------------


@dataclass
class SelfRepairResult:
    """The outcome of :meth:`SelfRepair.run`.

    ``status`` is one of ``not_needed`` (the run did not halt),
    ``not_repairable`` (the halt is not one self-repair handles), ``no_fix``
    (no proposal passed its probe), ``needs_fault_probes`` (the step's
    identity can't be judged from pixels and no fault probe was given),
    ``declined`` (the person declined), ``awaiting_approval`` (nobody
    answered in time, or an answer was refused; the candidate waits in the
    store), ``resumed`` (the approved bundle re-ran; see
    ``report.execution_outcome``) or ``resume_failed`` (the approved bundle
    could not run, or an unexpected error stopped self-repair; any canary was
    rolled back).
    """

    status: str
    report: RunReport
    run_dir: Path
    evidence: SelfRepairEvidence
    halted_report: Optional[RunReport] = None
    candidate: Optional[RepairCandidate] = None
    patched_bundle: Optional[Path] = None
    approval_request_id: Optional[str] = None

    @property
    def verified(self) -> bool:
        return self.report.execution_outcome == "VERIFIED"


@dataclass
class _Patch:
    attempt: int
    #: The step whose target the person is shown: the replaced step, or the
    #: halted step a bridge follows.
    step: Step
    workflow: Workflow
    bundle_dir: Path
    evidence_dir: Path
    candidate: RepairCandidate
    target_region: tuple[int, int, int, int]
    #: Every step the patch added or re-targeted; each must resolve on the
    #: deterministic ladder in the probe.
    checked_step_ids: tuple[str, ...] = ()
    #: One line for the person: what the patch adds and changes.
    summary: str = ""


# -- the loop ------------------------------------------------------------------


class SelfRepair:
    """Run an item; when it halts on a changed screen, repair it under a person.

    Args:
        agent: The repair agent (see :class:`RepairAgent`).
        target: How to open the app and run the engine
            (:class:`ReplayTarget` or your own :class:`SelfRepairTarget`).
        store: The repair store the candidate enters.
        screen_access: Who allowed the agent to see screens, and how.
        model_key: The customer's key, required unless the agent sets
            ``requires_model_key = False``.
        max_attempts: Proposals tried for one halt before giving up.
        max_bridge: The most actions one proposal may have (at most 3).
        fault_probes: Fault names passed to ``target.open(fault=...)``. Each
            must stop before the save. Required when the step's identity is
            checked from the DOM rather than from pixels.
        approval_wait_s: How long to wait for the person's answer. ``0``
            asks and returns ``awaiting_approval`` at once.
        canary_runs: The canary window the approved bundle enters.
        on_approval_request: Called with ``(run_dir, request_id,
            request_sha256)`` as soon as a person is asked, for example to
            print the ``item-approval approve`` command or send a
            notification.
        standard_changes: A signed repair policy
            (:class:`~openadapt_flow.repair.standard_change.StandardChanges`).
            A fix it allows applies without a person; any other goes to one.
    """

    def __init__(
        self,
        agent: RepairAgent,
        target: SelfRepairTarget,
        *,
        store: "RepairStore | Path | str",
        screen_access: ScreenAccess,
        model_key: Optional["CustomerModelKey"] = None,
        max_attempts: int = 3,
        max_bridge: int = MAX_BRIDGE,
        fault_probes: Sequence[str] = (),
        approval_wait_s: float = 900.0,
        approval_poll_s: float = 0.5,
        canary_runs: int = 5,
        on_approval_request: Optional[Callable[[Path, str, str], None]] = None,
        standard_changes: Any = None,
        vision: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        from openadapt_flow.jit.adapters import dialects
        from openadapt_flow.repair.lifecycle import RepairStore

        if agent.dialect not in dialects():
            raise SelfRepairRefused(
                f"unknown agent dialect {agent.dialect!r} (expected one of "
                f"{dialects()})"
            )
        if not getattr(agent, "agent_id", None):
            raise SelfRepairRefused("the repair agent needs an agent_id")
        if getattr(agent, "requires_model_key", True) and model_key is None:
            raise SelfRepairRefused(
                "this repair agent calls a model, so it needs the customer's own "
                "model key (openadapt_flow.jit.load_customer_model_key); there is "
                "no OpenAdapt-held key to fall back to"
            )
        if not screen_access.enabled_by.strip():
            raise SelfRepairRefused("screen access needs the name of who enabled it")
        if screen_access.mode == "ask_each_time":
            raise SelfRepairRefused(
                "ask_each_time screen sharing is not available for self-repair; "
                "use scrubbed_only or allow_unscrubbed"
            )
        if not 1 <= int(max_attempts) <= 10:
            raise SelfRepairRefused("max_attempts must be between 1 and 10")
        self.agent = agent
        self.target = target
        self.store = store if isinstance(store, RepairStore) else RepairStore(store)
        self.screen_access = screen_access
        self.model_key = model_key
        self.max_attempts = int(max_attempts)
        if not 1 <= int(max_bridge) <= MAX_BRIDGE:
            raise SelfRepairRefused(f"max_bridge must be between 1 and {MAX_BRIDGE}")
        self.max_bridge = int(max_bridge)
        self.fault_probes = tuple(fault_probes)
        self.approval_wait_s = max(0.0, float(approval_wait_s))
        self.approval_poll_s = max(0.01, float(approval_poll_s))
        self.canary_runs = int(canary_runs)
        self.on_approval_request = on_approval_request
        self.standard_changes = standard_changes
        #: Added to the approval request, e.g. how many queued items it releases.
        self.approval_note = ""
        if vision is None:
            import openadapt_flow.vision as vision
        self.vision = vision
        self._sleep = sleep
        self._monotonic = monotonic
        self._token = secrets.token_urlsafe(32)

    # -- public entry point --------------------------------------------------

    def run(self, bundle_dir: Path | str, run_dir: Path | str) -> SelfRepairResult:
        """Run the item; on a repairable halt, repair it under a person.

        ``run_dir`` holds the item's own run (``report.json``). Self-repair
        work goes under ``run_dir/self-repair/``; the approval request goes in
        ``run_dir/item-approval/`` so the decision portal lists it.
        """

        bundle_dir = Path(bundle_dir)
        run_dir = Path(run_dir)
        work = run_dir / "self-repair"
        workflow = Workflow.load(bundle_dir)
        evidence = SelfRepairEvidence(
            status="not_needed",
            agent_id=self.agent.agent_id,
            agent_dialect=self.agent.dialect,
            prior_bundle_digest=_digest_of(workflow),
        )
        session = self.target.open()
        try:
            report = self.target.execute(workflow, bundle_dir, session, run_dir)
            if report.success or report.halt is None:
                # Not a halt: a healthy run, or one that ended some other way.
                # Self-repair has nothing to do; the agent is never called.
                return self._finish(
                    "not_needed" if report.success else "not_repairable",
                    "" if report.success else "the run did not halt at a step",
                    evidence,
                    report,
                    run_dir,
                    work,
                )
            if report.transaction_outcome not in (None, "HALTED_BEFORE_EFFECT"):
                return self._finish(
                    "not_repairable",
                    f"the run ended {report.transaction_outcome}; self-repair "
                    "handles only a halt before any effect",
                    evidence,
                    report,
                    run_dir,
                    work,
                )
            evidence.halted_run_dir = str(run_dir)
            found = _repairable(report, workflow)
            if isinstance(found, str):
                evidence.steps.append(
                    SelfRepairStepRecord(stage="halt", passed=False, detail=found)
                )
                return self._finish(
                    "not_repairable", found, evidence, report, run_dir, work
                )
            step, mode, halted = found
            evidence.halted_step_id = step.id
            evidence.steps.append(
                SelfRepairStepRecord(
                    stage="halt",
                    passed=True,
                    detail=f"{step.id} halted before any effect ({mode}): "
                    f"{(halted.error or '')[:300]}",
                )
            )
            live_png = session.backend.screenshot()
            halted_form = _form_state(session.backend, self.vision, live_png)
            try:
                patch, probe = self._propose_until_probe_passes(
                    workflow,
                    bundle_dir,
                    step,
                    mode,
                    halted,
                    session,
                    live_png,
                    halted_form,
                    work,
                    evidence,
                )
            except Exception as exc:  # noqa: BLE001 - every failure saves nothing
                detail = (
                    str(exc)
                    if isinstance(exc, SelfRepairRefused)
                    else f"self-repair failed ({type(exc).__name__}); nothing was saved"
                )
                evidence.steps.append(
                    SelfRepairStepRecord(stage="propose", passed=False, detail=detail)
                )
                patch, probe = None, None
        finally:
            _close(session)
        if patch is None or probe is None:
            return self._finish(
                "no_fix",
                evidence.steps[-1].detail if evidence.steps else "no proposal",
                evidence,
                report,
                run_dir,
                work,
                halted_report=report,
            )
        try:
            return self._govern_and_resume(
                workflow, bundle_dir, patch, probe, report, run_dir, work, evidence
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, never half-promoted
            return self._fail_closed(exc, evidence, report, run_dir, work, patch)

    def _fail_closed(
        self,
        exc: Exception,
        evidence: SelfRepairEvidence,
        halted_report: RunReport,
        run_dir: Path,
        work: Path,
        patch: _Patch,
    ) -> SelfRepairResult:
        """An unexpected error after the probe: roll back any canary, stop."""

        from openadapt_flow.repair.lifecycle import RepairLifecycleError

        candidate = None
        if evidence.candidate_id:
            try:
                candidate = self.store.load_candidate(evidence.candidate_id)
                if candidate.state == "canary":
                    candidate = self.store.rollback(AUTOMATION, candidate.candidate_id)
            except RepairLifecycleError:
                pass
        reason = f"self-repair stopped ({type(exc).__name__}: {exc})"[:1024]
        evidence.steps.append(
            SelfRepairStepRecord(stage="resume", passed=False, detail=reason)
        )
        return self._finish(
            "resume_failed",
            reason,
            evidence,
            halted_report,
            run_dir,
            work,
            halted_report=halted_report,
            candidate=candidate,
            patched_bundle=patch.bundle_dir,
        )

    # -- propose / patch / probe ----------------------------------------------

    def _propose_until_probe_passes(
        self,
        workflow: Workflow,
        bundle_dir: Path,
        step: Step,
        mode: str,
        halted: Any,
        session: AppSession,
        live_png: bytes,
        halted_form: Optional[tuple[str, str]],
        work: Path,
        evidence: SelfRepairEvidence,
    ) -> tuple[Optional[_Patch], Optional[ProbeOutcome]]:
        shown_live, scrub_evidence = self._shown(live_png)
        crop = _recorded_crop(workflow, bundle_dir, step)
        shown_crop = self._shown(crop)[0] if crop is not None else None
        expected_png = _expected_crop(workflow, bundle_dir, step)
        if expected_png is not None:
            expected_png = self._shown(expected_png)[0]
        assert step.anchor is not None  # _repairable checked it
        from openadapt_flow.jit.signature import record_check_of

        record_check = record_check_of(workflow, literals=False)
        refusals: list[str] = []
        current: Optional[AppSession] = session
        try:
            for attempt in range(1, self.max_attempts + 1):
                attempt_dir = work / f"attempt-{attempt}"
                if current is None:
                    # The last attempt changed the halted page (a bridge acts as
                    # it is captured). Reach the same halt again, behind the
                    # probe gate, so this proposal starts from the real halt.
                    current = self._reproduce_halt(
                        workflow, bundle_dir, step, attempt_dir / "halt"
                    )
                request = RepairRequest(
                    step_id=step.id,
                    intent=step.intent or "",
                    action=_POINTER_KINDS[step.action],
                    halt_reason=(halted.error or "")[:1000],
                    live_png=shown_live,
                    viewport=_png_size(live_png),
                    recorded_label=step.anchor.ocr_text
                    or (
                        step.anchor.structural.name if step.anchor.structural else None
                    ),
                    recorded_region=tuple(step.anchor.region),  # type: ignore[arg-type]
                    recorded_click_point=tuple(step.anchor.click_point),  # type: ignore[arg-type]
                    recorded_crop_png=shown_crop,
                    dialect=self.agent.dialect,
                    attempt=attempt,
                    previous_refusals=tuple(refusals),
                    model_key=self.model_key,
                    mode=mode,
                    max_actions=self.max_bridge,
                    expected_texts=tuple(
                        e.text for e in step.expect if e.text and mode == "after"
                    ),
                    expected_png=expected_png if mode == "after" else None,
                    param_values=_typable_params(workflow, self._params()),
                    record_check=record_check,
                )
                evidence.attempts = attempt
                try:
                    proposal = self.agent.propose(request)
                except Exception as exc:  # noqa: BLE001 - an agent failure is a refusal
                    proposal = None
                    detail = f"the agent raised {type(exc).__name__}"
                else:
                    detail = "the agent gave up" if proposal is None else ""
                calls = max(int(getattr(proposal, "model_calls", 0) or 0), 0)
                cost = max(float(getattr(proposal, "cost_usd", 0.0) or 0.0), 0.0)
                evidence.model_calls += calls
                evidence.est_model_cost_usd = round(
                    evidence.est_model_cost_usd + cost, 6
                )
                if proposal is None:
                    evidence.steps.append(
                        SelfRepairStepRecord(
                            stage="propose",
                            passed=False,
                            detail=detail,
                            attempt=attempt,
                            model_calls=calls,
                            est_model_cost_usd=cost,
                        )
                    )
                    return None, None
                try:
                    shown = json.dumps(
                        [dict(a) for a in proposal.bridge()], sort_keys=True
                    )
                except SelfRepairRefused as exc:
                    shown = str(exc)
                evidence.steps.append(
                    SelfRepairStepRecord(
                        stage="propose",
                        passed=True,
                        detail=shown[:500],
                        attempt=attempt,
                        model_calls=calls,
                        est_model_cost_usd=cost,
                    )
                )
                touched = [False]
                try:
                    patch = self._patch(
                        workflow,
                        bundle_dir,
                        step,
                        mode,
                        proposal,
                        current,
                        live_png,
                        attempt,
                        attempt_dir,
                        scrub_evidence,
                        touched,
                        _before_frame(work.parent, halted),
                    )
                except Exception as exc:  # noqa: BLE001 - a bad proposal is a refusal
                    detail = (
                        str(exc)
                        if isinstance(exc, SelfRepairRefused)
                        else f"the proposal could not be applied ({type(exc).__name__})"
                    )
                    refusals.append(detail)
                    evidence.steps.append(
                        SelfRepairStepRecord(
                            stage="patch", passed=False, detail=detail, attempt=attempt
                        )
                    )
                    if touched[0]:
                        _close(current)
                        current = None
                    continue
                if touched[0]:
                    _close(current)
                    current = None
                evidence.steps.append(
                    SelfRepairStepRecord(
                        stage="patch",
                        passed=True,
                        detail=f"{patch.summary}; bundle "
                        f"{patch.candidate.proposed_content_digest[:16]}"[:1024],
                        attempt=attempt,
                    )
                )
                probe = self._probe(
                    patch,
                    attempt_dir / "probe",
                    halted_step_id=step.id,
                    halted_form=halted_form,
                )
                if probe.passed:
                    evidence.steps.append(
                        SelfRepairStepRecord(
                            stage="probe",
                            passed=True,
                            detail="stopped before the save; "
                            + ", ".join(code for code, _, _ in probe.checks),
                            attempt=attempt,
                        )
                    )
                    return patch, probe
                reason = probe.failure()
                refusals.append(reason)
                evidence.steps.append(
                    SelfRepairStepRecord(
                        stage="probe",
                        passed=False,
                        detail=reason[:1000],
                        attempt=attempt,
                    )
                )
            return None, None
        finally:
            if current is not session:
                _close(current)

    def _reproduce_halt(
        self, workflow: Workflow, bundle_dir: Path, step: Step, run_dir: Path
    ) -> AppSession:
        """A fresh session halted at ``step`` again, behind the probe gate."""

        def never(ctx: SaveContext) -> ProbeOutcome:
            return ProbeOutcome(reached_save=True, step_id=ctx.step.id)

        session = self.target.open()
        try:
            report = self.target.execute(
                workflow, bundle_dir, session, run_dir, approval_gate=_ProbeGate(never)
            )
        except BaseException:
            _close(session)
            raise
        if report.halt is None or report.halt.state_id != step.id:
            _close(session)
            raise SelfRepairRefused(
                f"the halt at {step.id} did not happen again in a fresh session"
            )
        return session

    def _shown(self, png: bytes) -> tuple[bytes, Optional[ScreenScrubEvidence]]:
        """The image the agent may see under the screen-access mode."""

        if self.screen_access.mode == "allow_unscrubbed":
            return png, None
        scrub = self.screen_access.scrub
        if scrub is None:
            from openadapt_flow.agent.repair import default_scrubber, scrub_png

            def scrub(data: bytes) -> tuple[bytes, Mapping[str, Any]]:
                return scrub_png(data, default_scrubber)

        try:
            scrubbed, info = scrub(png)
        except Exception as exc:  # noqa: BLE001 - no scrub, no screen
            raise SelfRepairRefused(
                f"the screen could not be scrubbed ({type(exc).__name__}); under "
                "scrubbed_only no screen is shared"
            ) from exc
        return scrubbed, ScreenScrubEvidence(
            provider=str(info.get("provider") or "unknown"),
            package_version=str(info.get("package_version") or "unknown"),
            policy_sha256=info.get("policy_sha256"),
        )

    def _patch(
        self,
        workflow: Workflow,
        bundle_dir: Path,
        step: Step,
        mode: str,
        proposal: RepairProposal,
        session: AppSession,
        live_png: bytes,
        attempt: int,
        attempt_dir: Path,
        scrub_evidence: Optional[ScreenScrubEvidence],
        touched: list[bool],
        halted_before: Optional[bytes] = None,
    ) -> _Patch:
        """Build the patched bundle from the proposal, on the halted page.

        Every action before the last (and, in ``after`` mode, the last too)
        becomes a new step: it is checked, captured from the live screen and
        then performed, so the next action sees the screen it will act on. A
        new step may never save, and may type only a parameter's value or
        text the screen shows. In ``replace`` mode the last action is never
        performed; it becomes the halted step's new target.
        """

        from openadapt_flow.compiler.compile import _postconditions
        from openadapt_flow.repair.registration import build_candidate
        from openadapt_flow.run_gate import is_consequential
        from openadapt_flow.runtime.heal import persist_heal

        assert step.anchor is not None
        natives = proposal.bridge()
        if not natives:
            raise SelfRepairRefused("the proposal has no action")
        if len(natives) > self.max_bridge:
            raise SelfRepairRefused(
                f"a bridge is at most {self.max_bridge} action(s); this one has "
                f"{len(natives)}"
            )
        ops = [self._one_op(native) for native in natives]
        if len(ops) > 1 or mode == "after":
            if workflow.program is not None or workflow.subflows:
                raise SelfRepairRefused(
                    "a bridge can only be added to a linear workflow for now"
                )
        if mode == "replace" and ops[-1].kind != _POINTER_KINDS[step.action]:
            raise SelfRepairRefused(
                f"{step.id} is a {_POINTER_KINDS[step.action]}; the bridge's last "
                f"action is a {ops[-1].kind}"
            )
        params = _typable_params(workflow, self._params())
        others = {
            s.anchor.structural.selector: s.id
            for s in workflow.steps
            if s.id != step.id
            and s.anchor is not None
            and s.anchor.structural is not None
            and s.anchor.structural.selector
        }
        evidence_dir = attempt_dir / "evidence"
        new_steps: list[Step] = []
        crops: dict[str, bytes] = {}
        described: list[str] = []
        screen = live_png
        replaced: Optional[Step] = None
        target_region = tuple(step.anchor.region)
        for index, op in enumerate(ops):
            is_target = mode == "replace" and index == len(ops) - 1
            if op.kind == "type":
                text = op.text or ""
                param = next((n for n, v in params.items() if v == text and v), None)
                if param is None and not _shows(
                    session.backend, self.vision, screen, text
                ):
                    raise SelfRepairRefused(
                        f"the bridge types {text!r}, which is neither a parameter's "
                        "value nor text on the screen; refusing an invented value"
                    )
                new_id = f"{step.id}_bridge_{index + 1}"
                new_steps.append(
                    Step(
                        id=new_id,
                        intent=f"type <{param}>" if param else f"type {text!r}",
                        action=ActionKind.TYPE,
                        param=param,
                        text=None if param else text,
                        risk="reversible",
                    )
                )
                described.append(f"types <{param}>" if param else f"types {text!r}")
                touched[0] = True
                session.backend.type_text(text)
                screen = self._settled(session)
                continue
            point = self._point_for(op, session, screen)
            locate = getattr(session.backend, "structural_locator_at", None)
            structural = None
            if callable(locate):
                try:
                    structural = locate(int(point[0]), int(point[1]))
                except Exception:  # noqa: BLE001 - no DOM locator, visual anchor only
                    structural = None
            if structural is not None and structural.selector in others:
                raise SelfRepairRefused(
                    f"the proposed control is the one {others[structural.selector]} "
                    "already uses; refusing a patch that reuses another step's target"
                )
            if is_target:
                replaced, crop, region, label, event = self._retarget(
                    workflow, step, point, structural, screen
                )
                crops[step.id] = crop
                target_region = region
                described.append(f"{step.id} now uses {label!r}")
                persist_heal(event, crop, screen, evidence_dir)
                continue
            new_id = f"{step.id}_bridge_{index + 1}"
            new_step, crop, label = self._new_click(
                workflow,
                new_id,
                op,
                point,
                structural,
                screen,
                step.anchor.region,
                session,
            )
            crops[new_id] = crop
            described.append(f"clicks {label!r}")
            frame_dir = evidence_dir / "heals" / new_id
            frame_dir.mkdir(parents=True, exist_ok=True)
            (frame_dir / "screen.png").write_bytes(screen)
            touched[0] = True
            session.backend.click(
                int(point[0]), int(point[1]), double=op.kind == "double_click"
            )
            after = self._settled(session)
            # What the click changed on screen is the new step's postcondition,
            # derived the way the compiler derives a recorded step's.
            staging = evidence_dir / "expect"
            (staging / "templates").mkdir(parents=True, exist_ok=True)
            new_step.expect = _postconditions(
                screen,
                after,
                exclude_texts=tuple(params.values()),
                bundle=staging,
                step_id=new_id,
                click_point=point,
            )
            new_steps.append(new_step)
            screen = after

        patched = workflow.model_copy(deep=True)
        steps = list(patched.steps)
        at = next(i for i, s in enumerate(steps) if s.id == step.id)
        if mode == "replace":
            assert replaced is not None
            steps[at] = replaced
            steps[at:at] = new_steps
            shown_step = replaced
            summary = (
                f"before {step.id}: " + ", ".join(described[:-1]) + "; "
                if len(described) > 1
                else ""
            ) + described[-1]
        else:
            # The halted step's expected screen now comes after the bridge.
            # The halted step now leads to the new screen it reached; that
            # screen is its postcondition, derived like a recorded step's.
            leads_to = _postconditions(
                halted_before,
                live_png,
                exclude_texts=tuple(params.values()),
                bundle=evidence_dir / "expect",
                step_id=step.id + "_now",
                click_point=step.anchor.click_point,
            )
            if not leads_to:
                leads_to = [
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text=new_steps[0].intent.removeprefix("click ").strip("'\""),
                    )
                ]
            halted = steps[at].model_copy(update={"expect": leads_to})
            last = new_steps[-1].model_copy(update={"expect": list(step.expect)})
            steps[at] = halted
            steps[at + 1 : at + 1] = [*new_steps[:-1], last]
            new_steps[-1] = last
            shown_step = halted
            summary = f"after {step.id}: " + ", ".join(described)
        if any(is_consequential(s, patched) for s in new_steps):
            raise SelfRepairRefused(
                "a new step of the bridge would save; a bridge may only lead to "
                "the recorded save"
            )
        patched.steps = steps
        bundle_out = attempt_dir / "bundle"
        if bundle_out.exists():
            shutil.rmtree(bundle_out)
        shutil.copytree(
            bundle_dir,
            bundle_out,
            ignore=shutil.ignore_patterns("repair", "manifest.json"),
        )
        staged = evidence_dir / "expect" / "templates"
        if staged.is_dir():
            for path in staged.iterdir():
                shutil.copy2(path, bundle_out / "templates" / path.name)
        for step_id, crop in crops.items():
            target = next(s for s in steps if s.id == step_id)
            assert target.anchor is not None
            path = bundle_out / target.anchor.template
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(crop)
        patched.save(bundle_out, encrypt=bool(workflow.encrypted))
        run_dir = attempt_dir.parent.parent
        halted_report = run_dir / "report.json"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        if halted_report.is_file():
            shutil.copy2(halted_report, evidence_dir / "report.json")
        candidate = build_candidate(
            bundle_dir,
            bundle_out,
            source="model_suggestion",
            evidence_run_dir=evidence_dir,
        )
        if candidate.state == "rejected":
            shutil.rmtree(bundle_out, ignore_errors=True)
            raise SelfRepairRefused(
                candidate.rejection_reason or "the patch weakens the contract"
            )
        candidate.taught_steps = [
            TaughtStep(
                step_id=s.id,
                frame_sha256=sha256_hex(
                    (evidence_dir / "heals" / s.id / "screen.png").read_bytes()
                ),
            )
            for s in new_steps
            if s.anchor is not None
        ]
        candidate.provenance = RepairProvenance(
            repaired_by="self_repair",
            agent_id=self.agent.agent_id,
            halted_run_id=run_dir.name or "run",
            halted_step_id=step.id,
            repair_token_sha256=hashlib.sha256(self._token.encode()).hexdigest(),
            screen_access_enabled_by=self.screen_access.enabled_by,
            screen_share_mode=self.screen_access.mode,
            screens_shared=1,
            screens_scrubbed=self.screen_access.mode != "allow_unscrubbed",
            scrub_evidence=scrub_evidence,
        )
        checked = tuple(s.id for s in new_steps if s.anchor is not None) + (
            (step.id,) if mode == "replace" else ()
        )
        return _Patch(
            attempt=attempt,
            step=shown_step,
            workflow=Workflow.load(bundle_out),
            bundle_dir=bundle_out,
            evidence_dir=evidence_dir,
            candidate=candidate,
            target_region=target_region,  # type: ignore[arg-type]
            checked_step_ids=checked,
            summary=summary,
        )

    def _retarget(
        self,
        workflow: Workflow,
        step: Step,
        point: tuple[int, int],
        structural: Any,
        screen: bytes,
    ) -> tuple[Step, bytes, tuple[int, int, int, int], Optional[str], Any]:
        """The halted step with a new target at ``point`` on ``screen``."""

        from openadapt_flow.compiler.compile import _landmarks_for
        from openadapt_flow.risk import is_meaningful_label, is_write_shaped
        from openadapt_flow.run_gate import is_consequential
        from openadapt_flow.runtime.heal import _reocr_text, build_heal_event

        assert step.anchor is not None
        size = _png_size(screen)
        region = _centered_region(
            point, (step.anchor.region[2], step.anchor.region[3]), size
        )
        event, crop = build_heal_event(
            step,
            Resolution(rung="grounder", point=point, confidence=1.0, elapsed_ms=0.0),
            region,
            screen,
            self.vision,
        )
        # What the control at the point is called now: its accessible name,
        # else what OCR reads there. Never the recorded label, which the heal
        # event keeps when OCR finds nothing.
        read = _reocr_text(self.vision, screen, region, click_y=point[1])
        label = (structural.name if structural is not None else None) or read
        if is_consequential(step, workflow):
            if not is_meaningful_label(label) or not is_write_shaped(label or ""):
                raise SelfRepairRefused(
                    f"{step.id} saves, but the proposed control "
                    f"{label!r} does not read as a save; refusing a patch that "
                    "would point a save at another kind of control"
                )
        elif label and is_write_shaped(label):
            raise SelfRepairRefused(
                f"{step.id} does not save, but the proposed control {label!r} "
                "reads as a save; a patch may not turn a read step into a write"
            )
        landmarks = self._landmarks(_landmarks_for, workflow, screen, region, point)
        anchor = event.new_anchor.model_copy(
            update={
                "template": f"templates/{step.id}_repair.png",
                "structural": _patched_structural(step, structural),
                "ocr_text": read,
                "landmarks": landmarks,
            }
        )
        event.new_anchor = anchor
        return step.model_copy(update={"anchor": anchor}), crop, region, label, event

    def _new_click(
        self,
        workflow: Workflow,
        step_id: str,
        op: Any,
        point: tuple[int, int],
        structural: Any,
        screen: bytes,
        size_like: tuple[int, int, int, int],
        session: AppSession,
    ) -> tuple[Step, bytes, str]:
        """A new bridge click: a readable control in the recorded record.

        The control must not read as a save, and the record identity the DOM
        shows under it must be one the recording already verified (the same
        salted hash), so the new step carries an identity check of its own.
        """

        from openadapt_flow.ir import Anchor
        from openadapt_flow.risk import is_meaningful_label, is_write_shaped
        from openadapt_flow.runtime.heal import _crop_png, _reocr_text

        region = _centered_region(
            point, (size_like[2], size_like[3]), _png_size(screen)
        )
        read = _reocr_text(self.vision, screen, region, click_y=point[1])
        label = (structural.name if structural is not None else None) or read
        if not is_meaningful_label(label):
            raise SelfRepairRefused(
                f"a bridge click needs a readable control; {label!r} is not one "
                "(an unlabeled control may save)"
            )
        assert label is not None
        if is_write_shaped(label):
            raise SelfRepairRefused(
                f"the bridge clicks {label!r}, which reads as a save; a bridge may "
                "only lead to the recorded save"
            )
        anchor = Anchor(
            template=f"templates/{step_id}.png",
            structural=structural,
            region=region,
            click_point=point,
            ocr_text=read,
            identity_template=_recorded_identity(workflow, session.backend, point),
        )
        step = Step(
            id=step_id,
            intent=f"click {label!r}",
            action=ActionKind.DOUBLE_CLICK
            if op.kind == "double_click"
            else ActionKind.CLICK,
            anchor=anchor,
            risk="reversible",
        )
        return step, _crop_png(screen, region), label

    def _landmarks(
        self,
        landmarks_for: Callable[..., Any],
        workflow: Workflow,
        screen: bytes,
        region: tuple[int, int, int, int],
        point: tuple[int, int],
    ) -> list[Any]:
        typed = tuple(v for v in _typed_values(workflow, self._params()).values() if v)
        try:
            return list(
                landmarks_for(
                    self.vision.ocr(screen), region, point, exclude_texts=typed
                )
            )
        except Exception:  # noqa: BLE001 - landmarks are optional aids
            return []

    def _settled(self, session: AppSession) -> bytes:
        try:
            frame = self.vision.wait_settled(session.backend)
        except Exception:  # noqa: BLE001 - fall back to a plain screenshot
            frame = None
        return frame or session.backend.screenshot()

    def _one_op(self, native: Mapping[str, Any]) -> Any:
        """The single acting step one native action maps to."""

        from openadapt_flow.jit.adapters import UnsupportedAction, translate

        try:
            ops = translate(self.agent.dialect, native)
        except (UnsupportedAction, ValueError, TypeError) as exc:
            raise SelfRepairRefused(f"the proposal is not one action: {exc}") from exc
        acting = [op for op in ops if op.kind not in ("observe", "wait")]
        if len(acting) != 1:
            raise SelfRepairRefused(
                f"each repair action is exactly one step; one has {len(acting)}"
            )
        op = acting[0]
        if op.kind not in ("click", "double_click", "type"):
            raise SelfRepairRefused(f"a repair action may not be a {op.kind}")
        if op.kind == "type" and (op.target is not None or op.fill):
            raise SelfRepairRefused("click the field first, then type into it")
        return op

    def _point_for(
        self, op: Any, session: AppSession, screen: bytes
    ) -> tuple[int, int]:
        if op.target is not None:
            page = getattr(session.backend, "page", None)
            if page is None:
                raise SelfRepairRefused("no page to resolve the proposed selector")
            try:
                locator = page.locator(op.target)
                count = locator.count()
                box = locator.bounding_box() if count == 1 else None
            except Exception as exc:  # noqa: BLE001 - an invalid selector
                raise SelfRepairRefused(
                    f"the proposed selector is not usable ({type(exc).__name__})"
                ) from exc
            if count != 1:
                raise SelfRepairRefused(
                    f"the proposed selector matches {count} elements"
                )
            if box is None:
                raise SelfRepairRefused("the proposed element is not visible")
            point = (
                int(box["x"] + box["width"] / 2),
                int(box["y"] + box["height"] / 2),
            )
        elif op.x is None or op.y is None:
            raise SelfRepairRefused("the proposal names no point or element")
        else:
            point = (int(op.x), int(op.y))
        width, height = _png_size(screen)
        if not (0 <= point[0] < width and 0 <= point[1] < height):
            raise SelfRepairRefused(f"the proposed point {point} is off the screen")
        return point

    def _params(self) -> Mapping[str, Any]:
        return dict(getattr(self.target, "params", None) or {})

    def _probe(
        self,
        patch: _Patch,
        run_dir: Path,
        *,
        halted_step_id: str,
        halted_form: Optional[tuple[str, str]],
        fault: Optional[str] = None,
        resume_form_state: Optional[tuple[str, str]] = None,
    ) -> ProbeOutcome:
        """Run the patched bundle to the last point before the first save."""

        params = self._params()
        workflow = patch.workflow

        def checks(ctx: SaveContext) -> ProbeOutcome:
            # Called by the gate during execute(), after the session opened.
            assert session is not None
            return _pre_save_checks(
                ctx,
                workflow,
                params,
                checked_step_ids=patch.checked_step_ids or (patch.step.id,),
                halted_step_id=halted_step_id,
                halted_form=halted_form,
                backend=session.backend,
                vision=self.vision,
            )

        gate = _ProbeGate(checks, resume_form_state=resume_form_state)
        session: Optional[AppSession] = None
        try:
            session = self.target.open(fault)
            report = self.target.execute(
                workflow, patch.bundle_dir, session, run_dir, approval_gate=gate
            )
        except Exception as exc:  # noqa: BLE001 - a run that can't start saves nothing
            detail = (
                str(exc)[:300]
                if isinstance(exc, SelfRepairRefused)
                else f"the run could not start ({type(exc).__name__})"
            )
            outcome = ProbeOutcome(checks=[("run_gate", False, detail)])
            outcome.run_dir = run_dir
            return outcome
        finally:
            _close(session)
        outcome = gate.outcome or ProbeOutcome()
        outcome.report = report
        outcome.run_dir = run_dir
        return outcome

    # -- campaigns, approval, resume ------------------------------------------

    def _govern_and_resume(
        self,
        workflow: Workflow,
        bundle_dir: Path,
        patch: _Patch,
        probe: ProbeOutcome,
        halted_report: RunReport,
        run_dir: Path,
        work: Path,
        evidence: SelfRepairEvidence,
    ) -> SelfRepairResult:
        from openadapt_flow.repair.lifecycle import (
            RepairLifecycleError,
        )

        candidate = self.store.add_candidate(patch.candidate)
        evidence.candidate_id = candidate.candidate_id
        evidence.patched_bundle_digest = candidate.proposed_content_digest
        if candidate.state != "candidate":
            # The store already holds this exact patch (same digests) further
            # along or rejected; self-repair never moves it on its own.
            return self._finish(
                "no_fix",
                f"this exact patch is already {candidate.state} in the repair store",
                evidence,
                halted_report,
                run_dir,
                work,
                halted_report=halted_report,
                candidate=candidate,
                patched_bundle=patch.bundle_dir,
            )
        replay, fault = self._campaigns(patch, probe, work, evidence)
        for result in (replay, fault):
            if result is not None:
                candidate = self.store.record_campaign(
                    candidate.candidate_id, result, AUTOMATION
                )
        if replay is None or not replay.passed or fault is None or not fault.passed:
            status = "needs_fault_probes" if fault is None else "no_fix"
            reason = (
                "the step's identity is checked from the DOM, which the frame "
                "campaigns can't read; give fault probes so the fault campaign "
                "can run"
                if fault is None
                else "a campaign failed: "
                + "; ".join(
                    r.summary()
                    for r in (replay, fault)
                    if r is not None and not r.passed
                )
            )
            return self._finish(
                status,
                reason,
                evidence,
                halted_report,
                run_dir,
                work,
                halted_report=halted_report,
                candidate=candidate,
                patched_bundle=patch.bundle_dir,
            )
        if self.standard_changes is not None:
            standard = self.standard_changes.apply(
                self,
                patch=patch,
                probe=probe,
                candidate=candidate,
                work=work,
                evidence=evidence,
            )
            if standard is not None:
                return self._resume(
                    patch, probe, standard, halted_report, run_dir, work, evidence, None
                )
        request_id, answer, why = self._ask_person(patch, probe, candidate, run_dir)
        if answer is None:
            evidence.steps.append(
                SelfRepairStepRecord(
                    stage="approval",
                    passed=False,
                    detail=f"{why}; the candidate waits in the repair store and "
                    "nothing was saved",
                )
            )
            return self._finish(
                "awaiting_approval",
                why,
                evidence,
                halted_report,
                run_dir,
                work,
                halted_report=halted_report,
                candidate=self.store.load_candidate(candidate.candidate_id),
                patched_bundle=patch.bundle_dir,
                approval_request_id=request_id,
            )
        decision, approver, route = answer
        evidence.approval_route = route
        if decision != "approve":
            try:
                candidate = self.store.reject(
                    candidate.candidate_id,
                    Actor(kind="human", id=approver),
                    "declined by a person during self-repair",
                )
            except RepairLifecycleError:
                pass
            evidence.steps.append(
                SelfRepairStepRecord(
                    stage="approval",
                    passed=False,
                    detail=f"declined by {approver}; nothing was saved",
                )
            )
            return self._finish(
                "declined",
                f"declined by {approver}",
                evidence,
                halted_report,
                run_dir,
                work,
                halted_report=halted_report,
                candidate=candidate,
                patched_bundle=patch.bundle_dir,
                approval_request_id=request_id,
            )
        person = Actor(kind="human", id=approver)
        try:
            candidate = self._approve(candidate.candidate_id, person, replay, fault)
        except (RepairLifecycleError, SelfRepairRefused) as exc:
            evidence.steps.append(
                SelfRepairStepRecord(
                    stage="approval",
                    passed=False,
                    detail=f"the repair store refused: {exc}"[:1024],
                )
            )
            return self._finish(
                "no_fix",
                f"the repair store refused the approval: {exc}",
                evidence,
                halted_report,
                run_dir,
                work,
                halted_report=halted_report,
                candidate=self.store.load_candidate(candidate.candidate_id),
                patched_bundle=patch.bundle_dir,
                approval_request_id=request_id,
            )
        assert candidate.approval is not None
        evidence.approved_by = candidate.approval.approved_by
        evidence.steps.append(
            SelfRepairStepRecord(
                stage="approval",
                passed=True,
                detail=f"reviewed and approved by {candidate.approval.approved_by} "
                f"({route})",
            )
        )
        return self._resume(
            patch, probe, candidate, halted_report, run_dir, work, evidence, request_id
        )

    def _resume(
        self,
        patch: _Patch,
        probe: ProbeOutcome,
        candidate: RepairCandidate,
        halted_report: RunReport,
        run_dir: Path,
        work: Path,
        evidence: SelfRepairEvidence,
        request_id: Optional[str],
    ) -> SelfRepairResult:
        """Re-run the item from the start with the approved bundle."""

        from openadapt_flow.repair.lifecycle import (
            RepairLifecycleError,
            observation_from_report,
        )

        resume_dir = work / "resume"
        resumed = self._probe(
            patch,
            resume_dir,
            halted_step_id=patch.step.id,
            halted_form=probe.form_state,
            resume_form_state=probe.form_state,
        )
        report = resumed.report
        if report is None:
            # The approved bundle never ran: put the prior bundle back so
            # nothing else picks up a bundle with no canary run behind it.
            reason = resumed.failure()
            try:
                candidate = self.store.rollback(AUTOMATION, candidate.candidate_id)
            except RepairLifecycleError:
                pass
            evidence.steps.append(
                SelfRepairStepRecord(
                    stage="resume",
                    passed=False,
                    detail=f"{reason}; the canary was rolled back"[:1024],
                )
            )
            return self._finish(
                "resume_failed",
                f"the resumed run could not start ({reason}); the canary was "
                "rolled back",
                evidence,
                halted_report,
                run_dir,
                work,
                halted_report=halted_report,
                candidate=self.store.load_candidate(candidate.candidate_id),
                patched_bundle=patch.bundle_dir,
                approval_request_id=request_id,
            )
        candidate = self.store.record_canary_run(
            candidate.candidate_id, observation_from_report(report)
        )
        evidence.steps.append(
            SelfRepairStepRecord(
                stage="resume",
                passed=report.success
                and report.execution_outcome in (None, "VERIFIED"),
                detail=f"{report.execution_outcome} / {report.transaction_outcome}; "
                f"{report.model_calls} model call(s) in the run",
            )
        )
        return self._finish(
            "resumed",
            "",
            evidence,
            report,
            resume_dir,
            work,
            halted_report=halted_report,
            candidate=candidate,
            patched_bundle=patch.bundle_dir,
            approval_request_id=request_id,
        )

    def _approve(
        self,
        candidate_id: str,
        person: Actor,
        replay: CampaignResult,
        fault: CampaignResult,
    ) -> RepairCandidate:
        """Review, campaigns, approval, staging and canary, in store order.

        The person's one answer is both the review and the approval. The
        campaign results computed before they were asked advance the state;
        automation stages the bundles and starts the canary window.
        """

        candidate = self.store.review(candidate_id, person)
        for result in (replay, fault):
            candidate = self.store.record_campaign(candidate_id, result, AUTOMATION)
        candidate = self.store.approve(candidate_id, person)
        if candidate.state != "approved" or candidate.approval is None:
            raise SelfRepairRefused(f"the candidate is {candidate.state}, not approved")
        self.store.stage(candidate_id, AUTOMATION)
        return self.store.start_canary(
            candidate_id, AUTOMATION, max_runs=self.canary_runs
        )

    def _campaigns(
        self,
        patch: _Patch,
        probe: ProbeOutcome,
        work: Path,
        evidence: SelfRepairEvidence,
    ) -> tuple[Optional[CampaignResult], Optional[CampaignResult]]:
        """The replay and fault campaigns for this candidate.

        Frame campaigns when the step's identity is readable from pixels;
        otherwise the live probe and the fault probes.
        """

        anchor = patch.step.anchor
        assert anchor is not None
        template = anchor.identity_template
        added = any(step_id != patch.step.id for step_id in patch.checked_step_ids)
        # Frame campaigns judge one changed binding against its identity band.
        # They can't judge a step whose identity is read from the DOM, or a new
        # step with no identity of its own: those get the live campaigns.
        pixel_readable = not added and bool(
            anchor.context_text or (template is not None and template.tokens)
        )
        live_case = CampaignCaseResult(
            label=f"{patch.step.id}:live_probe",
            kind="live_probe",
            passed=probe.passed,
            detail="stopped before the save with every pre-save check passing",
        )
        if pixel_readable:
            replay = _frame_campaign(patch.candidate, "replay")
            replay = replay.model_copy(
                update={
                    "cases": [*replay.cases, live_case],
                    "total": replay.total + 1,
                    "failed": replay.failed + (0 if probe.passed else 1),
                    "passed": replay.passed and probe.passed,
                }
            )
            fault: Optional[CampaignResult] = _frame_campaign(patch.candidate, "fault")
        else:
            replay = CampaignResult(
                campaign="replay",
                passed=probe.passed,
                total=1,
                failed=0 if probe.passed else 1,
                cases=[live_case],
            )
            fault = (
                self._fault_probes(patch, probe, work) if self.fault_probes else None
            )
        for result in (replay, fault):
            if result is not None:
                evidence.steps.append(
                    SelfRepairStepRecord(
                        stage="campaign", passed=result.passed, detail=result.summary()
                    )
                )
        return replay, fault

    def _fault_probes(
        self, patch: _Patch, probe: ProbeOutcome, work: Path
    ) -> CampaignResult:
        cases: list[CampaignCaseResult] = []
        for index, fault in enumerate(self.fault_probes, start=1):
            outcome = self._probe(
                patch,
                work / f"fault-{index}",
                halted_step_id=patch.step.id,
                halted_form=probe.form_state,
                fault=fault,
            )
            report = outcome.report
            wrote = report is not None and any(
                evidence.observed_effect == "present"
                for result in report.results
                for evidence in result.effect_evidence
            )
            refused = not outcome.passed and not wrote
            cases.append(
                CampaignCaseResult(
                    label=f"fault-probe-{index}",
                    kind="live_fault_probe",
                    passed=refused,
                    detail=(
                        f"refused before the save ({outcome.failure()[:400]})"
                        if refused
                        else "reached the save with every check passing on a fault"
                    ),
                )
            )
        failed = sum(1 for case in cases if not case.passed)
        return CampaignResult(
            campaign="fault",
            passed=bool(cases) and failed == 0,
            total=len(cases),
            failed=failed,
            cases=cases,
        )

    def _ask_person(
        self,
        patch: _Patch,
        probe: ProbeOutcome,
        candidate: RepairCandidate,
        run_dir: Path,
    ) -> tuple[Optional[str], Optional[tuple[str, str, str]], str]:
        """Ask one person: ``(request_id, (decision, approver, route), why)``.

        The answer is ``None`` when none counts; ``why`` then says whether
        nobody answered in time or an answer was refused.

        The request goes in the halted run's ``item-approval/`` folder, where
        the decision portal and ``openadapt-flow item-approval`` find it.
        Rejecting the candidate in the store (``RepairStore.reject``) ends
        the wait as a decline.
        """

        from openadapt_flow.approval_mode.approver import DEFAULT_APPROVER_POLICY
        from openadapt_flow.approval_mode.item_approval import (
            ItemApprovalRefused,
            ItemApprovalStore,
            answer_refusal,
        )
        from openadapt_flow.approval_mode.model import ApprovalMode

        store = ItemApprovalStore(run_dir)
        store.write_policy(DEFAULT_APPROVER_POLICY)
        assert probe.frame_png is not None and probe.form_state is not None
        label = (
            patch.step.anchor.structural.name
            if patch.step.anchor and patch.step.anchor.structural
            else (patch.step.anchor.ocr_text if patch.step.anchor else None)
        )
        request = store.open_request(
            run_id=run_dir.name or "run",
            workflow_name=patch.workflow.name,
            step_id=patch.step.id,
            step_index=next(
                (
                    i
                    for i, s in enumerate(patch.workflow.steps)
                    if s.id == patch.step.id
                ),
                0,
            ),
            step_intent=(
                f"Accept a repair: {patch.summary or label}. Approving saves "
                f"this item. Candidate {candidate.candidate_id}."
                + (f" {self.approval_note}" if self.approval_note else "")
            ),
            mode=ApprovalMode.EACH_ITEM,
            bundle_content_digest=candidate.proposed_content_digest,
            runtime_inputs_digest=None,
            frame_png=_outline(
                probe.frame_png, probe.resolved_region or patch.target_region
            ),
            form_state=probe.form_state,
        )
        if self.on_approval_request is not None:
            try:
                self.on_approval_request(run_dir, request.request_id, request.digest())
            except Exception:  # noqa: BLE001 - a notifier never decides anything
                pass
        if self.approval_wait_s <= 0:
            return request.request_id, None, "no wait was configured"
        deadline = self._monotonic() + self.approval_wait_s
        while True:
            try:
                answer = store.read_answer(request.request_id)
            except ItemApprovalRefused:
                answer = None
            if answer is not None:
                refusal = answer_refusal(request, answer, policy=store.context())
                identity = answer.approver_identity()
                if (
                    refusal is not None
                    or answer.decided_by != "human"
                    or not (identity.is_person)
                ):
                    # Only a person's answer counts, and a refused answer is
                    # final for this request: nothing is saved.
                    code = refusal or "not_a_person"
                    return request.request_id, None, f"an answer was refused ({code})"
                store.consume(request.request_id)
                approver = answer.approver_id or f"{answer.route}:person"
                return (
                    request.request_id,
                    (answer.decision, approver, answer.route),
                    "",
                )
            current = self.store.load_candidate(candidate.candidate_id)
            if current.state == "rejected":
                return (
                    request.request_id,
                    ("decline", "repair_store:person", "repair_store"),
                    "",
                )
            if self._monotonic() >= deadline or request.expired():
                return request.request_id, None, "nobody answered in time"
            self._sleep(self.approval_poll_s)

    # -- bookkeeping ---------------------------------------------------------

    def _finish(
        self,
        status: str,
        reason: str,
        evidence: SelfRepairEvidence,
        report: RunReport,
        report_dir: Path,
        work: Path,
        *,
        halted_report: Optional[RunReport] = None,
        candidate: Optional[RepairCandidate] = None,
        patched_bundle: Optional[Path] = None,
        approval_request_id: Optional[str] = None,
    ) -> SelfRepairResult:
        evidence.status = status  # type: ignore[assignment]
        evidence.reason = reason[:1024]
        report.self_repair = evidence
        report.save(report_dir)
        if halted_report is not None and halted_report is not report:
            # The halted run's own report says what happened to it, too.
            halted_report.self_repair = evidence
            halted_report.save(work.parent)
        try:
            from openadapt_flow.report import render_run_report

            render_run_report(report_dir)
        except Exception:  # noqa: BLE001 - REPORT.md is presentation only
            pass
        if status != "not_needed":
            work.mkdir(parents=True, exist_ok=True)
            (work / "self-repair.json").write_text(evidence.model_dump_json(indent=2))
        return SelfRepairResult(
            status=status,
            report=report,
            run_dir=report_dir,
            evidence=evidence,
            halted_report=halted_report,
            candidate=candidate,
            patched_bundle=patched_bundle,
            approval_request_id=approval_request_id,
        )


# -- helpers ---------------------------------------------------------------------


def _frame_campaign(candidate: RepairCandidate, kind: str) -> CampaignResult:
    """The existing frame campaign; an error is a failed campaign."""

    from openadapt_flow.repair.cli import _run_campaign
    from openadapt_flow.repair.lifecycle import RepairLifecycleError

    try:
        return _run_campaign(candidate, kind)
    except RepairLifecycleError as exc:
        return CampaignResult(
            campaign=kind,  # type: ignore[arg-type]
            passed=False,
            total=1,
            failed=1,
            cases=[
                CampaignCaseResult(
                    label=f"{kind}:setup",
                    kind="setup",
                    passed=False,
                    detail=str(exc)[:512],
                )
            ],
        )


def _digest_of(workflow: Workflow) -> Optional[str]:
    return workflow.manifest.content_digest if workflow.manifest else None


def _close(session: Optional[AppSession]) -> None:
    if session is None:
        return
    try:
        session.close()
    except Exception:  # noqa: BLE001 - closing a session never hides a result
        pass


def _png_size(png: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        return image.size


def _centered_region(
    center: tuple[int, int], size: tuple[int, int], frame: tuple[int, int]
) -> tuple[int, int, int, int]:
    fw, fh = frame
    w, h = min(size[0], fw), min(size[1], fh)
    x = min(max(0, center[0] - w // 2), max(0, fw - w))
    y = min(max(0, center[1] - h // 2), max(0, fh - h))
    return (x, y, w, h)


def _outline(png: bytes, region: tuple[int, int, int, int]) -> bytes:
    """The screen the person is shown, with the repaired target outlined."""

    from PIL import Image, ImageDraw

    with Image.open(io.BytesIO(png)) as image:
        canvas = image.convert("RGB")
    x, y, w, h = region
    draw = ImageDraw.Draw(canvas)
    for inset in range(3):
        draw.rectangle(
            (x - inset, y - inset, x + w + inset, y + h + inset), outline=(220, 30, 30)
        )
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


def _patched_structural(step: Step, structural: Any) -> Any:
    """The structural locator a re-targeted step's anchor keeps.

    A step with a ``submit_binding`` was recorded on the visual path: the
    binding stands in for a record identity there, and a step may not carry
    both it and a usable locator. Self-repair keeps that contract as it was
    recorded, so the step stays visual (a new template crop) and keeps its
    binding, which the save checks again at run time. Any other step takes
    the locator found under the new point.
    """

    if step.submit_binding is not None:
        return step.anchor.structural if step.anchor is not None else None
    return structural


def _recorded_crop(workflow: Workflow, bundle_dir: Path, step: Step) -> Optional[bytes]:
    if step.anchor is None:
        return None
    crop = workflow.decrypted_template(step.anchor.template)
    if crop is not None:
        return crop
    path = bundle_dir / step.anchor.template
    return path.read_bytes() if path.is_file() else None


def _repairable(report: RunReport, workflow: Workflow) -> "tuple[Step, str, Any] | str":
    """``(step, mode, result)`` when self-repair may handle this halt, else why not.

    ``replace``: the step never acted because the ladder could not find its
    target (or found it only by geometry and refused). ``after``: a step that
    does not save acted on the target the ladder found, but its expected
    screen never came, as when a new dialog appears.
    """

    from openadapt_flow.run_gate import is_consequential

    steps = {step.id: (index, step) for index, step in enumerate(workflow.steps)}
    for result in report.results:
        found = steps.get(result.step_id)
        if found is None:
            continue
        _, step = found
        if result.delivery_attempted and is_consequential(step, workflow):
            return f"input reached {step.id}, a step that saves; never repaired"
        for item in result.effect_evidence:
            if item.observed_effect not in (None, "absent"):
                return f"{step.id} has an observed effect; never repaired"
    halted = next(
        (r for r in reversed(report.results) if not r.ok and not r.skipped), None
    )
    if halted is None or halted.step_id not in steps:
        return "the halt is not at a step of this bundle"
    _, step = steps[halted.step_id]
    if halted.identity is not None and halted.identity.status != "verified":
        return (
            f"{step.id} stopped on the record check ({halted.identity.status}); a "
            "wrong-record halt is never repaired"
        )
    if step.action not in _POINTER_KINDS or step.anchor is None:
        return f"{step.id} is not a click on an on-screen target"
    # Only the ladder's own refusals count as "the screen changed": it found
    # nothing, or it found the target only by geometry (or a model) and
    # refused to act on that, or the structural safety check refused that
    # point because no control is there. Any other stop before resolution,
    # such as a person declining the save at the approval gate, is never
    # repaired.
    error = halted.error or ""
    rung = halted.resolution.rung if halted.resolution is not None else None
    if halted.delivery_attempted is False:
        mode = "replace"
        if rung is None and not error.startswith(_NOT_FOUND):
            return (
                f"{step.id} stopped before resolving its target for another "
                "reason; only a target the ladder could not find is repaired"
            )
        if rung is not None and (
            rung not in _UNTRUSTED_RUNGS
            or not (
                _BELOW_RUNG in error
                or (
                    error.startswith(_NOT_A_CONTROL)
                    and error.endswith(_NO_CONTROL_THERE)
                )
            )
        ):
            return f"{step.id} found its target ({rung}) and stopped for another reason"
    elif (
        halted.delivery_attempted is True
        and rung in TRUSTED_RUNGS
        and error.startswith(_EXPECTED_SCREEN)
        and not is_consequential(step, workflow)
    ):
        # A step that does not save clicked the target the ladder found, and
        # the screen it expected never came: a new step appeared after it.
        mode = "after"
    else:
        return f"input may have reached {step.id}; only a step that never acted is repaired"
    approval = report.approval
    if approval is not None and approval.decision is ItemDecisionKind.DECLINED:
        return "a person declined this item; it is never repaired"
    saves = [s.id for s in workflow.steps if is_consequential(s, workflow)]
    if len(saves) != 1:
        return (
            f"the workflow has {len(saves)} saves; self-repair handles a workflow "
            "with exactly one save for now"
        )
    if not report.halt or report.halt.state_id != step.id:
        return "the report's halt does not name the failed step"
    return step, mode, halted


def _before_frame(run_dir: Path, result: Any) -> Optional[bytes]:
    """The frame a halted step acted on, from its run directory."""

    rel = getattr(result, "before_png", None)
    if not rel:
        return None
    path = run_dir / rel
    try:
        return path.read_bytes()
    except OSError:
        return None


def _recorded_identity(workflow: Workflow, backend: Any, point: tuple[int, int]) -> Any:
    """The new click's identity template, when it names a recorded record.

    Reads the DOM identity under ``point`` and hashes it with the bundle's
    own salt. Refuses when the backend can't read it, or when no recorded
    step verified that same identity.
    """

    from openadapt_flow.runtime.identity_template import build_identity_template

    recorded = [
        s.anchor.identity_template
        for s in workflow.steps
        if s.anchor is not None
        and s.anchor.identity_template is not None
        and s.anchor.identity_template.structured
    ]
    getter = getattr(backend, "structured_text_at", None)
    text = None
    if callable(getter):
        try:
            text = getter(int(point[0]), int(point[1]))
        except Exception:  # noqa: BLE001 - unreadable identity is refused
            text = None
    if not recorded or not text:
        raise SelfRepairRefused(
            "a new bridge step needs a record identity the page shows under it; "
            "none could be read"
        )
    template = build_identity_template(
        None,
        structured_identity=text,
        param_examples=dict(workflow.params),
        salt_hex=recorded[0].salt,
    )
    if template is None or template.structured not in {t.structured for t in recorded}:
        raise SelfRepairRefused(
            "the control is not inside a record the recording verified; a bridge "
            "may only act on the recorded record"
        )
    return template


def _expected_crop(workflow: Workflow, bundle_dir: Path, step: Step) -> Optional[bytes]:
    """The recorded crop of the step's expected screen, when there is one."""

    for expect in step.expect:
        if expect.template:
            crop = workflow.decrypted_template(expect.template)
            if crop is not None:
                return crop
            path = bundle_dir / expect.template
            if path.is_file():
                return path.read_bytes()
    return None


def _typable_params(workflow: Workflow, params: Mapping[str, Any]) -> dict[str, str]:
    """The item's parameter values a bridge may type, secrets left out."""

    secret = set(getattr(workflow, "secret_params", None) or [])
    secret |= {s.param for s in workflow.steps if s.secret and s.param}
    merged = {**workflow.params, **dict(params)}
    return {
        str(name): str(value)
        for name, value in merged.items()
        if name not in secret and value is not None and str(value)
    }


def _shows(backend: Any, vision: Any, screen: bytes, text: str) -> bool:
    """Whether ``text`` is on the screen now: in the page, or read by OCR."""

    wanted = " ".join(text.split()).casefold()
    if not wanted:
        return False
    page = getattr(backend, "page", None)
    if page is not None:
        try:
            body = str(page.locator("body").inner_text())
        except Exception:  # noqa: BLE001 - fall back to OCR
            body = ""
        if wanted in " ".join(body.split()).casefold():
            return True
    words = _screen_words(vision, screen)
    return words is not None and set(_words(text)) <= words


def _typed_values(workflow: Workflow, params: Mapping[str, Any]) -> dict[str, str]:
    """``step_id -> value`` for every typing step, secrets left out."""

    values: dict[str, str] = {}
    secrets_ = set(getattr(workflow, "secret_params", None) or [])
    for step in workflow.steps:
        if step.action != ActionKind.TYPE or step.secret:
            continue
        if step.param:
            if step.param in secrets_:
                continue
            value = params.get(step.param, workflow.params.get(step.param))
        else:
            value = step.text
        if value is not None:
            values[step.id] = str(value)
    return values


def _form_state(
    backend: Any, vision: Any, frame: Optional[bytes]
) -> Optional[tuple[str, str]]:
    from openadapt_flow.approval_mode.form_state import capture_form_state

    try:
        return capture_form_state(backend, vision, frame)
    except Exception:  # noqa: BLE001 - unreadable is never "unchanged"
        return None


def _form_values(backend: Any) -> Optional[list[str]]:
    """The non-empty values of the page's own text fields, or ``None``."""

    from openadapt_flow.approval_mode.form_state import _DOM_FORM_STATE_JS

    page = getattr(backend, "page", None)
    if page is None:
        return None
    values: list[str] = []
    for frame in list(getattr(page, "frames", None) or []):
        try:
            rows = frame.evaluate(_DOM_FORM_STATE_JS)
        except Exception:  # noqa: BLE001
            return None
        for row in rows or []:
            if len(row) >= 3 and row[1] in ("value", "editable") and row[2]:
                values.append(str(row[2]))
    return values


def _pre_save_checks(
    ctx: SaveContext,
    workflow: Workflow,
    params: Mapping[str, Any],
    *,
    checked_step_ids: Sequence[str],
    halted_step_id: str,
    halted_form: Optional[tuple[str, str]],
    backend: Any,
    vision: Any,
) -> ProbeOutcome:
    """The checks a probe makes at the last point before the first save."""

    from openadapt_flow.run_gate import is_consequential

    outcome = ProbeOutcome(reached_save=True, step_id=ctx.step.id)
    checks = outcome.checks
    results = [*ctx.prior_results, ctx.result]
    by_id = {step.id: step for step in workflow.steps}

    # Nothing has been written: no earlier step that saves received input.
    early = [
        r.step_id
        for r in ctx.prior_results
        if r.delivery_attempted
        and r.step_id in by_id
        and is_consequential(by_id[r.step_id], workflow)
    ]
    checks.append(
        ("nothing_written", not early, "ok" if not early else ",".join(early))
    )
    # The deterministic run made no model call on the way to the save.
    checks.append(
        (
            "zero_model_calls",
            ctx.model_calls == 0,
            f"{ctx.model_calls} model call(s) before the save",
        )
    )

    # Every new or re-targeted step ran, and the deterministic ladder (not
    # the agent) found its target.
    found: list[str] = []
    missing: list[str] = []
    for step_id in checked_step_ids:
        result = next((r for r in results if r.step_id == step_id), None)
        rung = result.resolution.rung if result and result.resolution else None
        if (
            result is not None
            and rung in TRUSTED_RUNGS
            and (result is ctx.result or result.ok)
        ):
            found.append(f"{step_id}:{rung}")
        else:
            missing.append(f"{step_id}:{rung or 'not resolved'}")
    checks.append(
        (
            "target_found",
            not missing,
            ", ".join(found) if not missing else "not found: " + ", ".join(missing),
        )
    )
    if ctx.result.resolution is not None and ctx.step.anchor is not None:
        point = ctx.result.resolution.point
        w, h = ctx.step.anchor.region[2], ctx.step.anchor.region[3]
        outcome.resolved_region = (point[0] - w // 2, point[1] - h // 2, w, h)

    # Every record check verified, including the save's own when it has one.
    bad = [
        f"{r.step_id}:{r.identity.status}"
        for r in results
        if r.identity is not None and r.identity.status != "verified"
    ]
    anchor = ctx.step.anchor
    armed = anchor is not None and any(
        getattr(anchor, tier, None)
        for tier in (
            "context_text",
            "structured_identity",
            "identity_template",
            "identifier_crop",
        )
    )
    if armed and ctx.result.identity is None:
        bad.append(f"{ctx.step.id}:not_checked")
    for r in results:
        if r.identity is not None:
            outcome.identity_modes[r.step_id] = str(r.identity.mode)
    checks.append(("same_record", not bad, "verified" if not bad else ",".join(bad)))

    # The typed values are in the form.
    expected = [
        value
        for step_id, value in _typed_values(workflow, params).items()
        if any(r.step_id == step_id and r.ok for r in ctx.prior_results)
    ]
    frame = ctx.frame()
    outcome.frame_png = frame
    if expected:
        values = _form_values(backend)
        if values is None:
            words = _screen_words(vision, frame)
            missing = [
                v for v in expected if words is None or not set(_words(v)) <= words
            ]
        else:
            pool = list(values)
            missing = []
            for value in expected:
                if value in pool:
                    pool.remove(value)
                else:
                    missing.append(value)
        checks.append(
            (
                "same_fields",
                not missing,
                f"{len(expected) - len(missing)}/{len(expected)} typed value(s) "
                "in the form",
            )
        )

    # The form is the one the run halted on (when the halt was at this save).
    outcome.form_state = ctx.form_state(frame)
    if outcome.form_state is None:
        checks.append(("form_readable", False, "form_state_unreadable"))
    elif ctx.step.id == halted_step_id:
        same = halted_form is not None and outcome.form_state == halted_form
        checks.append(
            ("same_form", same, "unchanged since the halt" if same else "changed")
        )
    return outcome


def _words(text: str) -> list[str]:
    import re

    return [w.casefold() for w in re.findall(r"[0-9A-Za-zÀ-ɏ]+", text)]


def _screen_words(vision: Any, frame: Optional[bytes]) -> Optional[set[str]]:
    if not frame:
        return None
    try:
        lines = vision.ocr(frame)
    except Exception:  # noqa: BLE001
        return None
    return {w for line in lines or [] for w in _words(str(getattr(line, "text", "")))}
