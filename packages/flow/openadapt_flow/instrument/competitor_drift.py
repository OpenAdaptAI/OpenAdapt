"""Competitor-drift instrument: a pluggable external-agent silent-wrong-action
runner, cost-capped.

This EXTENDS the self-directed silent-wrong-action benchmark
(``openadapt_flow.benchmark.silent_wrong_action``, #67) from "our own runtime"
to "any external computer-use agent." The measurement is identical — did a
wrong / absent / duplicate business effect land in the system of record while
the actor reported success (a *silent wrong-action*)? — but the actor is now an
arbitrary external agent hidden behind the :class:`ExternalAgentAdapter` seam
instead of our replay runtime.

Architecture, at a glance::

    external agent  --ExternalAgentAdapter-->  MockMed fault_server (SoR)
                                                        |
                                        RestRecordVerifier (#63, our engine)
                                                        |
                                        silent-wrong-action rate, by ARCH CLASS

What this module deliberately does NOT do:

- It ships **no concrete adapter for any real external product**. The Protocol
  is the seam; a real adapter (which wraps a vendor's own record/replay or
  agent entry points) is added later, out of this PR, and gated on an explicit
  user decision because it costs money.
- It makes **no paid API / model calls** and **names no vendor**. The only
  bundled adapter is a deterministic, offline, $0 :class:`StubExternalAgentAdapter`
  that proves the harness measures the rate correctly end to end.
- Its output is anonymized by **architecture class** (``Tool A`` / ``Tool B`` /
  ...) — enforced structurally by :func:`ensure_architecture_class`, never a
  product or vendor name (see ``docs/validation/SILENT_WRONG_ACTION_RATE.md``).

Cost is the other first-class concern: :class:`CostGuard` is a hard
kill-switch on ``max_cost_usd`` / ``max_steps`` / ``max_runs`` that aborts the
WHOLE run the moment any limit would be crossed, plus a :func:`run_instrument`
``dry_run`` mode that reports the projected cost BEFORE a cent is spent. No run
can silently exceed the cap.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

# Single source of truth for the fault scenarios, the independent ground-truth
# judge, and the #63 effect contract. We REUSE these from the self-directed
# benchmark rather than re-deriving them so the external-agent instrument and
# the self-directed instrument grade with byte-identical semantics.
from openadapt_flow.benchmark.silent_wrong_action import (
    NOTE,
    SCENARIOS,
    Scenario,
    _business_effect,
    _drive,
    _effect_verify,
    _screen_shows_success,
)
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import RestRecordVerifier

DEFAULT_N = 1
DEFAULT_OUT_DIR = "instrument/competitor_drift"

#: Anonymized architecture-class label shape. The whole anonymization guarantee
#: rides on this being STRUCTURAL, not a best-effort denylist: a class label is
#: only ever ``Tool <SINGLE UPPERCASE LETTER>`` (optionally with a short,
#: vendor-free parenthetical descriptor), so a vendor name cannot occupy the
#: field the report emits.
_ARCH_CLASS_RE = re.compile(r"^Tool [A-Z](?: \([\w ,/+-]+\))?$")

#: Belt-and-suspenders scan applied to every rendered artifact. The structural
#: label check above is the real guarantee; this catches an accidental vendor
#: string smuggled in via a free-text ``blurb`` / ``note`` / adapter id. It is
#: intentionally conservative and case-insensitive. Extend as needed — but the
#: report must NEVER depend on this list being exhaustive.
_VENDOR_DENYLIST: tuple[str, ...] = (
    "skyvern",
    "workflow-use",
    "workflowuse",
    "workflow use",
    "browser-use",
    "browseruse",
    "anthropic",
    "claude",
    "openai",
    "gpt-4",
    "gpt4",
    "gemini",
    "adept",
    "multion",
)


class ArchitectureClassError(ValueError):
    """Raised when a label is not an anonymized architecture class.

    The instrument refuses to emit anything but ``Tool <LETTER>`` so a vendor
    name can never reach a published artifact through the class field.
    """


def ensure_architecture_class(label: str) -> str:
    """Return ``label`` iff it is an anonymized architecture class, else raise.

    Accepts ``"Tool A"``, ``"Tool B (cached-script replay)"``, etc. Rejects
    anything that could carry a product or vendor identity. This is the
    structural anonymization gate every adapter's ``architecture_class`` passes
    through before it can reach the report.
    """
    if not isinstance(label, str) or not _ARCH_CLASS_RE.match(label):
        raise ArchitectureClassError(
            f"architecture_class {label!r} is not anonymized; it must match "
            f"'Tool <LETTER>' (optionally with a vendor-free parenthetical), "
            "so no product/vendor name can reach the report"
        )
    _assert_no_vendor(label, where="architecture_class")
    return label


def _assert_no_vendor(text: str, *, where: str) -> None:
    lowered = text.lower()
    for needle in _VENDOR_DENYLIST:
        if needle in lowered:
            raise ArchitectureClassError(
                f"vendor-like string {needle!r} found in {where}; the "
                "competitor-drift instrument is anonymized by architecture "
                "class and must never emit a vendor name"
            )


def assert_anonymized(text: str, *, where: str = "report") -> str:
    """Assert a fully-rendered artifact contains no denylisted vendor string.

    Called on the final JSON / markdown before it is written. Complements (does
    not replace) the structural :func:`ensure_architecture_class` gate.
    """
    _assert_no_vendor(text, where=where)
    return text


# --------------------------------------------------------------------------
# The pluggable seam: an external agent, and what one run of it produced.
# --------------------------------------------------------------------------


@dataclass
class AgentRunResult:
    """What an external agent produced on ONE task — the harness's only input.

    An adapter reports exactly this and nothing about HOW it ran: the harness
    reads the system of record itself (never the agent's word) to decide
    whether the effect was right. The only agent-supplied signal the metric
    uses is :attr:`reported_success` — the agent's own claim — which is what a
    silent wrong-action is measured against.

    Attributes:
        reported_success: The agent's OWN claim that it completed the task.
            The silent-wrong-action rate is exactly ``reported_success`` AND
            "the effect was not confirmed by our verifier."
        steps_used: How many steps / actions the agent consumed (checked
            against :attr:`CostGuard.max_steps`).
        cost_usd: Actual spend this run incurred (LLM tokens, API calls). $0
            for deterministic / no-model adapters. Fed to the cost kill-switch.
        actions: Optional opaque record of what the agent did, for audit only —
            the harness never scores against it. Kept vendor-free.
        error: Optional failure detail when the agent could not complete.
    """

    reported_success: bool
    steps_used: int = 0
    cost_usd: float = 0.0
    actions: list[Any] = field(default_factory=list)
    error: Optional[str] = None


@dataclass(frozen=True)
class DriftTask:
    """One MockMed task the external agent must perform, plus the server-side
    fault it is run under.

    A real browser-driving agent uses only :attr:`instruction` and the
    ``target_url`` the runner hands it — the fault is injected server-side and
    baked into that URL, exactly as UI/data drift would arrive under a constant
    address. The mechanical :attr:`scenario` fields (delivery, keyed,
    seed_concurrent) describe the fault backend's behavior and are what the
    offline STUB uses to emulate the app's write; a real adapter ignores them.

    Attributes:
        task_id: Stable id for the run row.
        scenario: The transactional-fault scenario (``mockmed.fault_server``
            mode + how the app delivers the write).
        instruction: The natural-language task a real agent would receive.
        expected_note: The note the intended write must carry (a partial save
            drops it; ground truth uses it to catch a wrong-field write).
    """

    task_id: str
    scenario: Scenario
    instruction: str
    expected_note: str = NOTE


@runtime_checkable
class ExternalAgentAdapter(Protocol):
    """The seam that lets ANY external computer-use agent be measured without
    its code living in this repo.

    A concrete adapter is a thin wrapper over one external product's own record
    / replay / agent entry points. It is NOT included in this repo (this PR
    ships only the Protocol and an offline stub); a real adapter is added later
    and its run is gated on an explicit, cost-capped user decision.

    A conforming adapter must expose:

    - :attr:`architecture_class`: an anonymized ``Tool <LETTER>`` label (passed
      through :func:`ensure_architecture_class`). NEVER a vendor name.
    - :meth:`estimate_cost_usd`: a *pre-flight* projected cost for one task, so
      :func:`run_instrument`'s dry-run and the cost kill-switch can decide
      whether a run is affordable BEFORE it happens.
    - :meth:`run_task`: drive the external agent through one task against
      ``target_url`` under a hard ``max_steps`` budget, and return an
      :class:`AgentRunResult`.

    Example — how a real (vendor-specific) adapter would plug in later, kept
    entirely outside this repo::

        class _SomeVendorAdapter:                      # not shipped here
            architecture_class = "Tool B (cached-script replay)"

            def __init__(self, client, *, cost_model):
                self._client = client                  # the vendor's SDK
                self._cost_model = cost_model

            def estimate_cost_usd(self, task):
                # project spend from the vendor's own token/step pricing
                return self._cost_model.project(task.instruction)

            def run_task(self, task, target_url, *, max_steps):
                # drive the vendor's replay/agent against the MockMed app URL;
                # the fault is already baked into target_url server-side.
                run = self._client.replay(
                    goal=task.instruction, url=target_url, max_steps=max_steps
                )
                return AgentRunResult(
                    reported_success=run.claims_success,   # the agent's CLAIM
                    steps_used=run.step_count,
                    cost_usd=run.usage.total_usd,
                    actions=run.redacted_action_log,       # vendor-free
                )

        # gated, cost-capped, run out of this PR:
        report = run_instrument(
            _SomeVendorAdapter(client, cost_model=pricing),
            guard=CostGuard(max_cost_usd=10.0, max_steps=40, max_runs=63),
        )

    The harness then reads the MockMed system of record with our
    :class:`RestRecordVerifier` and scores the vendor's run — the vendor's code
    never touches our scoring path.
    """

    #: Anonymized architecture-class label (``"Tool A"`` / ``"Tool B"`` / ...).
    architecture_class: str

    def estimate_cost_usd(self, task: DriftTask) -> float:
        """Projected USD cost of running ``task`` once (0.0 if free)."""
        ...

    def run_task(
        self, task: DriftTask, target_url: str, *, max_steps: int
    ) -> AgentRunResult:
        """Drive the external agent through ``task`` against ``target_url``
        within ``max_steps`` and report what it produced."""
        ...


# --------------------------------------------------------------------------
# Cost guardrails: a hard kill-switch on cost / steps / runs.
# --------------------------------------------------------------------------


@dataclass
class CostGuard:
    """Hard kill-switch over an instrument run's spend, steps, and count.

    Enforced two ways so no run can silently exceed a cap:

    1. **Pre-flight** (:meth:`can_start`): before every run the runner asks
       whether starting it could cross ``max_cost_usd`` (given the adapter's
       *estimate*) or ``max_runs``. If so the whole run aborts BEFORE spending.
    2. **Post-flight** (:meth:`record`): after every run the ACTUAL cost and
       step count are booked; if either the cumulative spend passed
       ``max_cost_usd`` or the run exceeded ``max_steps``, the whole run aborts
       (no further runs are started).

    All spend and every dropped run are logged, so an aborted run reports
    exactly what it spent and what it skipped.

    Attributes:
        max_cost_usd: Hard cumulative spend cap across the whole run.
        max_steps: Hard per-run step cap; a run over it aborts the whole run.
        max_runs: Hard cap on the number of runs started.
    """

    max_cost_usd: float
    max_steps: int
    max_runs: int
    spent_usd: float = 0.0
    runs_started: int = 0
    runs_completed: int = 0
    steps_used: int = 0
    log: Callable[[str], None] = print

    def can_start(self, estimated_cost_usd: float) -> tuple[bool, str]:
        """Whether one more run (projected to cost ``estimated_cost_usd``) may
        start without crossing a cap. Returns ``(ok, reason_if_not)``."""
        if self.runs_started >= self.max_runs:
            return False, (f"max_runs reached ({self.runs_started}/{self.max_runs})")
        projected = self.spent_usd + max(0.0, estimated_cost_usd)
        if projected > self.max_cost_usd:
            return False, (
                f"projected spend ${projected:.4f} would exceed max_cost_usd "
                f"${self.max_cost_usd:.4f}"
            )
        return True, ""

    def start_run(self) -> None:
        self.runs_started += 1

    def record(self, cost_usd: float, steps_used: int) -> tuple[bool, str]:
        """Book one completed run's ACTUAL cost/steps.

        Returns ``(must_abort, reason)`` — ``must_abort`` is True when the
        cumulative spend has passed ``max_cost_usd`` or this run exceeded
        ``max_steps``, i.e. the whole run must now stop.
        """
        self.spent_usd += max(0.0, cost_usd)
        self.steps_used += max(0, steps_used)
        self.runs_completed += 1
        if self.spent_usd > self.max_cost_usd:
            return True, (
                f"cumulative spend ${self.spent_usd:.4f} exceeded max_cost_usd "
                f"${self.max_cost_usd:.4f}"
            )
        if steps_used > self.max_steps:
            return True, (
                f"run used {steps_used} steps, exceeding max_steps {self.max_steps}"
            )
        return False, ""


# --------------------------------------------------------------------------
# The deterministic, $0, offline stub adapter (CI proof).
# --------------------------------------------------------------------------


class StubExternalAgentAdapter:
    """A deterministic, offline, $0 stand-in for an external agent — the CI
    proof that the harness measures the silent-wrong-action rate correctly.

    It emulates an external agent driving the MockMed app by issuing exactly
    the write(s) the app issues under each fault scenario (reusing the app's
    documented write behavior), then reports success according to one of two
    self-report policies:

    - ``mode="screen_blind"`` (default): reports success whenever the app would
      paint its "saved" banner — the weak, vision-style self-report a
      screen-only agent has. On the five silent fault classes (partial,
      optimistic, duplicate, double, stale) this produces a KNOWN-WRONG effect
      that the agent still claims succeeded → a silent wrong-action the harness
      must catch. On the clean control and the idempotent fix it produces a
      KNOWN-RIGHT effect → no silent wrong-action.
    - ``mode="honest"``: reports success only when the effect actually landed
      correctly (it reads the system of record). Produces ZERO silent
      wrong-actions everywhere — the negative control proving the metric is not
      hardwired to fire.

    No network beyond localhost, no model, $0. ``cost_per_run`` and
    ``fixed_steps`` are injectable ONLY so tests can exercise the cost / step
    kill-switch; both default to 0.
    """

    def __init__(
        self,
        *,
        architecture_class: str = "Tool A (stub, deterministic)",
        mode: str = "screen_blind",
        cost_per_run: float = 0.0,
        fixed_steps: Optional[int] = None,
    ) -> None:
        if mode not in ("screen_blind", "honest"):
            raise ValueError(f"unknown stub mode {mode!r}")
        self.architecture_class = ensure_architecture_class(architecture_class)
        self.mode = mode
        self.cost_per_run = cost_per_run
        self.fixed_steps = fixed_steps

    def estimate_cost_usd(self, task: DriftTask) -> float:  # noqa: ARG002
        return self.cost_per_run

    @staticmethod
    def _base_of(target_url: str) -> str:
        """The scheme://host:port of ``target_url`` (drops path/query/fragment)
        so the stub can talk to the same fault backend the app talks to."""
        parts = urlsplit(target_url)
        return urlunsplit((parts.scheme, parts.netloc, "", "", ""))

    def run_task(
        self, task: DriftTask, target_url: str, *, max_steps: int
    ) -> AgentRunResult:
        base = self._base_of(target_url)
        verifier = RestRecordVerifier(base)
        # Observe the system of record BEFORE acting (an agent may inspect
        # state around its own action) so the honest policy can detect
        # collateral loss, not just its own row.
        before = verifier.capture_pre_state().records

        # Emulate the app's write(s) under this fault scenario. This IS the
        # external agent "operating the app": the fault backend records the
        # real (possibly wrong) effect, which the harness reads back
        # independently.
        statuses = _drive(base, task.scenario)
        steps = self.fixed_steps if self.fixed_steps is not None else len(statuses)

        if self.mode == "screen_blind":
            reported = _screen_shows_success(task.scenario.name, statuses)
        else:  # honest negative control: claim success only if truly correct
            after = verifier.capture_pre_state().records
            correct, _ = _business_effect(
                before, after, expected_note=task.expected_note
            )
            reported = correct

        return AgentRunResult(
            reported_success=reported,
            steps_used=steps,
            cost_usd=self.cost_per_run,
            actions=[{"statuses": statuses}],
        )


# --------------------------------------------------------------------------
# Run rows, the report, and the runner.
# --------------------------------------------------------------------------


@dataclass
class InstrumentRunRow:
    """One (task × iteration) result: the agent's claim vs. our verifier."""

    task_id: str
    scenario: str
    blurb: str
    reported_success: bool
    steps_used: int
    cost_usd: float
    #: Independent ground-truth off the SoR store (never an oracle / the agent).
    ground_truth_correct: bool
    ground_truth_fault: str
    #: Our #63 EffectVerifier verdict against the system of record.
    effect_verdict: str
    effect_confirmed: bool
    effect_reason: str
    #: The metric: the agent claimed success AND our verifier did not confirm
    #: the write — a wrong effect the agent reported as a success.
    silent_wrong_action: bool
    #: The safe alternative our verifier would have forced: the agent claimed
    #: success but our verifier halts (converts the silent wrong-action).
    verifier_would_halt: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InstrumentReport:
    """The anonymized instrument result for one architecture class."""

    architecture_class: str
    generated_at: str
    dry_run: bool
    aborted: bool
    abort_reason: str
    guard: dict[str, Any]
    projected_cost_usd: float
    would_exceed_cap: bool
    n_runs_planned: int
    n_runs_completed: int
    n_runs_dropped: int
    metrics: dict[str, Any]
    runs: list[dict[str, Any]]
    platform: str
    target: str = "MockMed transactional-fault suite (mockmed.fault_server)"
    scoring_engine: str = (
        "RestRecordVerifier (#63) against the system of record at GET /api/db"
    )
    instrument: str = (
        "competitor-drift: external-agent silent-wrong-action rate (cost-capped)"
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_tasks() -> list[DriftTask]:
    """The MockMed transactional-fault suite as external-agent tasks.

    One task per fault scenario (same suite the self-directed benchmark uses),
    so an external agent is measured on exactly the fault classes screen
    verification is blind to.
    """
    return [
        DriftTask(
            task_id=f"mockmed-{sc.name}",
            scenario=sc,
            instruction=(
                "Open the first referral task, create a Triage encounter, "
                f"enter the note {NOTE!r}, and save."
            ),
        )
        for sc in SCENARIOS
    ]


def _rate(
    rows: list[InstrumentRunRow], pred: Callable[[InstrumentRunRow], bool]
) -> float:
    return (sum(1 for r in rows if pred(r)) / len(rows)) if rows else 0.0


def _aggregate(rows: list[InstrumentRunRow]) -> dict[str, Any]:
    """Headline silent-wrong-action metrics + per-scenario breakdown."""
    n = len(rows)
    wrong_rows = [r for r in rows if not r.effect_confirmed]
    silent = sum(1 for r in rows if r.silent_wrong_action)

    per_scenario: dict[str, Any] = {}
    for name in {r.scenario for r in rows}:
        srows = [r for r in rows if r.scenario == name]
        per_scenario[name] = {
            "n": len(srows),
            "blurb": srows[0].blurb,
            "ground_truth_fault": _mode_value(srows, lambda r: r.ground_truth_fault),
            "effect_verdict": _mode_value(srows, lambda r: r.effect_verdict),
            "reported_success_rate": _rate(srows, lambda r: r.reported_success),
            "silent_wrong_action_rate": _rate(srows, lambda r: r.silent_wrong_action),
        }

    return {
        "n_runs": n,
        "n_wrong_effect": len(wrong_rows),
        "silent_wrong_action_count": silent,
        # Headline: over ALL completed runs.
        "silent_wrong_action_rate": (silent / n) if n else 0.0,
        # Conditioned on runs where a wrong effect actually occurred — the
        # cleanest read of "when the agent got it wrong, how often did it still
        # claim success?"
        "undetected_wrong_rate": ((silent / len(wrong_rows)) if wrong_rows else 0.0),
        # What pairing this agent with our EffectVerifier buys: the fraction of
        # the agent's own success-claims our verifier would instead HALT on.
        "verifier_halt_rate_over_claims": (
            _rate(
                [r for r in rows if r.reported_success],
                lambda r: r.verifier_would_halt,
            )
        ),
        "per_scenario": per_scenario,
    }


def _mode_value(
    rows: list[InstrumentRunRow], key: Callable[[InstrumentRunRow], Any]
) -> Any:
    values = {key(r) for r in rows}
    if len(values) == 1:
        return next(iter(values))
    return "MIXED:" + ",".join(str(v) for v in sorted(values, key=str))


def run_instrument(
    adapter: ExternalAgentAdapter,
    *,
    guard: CostGuard,
    tasks: Optional[list[DriftTask]] = None,
    n_per_scenario: int = DEFAULT_N,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> InstrumentReport:
    """Drive ``adapter`` through the MockMed fault suite and score it.

    For each planned run the runner: resets the fault backend, captures the
    pre-state, hands the adapter the MockMed URL (with the fault baked in),
    then reads the system of record back with :class:`RestRecordVerifier` and
    records whether the agent's success-claim rode on a wrong effect (a silent
    wrong-action). Output is anonymized to ``adapter.architecture_class``.

    Cost is bounded by ``guard`` on both edges (see :class:`CostGuard`); the
    whole run aborts the instant a cap would be crossed. ``dry_run=True``
    projects the cost from the adapter's estimates and returns WITHOUT running
    or spending anything.

    Args:
        adapter: The external-agent adapter under test.
        guard: The hard cost / step / run kill-switch.
        tasks: Tasks to run (defaults to the full fault suite).
        n_per_scenario: Iterations per task.
        dry_run: If True, only project cost; never serve, run, or spend.
        log: Progress / audit logger.

    Returns:
        An :class:`InstrumentReport` (anonymized), also writable via
        :func:`write_outputs`.
    """
    arch = ensure_architecture_class(adapter.architecture_class)
    tasks = tasks if tasks is not None else default_tasks()
    guard.log = log
    plan: list[DriftTask] = [t for t in tasks for _ in range(n_per_scenario)]
    now = datetime.now(timezone.utc).isoformat()

    if dry_run:
        projected = sum(adapter.estimate_cost_usd(t) for t in plan)
        would_exceed = projected > guard.max_cost_usd
        log(
            f"[dry-run] {arch}: {len(plan)} planned runs, projected spend "
            f"${projected:.4f} vs max_cost_usd ${guard.max_cost_usd:.4f} — "
            + ("WOULD EXCEED cap (nothing will run)" if would_exceed else "fits")
        )
        return InstrumentReport(
            architecture_class=arch,
            generated_at=now,
            dry_run=True,
            aborted=False,
            abort_reason="",
            guard=_guard_snapshot(guard),
            projected_cost_usd=projected,
            would_exceed_cap=would_exceed,
            n_runs_planned=len(plan),
            n_runs_completed=0,
            n_runs_dropped=len(plan),
            metrics=_aggregate([]),
            runs=[],
            platform=platform.platform(),
        )

    url, db, stop = fault_serve()
    base = url.rstrip("/")
    rows: list[InstrumentRunRow] = []
    aborted = False
    abort_reason = ""
    try:
        for task in plan:
            estimate = adapter.estimate_cost_usd(task)
            ok, why = guard.can_start(estimate)
            if not ok:
                aborted = True
                abort_reason = why
                log(
                    f"[cost-guard] ABORT before run '{task.task_id}': {why}. "
                    f"spent=${guard.spent_usd:.4f}, "
                    f"completed={guard.runs_completed}"
                )
                break
            guard.start_run()
            row = _run_one(adapter, base, db, task, max_steps=guard.max_steps)
            rows.append(row)
            must_abort, reason = guard.record(row.cost_usd, row.steps_used)
            log(
                f"{task.scenario.name:11s} gt={row.ground_truth_fault:15s} "
                f"claim={'SUCCESS' if row.reported_success else 'failure'} "
                f"effect={row.effect_verdict:13s} "
                f"silent_wrong={row.silent_wrong_action} "
                f"spent=${guard.spent_usd:.4f}"
            )
            if must_abort:
                aborted = True
                abort_reason = reason
                log(f"[cost-guard] ABORT after run: {reason}.")
                break
    finally:
        stop()

    dropped = len(plan) - len(rows)
    metrics = _aggregate(rows)
    if aborted:
        log(
            f"[cost-guard] run aborted: {abort_reason}. "
            f"completed={len(rows)}, dropped={dropped}, "
            f"spent=${guard.spent_usd:.4f}"
        )
    return InstrumentReport(
        architecture_class=arch,
        generated_at=now,
        dry_run=False,
        aborted=aborted,
        abort_reason=abort_reason,
        guard=_guard_snapshot(guard),
        projected_cost_usd=guard.spent_usd,
        would_exceed_cap=aborted,
        n_runs_planned=len(plan),
        n_runs_completed=len(rows),
        n_runs_dropped=dropped,
        metrics=metrics,
        runs=[r.to_dict() for r in rows],
        platform=platform.platform(),
    )


def _guard_snapshot(guard: CostGuard) -> dict[str, Any]:
    return {
        "max_cost_usd": guard.max_cost_usd,
        "max_steps": guard.max_steps,
        "max_runs": guard.max_runs,
        "spent_usd": guard.spent_usd,
        "runs_started": guard.runs_started,
        "runs_completed": guard.runs_completed,
        "steps_used": guard.steps_used,
    }


def _run_one(
    adapter: ExternalAgentAdapter,
    base: str,
    db: Any,
    task: DriftTask,
    *,
    max_steps: int,
) -> InstrumentRunRow:
    """Drive the adapter once and score it against the system of record."""
    db.reset(seed_concurrent=task.scenario.seed_concurrent)
    before_snapshot = db.snapshot()["records"]

    verifier = RestRecordVerifier(base)
    before_state = verifier.capture_pre_state()

    target_url = f"{base}/?fault={task.scenario.name}"
    result = adapter.run_task(task, target_url, max_steps=max_steps)

    after_snapshot = db.snapshot()["records"]
    correct, fault_class = _business_effect(
        before_snapshot, after_snapshot, expected_note=task.expected_note
    )

    effect_verdict = _effect_verify(verifier, before_state, keyed=task.scenario.keyed)
    effect_confirmed = effect_verdict.confirmed

    # The metric: the agent CLAIMED success while our verifier could not
    # confirm the write — a wrong effect reported as a success.
    silent = result.reported_success and not effect_confirmed
    return InstrumentRunRow(
        task_id=task.task_id,
        scenario=task.scenario.name,
        blurb=task.scenario.blurb,
        reported_success=result.reported_success,
        steps_used=result.steps_used,
        cost_usd=result.cost_usd,
        ground_truth_correct=correct,
        ground_truth_fault=fault_class,
        effect_verdict=effect_verdict.verdict.value,
        effect_confirmed=effect_confirmed,
        effect_reason=effect_verdict.reason,
        silent_wrong_action=silent,
        verifier_would_halt=result.reported_success and not effect_confirmed,
    )


# --------------------------------------------------------------------------
# Rendering (anonymized artifacts).
# --------------------------------------------------------------------------


def render_markdown(report: InstrumentReport) -> str:
    """Render the anonymized instrument report to markdown.

    Passes the result through :func:`assert_anonymized` so no vendor string can
    reach the artifact.
    """
    m = report.metrics
    date = report.generated_at[:10]
    g = report.guard
    lines = [
        f"# Competitor-drift instrument — {report.architecture_class}",
        "",
        f"Date: {date}. This points our #63 EffectVerifier at an external "
        "agent's runs against the MockMed transactional-fault suite "
        "(`mockmed.fault_server`) and measures its **silent-wrong-action "
        "rate** — a wrong / absent / duplicate business effect that landed "
        "while the agent reported success. Output is anonymized by "
        "architecture class; no vendor is named. See "
        "`docs/validation/SILENT_WRONG_ACTION_RATE.md`.",
        "",
        f"- **Scoring engine:** {report.scoring_engine}",
        f"- **Target:** {report.target}",
        f"- **Cost guard:** max_cost_usd=${g['max_cost_usd']}, "
        f"max_steps={g['max_steps']}, max_runs={g['max_runs']}; "
        f"spent=${g['spent_usd']:.4f}",
    ]
    if report.dry_run:
        lines += [
            "",
            f"**Dry run.** Projected spend "
            f"${report.projected_cost_usd:.4f} across "
            f"{report.n_runs_planned} planned runs — "
            + (
                "WOULD EXCEED the cap; nothing ran."
                if report.would_exceed_cap
                else "fits under the cap. Nothing ran (estimate only)."
            ),
        ]
        return assert_anonymized("\n".join(lines) + "\n")
    if report.aborted:
        lines += [
            "",
            f"**Aborted by cost guard:** {report.abort_reason}. "
            f"Completed {report.n_runs_completed} of "
            f"{report.n_runs_planned} planned runs "
            f"({report.n_runs_dropped} dropped).",
        ]

    lines += [
        "",
        "## Headline",
        "",
        f"Over **{m['n_runs']} completed runs** "
        f"({m['n_wrong_effect']} produced a wrong / absent / duplicate effect, "
        "judged by our verifier against the system of record):",
        "",
        "| metric | value |",
        "|---|---|",
        f"| **silent-wrong-action rate** (wrong effect ∧ agent claimed "
        f"success, over all runs) | **{m['silent_wrong_action_rate']:.1%}** "
        f"({m['silent_wrong_action_count']}/{m['n_runs']}) |",
        f"| **undetected-wrong rate** (agent claimed success \\| a wrong effect "
        f"occurred) | **{m['undetected_wrong_rate']:.1%}** |",
        f"| **verifier-halt rate over the agent's own success-claims** "
        f"(what our EffectVerifier converts to a safe halt) | "
        f"{m['verifier_halt_rate_over_claims']:.1%} |",
        "",
        "## Per-scenario",
        "",
        "| scenario | ground-truth effect | agent claim rate | our verifier | "
        "silent-wrong rate |",
        "|---|---|---|---|---|",
    ]
    for name in [sc.name for sc in SCENARIOS]:
        ps = m["per_scenario"].get(name)
        if not ps:
            continue
        lines.append(
            f"| `{name}` | {ps['ground_truth_fault']} | "
            f"{ps['reported_success_rate']:.0%} | {ps['effect_verdict']} | "
            f"{ps['silent_wrong_action_rate']:.0%} |"
        )
    lines += [
        "",
        "## What this is (and is not)",
        "",
        "- Anonymized by **architecture class** — no product or vendor name "
        "appears anywhere in this artifact (structurally enforced).",
        "- The bundled run uses a **deterministic, $0, offline stub** adapter; "
        "no paid API or model call was made.",
        "- A real external agent plugs in behind the `ExternalAgentAdapter` "
        "Protocol; running it against a real competitor is a **separate, "
        "cost-capped, user-gated** step.",
        "",
    ]
    return assert_anonymized("\n".join(lines) + "\n")


def write_outputs(report: InstrumentReport, out_dir: Path | str) -> None:
    """Write ``results.json`` and ``COMPETITOR_DRIFT.md`` (both anonymized)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = assert_anonymized(json.dumps(report.to_dict(), indent=2) + "\n")
    (out / "results.json").write_text(payload)
    (out / "COMPETITOR_DRIFT.md").write_text(render_markdown(report))


# --------------------------------------------------------------------------
# CLI. $0 by default: dry-run, or the offline stub. No vendor, no model.
# --------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """CLI: ``python -m openadapt_flow.instrument.competitor_drift``.

    Ships only the offline stub adapter and a dry-run estimator — a real
    external adapter is wired programmatically (see
    :class:`ExternalAgentAdapter`) and run as a separate, cost-capped,
    user-gated step. Spends $0 and names no vendor.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Competitor-drift instrument: measure an external agent's "
            "silent-wrong-action rate on the MockMed transactional-fault "
            "suite, scored by our #63 EffectVerifier, anonymized by "
            "architecture class. This CLI ships only a deterministic $0 stub "
            "and a dry-run estimator; a real (paid) competitor adapter is "
            "wired in code and run as a separate user decision. No vendor is "
            "named; no model is called."
        )
    )
    parser.add_argument("--out", default=DEFAULT_OUT_DIR, help="output directory")
    parser.add_argument(
        "--n", type=int, default=DEFAULT_N, help="iterations per fault scenario"
    )
    parser.add_argument(
        "--stub-mode",
        choices=("screen_blind", "honest"),
        default="screen_blind",
        help="stub self-report policy (screen_blind emulates a blind agent)",
    )
    parser.add_argument(
        "--max-cost-usd", type=float, default=10.0, help="hard spend cap"
    )
    parser.add_argument(
        "--max-steps", type=int, default=40, help="hard per-run step cap"
    )
    parser.add_argument(
        "--max-runs", type=int, default=1000, help="hard cap on runs started"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="project cost only; do not run or spend",
    )
    args = parser.parse_args(argv)

    adapter = StubExternalAgentAdapter(mode=args.stub_mode)
    guard = CostGuard(
        max_cost_usd=args.max_cost_usd,
        max_steps=args.max_steps,
        max_runs=args.max_runs,
    )
    report = run_instrument(
        adapter, guard=guard, n_per_scenario=args.n, dry_run=args.dry_run
    )
    write_outputs(report, args.out)
    m = report.metrics
    print(
        f"\n{report.architecture_class}: silent-wrong-action rate "
        f"{m['silent_wrong_action_rate']:.1%} "
        f"({m['silent_wrong_action_count']}/{m['n_runs']}); "
        f"spent ${report.guard['spent_usd']:.4f}"
        + (f"; ABORTED: {report.abort_reason}" if report.aborted else "")
    )
    print(f"Wrote {Path(args.out) / 'results.json'}, COMPETITOR_DRIFT.md")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
