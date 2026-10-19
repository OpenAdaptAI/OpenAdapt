"""Settle a recording disagreement by replaying it on a declared test copy.

:func:`~openadapt_flow.compiler.induction.induce_program` refuses when one
recording has a consequential step another lacks: from the recordings alone it
cannot tell an incidental extra click from a required conditional branch. It
names the recording that would settle the question (a record-next item).

This module tries the other route, opt-in and only on a resettable test copy.
For each such point it replays each recording twice in a fresh session, once
per interpretation:

* ``incidental``: the step is noise. Every recording runs without it.
* ``conditional``: the step is a required branch. Every recording runs it,
  guarded to skip when its target (or its dialog) is not on screen.
* ``narrated`` (only when narration proposed a guard there and no recording
  contradicts it): every recording runs the step guarded by the narrated
  condition (:mod:`openadapt_flow.compiler.narration_rules`). It is confirmed
  when it produces the recorded effect in every context and ``incidental``
  conclusively fails in at least one.

Each run is scored by reading the system of record back through the reward
contract's oracle and judging it with
:func:`openadapt_flow.reward.worker.judge_episode`, the function
``serve-reward`` scores with. A run "produces the recorded effect" only when
that judge says ``verified`` for the recording's own subject. A point is
certified only when exactly one interpretation produces the recorded effect in
every recording's context and every other interpretation conclusively fails
in at least one. Anything else (both pass, neither passes, a read-back that
cannot decide) leaves the point refused, as without probing.

What this module will not do:

* Probe a target whose deployment file does not declare it a test copy
  (:func:`probe_target_refusals`), or whose runtime profile is a production one.
* Let a :class:`~openadapt_flow.compiler.induction.Proposer` choose. Proposals
  stay advisory in the result; probing never reads them.
* Call a model. Probe replays run with model grounding off, and each run's
  model-call count is written to the report.

Every run is written to :attr:`InductionResult.probe_runs`, decisive or not.
"""

from __future__ import annotations

import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, Union, runtime_checkable

from openadapt_flow.compiler.induction import (
    InductionResult,
    NarratedGuard,
    OptionalPoint,
    ProbeRun,
    ProbeVerdict,
    Proposer,
    TraceInput,
    induce_program,
    optional_guard,
    optional_points,
)
from openadapt_flow.ir import Guard, Step, Workflow

INTERPRETATIONS: tuple[str, ...] = ("incidental", "conditional")
NARRATED = "narrated"

#: Runtime signals the serve-reward judge accepts.
RUNTIME_SIGNALS = frozenset(
    {
        "completed",
        "halted_before_effect",
        "refused",
        "rejected_policy",
        "failed_platform",
    }
)

#: Judge outcomes that do not decide anything about the interpretation.
_INCONCLUSIVE_OUTCOMES = frozenset({"failed_platform", "reconciliation_required"})

#: Runtime profiles a production deployment selects (deployment.RuntimeSection).
_PRODUCTION_PROFILES = frozenset({"standard", "regulated"})


class ProbeRefused(Exception):
    """The target may not be probed. Nothing ran."""


@dataclass
class ProbeTrace:
    """One recording, as probing needs it.

    ``bundle_dir`` holds the files the workflow's anchors reference
    (template crops). ``identity`` is the recording's subject, keyed by the
    reward contract's identity keys; when None it is read from the
    workflow's ``params``.
    """

    workflow: Workflow
    bundle_dir: Path
    label: str
    identity: Optional[dict[str, str]] = None


@dataclass
class RunSignal:
    """What a probe replay reports back, before the read-back judges it."""

    signal: str
    model_calls: int = 0
    detail: str = ""


@runtime_checkable
class ProbeTarget(Protocol):
    """A resettable test copy that can replay a workflow in a fresh session."""

    name: str

    def reset(self) -> None:
        """Restore the copy to its baseline. Raise if that fails."""
        ...

    def run(
        self,
        workflow: Workflow,
        *,
        bundle_dir: Path,
        run_dir: Path,
        params: dict[str, str],
    ) -> RunSignal:
        """Replay ``workflow`` in a new session; never reuse one."""
        ...


# ---------------------------------------------------------------------------
# The test-copy gate
# ---------------------------------------------------------------------------


def probe_target_refusals(cfg: Any) -> list[str]:
    """Why ``cfg`` (a DeploymentConfig) may not be probed; empty when it may."""

    reasons: list[str] = []
    section = getattr(cfg, "test_copy", None)
    if section is None or section.declared is not True:
        reasons.append(
            "the deployment file does not declare its target a test copy "
            "(test_copy.declared: true)"
        )
        return reasons
    if not section.reset_url and not section.reset_command:
        reasons.append(
            "test_copy names no reset (reset_url or reset_command); a probe "
            "needs a copy it can restore before every run"
        )
    if not section.reward_contract:
        reasons.append(
            "test_copy names no reward_contract; a probe is scored by its "
            "read-back and nothing else"
        )
    profile = getattr(cfg.runtime, "profile", None)
    if profile in _PRODUCTION_PROFILES:
        reasons.append(
            f"runtime.profile is {profile!r}, a production profile; a test "
            "copy runs without one or with 'demo'"
        )
    backend = cfg.backend
    if backend.browser_cdp_endpoint or getattr(backend, "browser_page_url", None):
        reasons.append(
            "backend attaches to an already-open browser; each probe run needs "
            "a fresh session"
        )
    return reasons


def require_test_copy(cfg: Any) -> None:
    reasons = probe_target_refusals(cfg)
    if reasons:
        raise ProbeRefused("; ".join(reasons))


# ---------------------------------------------------------------------------
# Interpretations -> concrete per-recording workflows
# ---------------------------------------------------------------------------


def _guarded_copy(step: Step, guard: Optional[Guard] = None) -> Step:
    copy = step.model_copy(deep=True)
    copy.guard = guard if guard is not None else optional_guard(step)
    return copy


def interpretation_workflow(
    traces: list[ProbeTrace],
    point: OptionalPoint,
    t: int,
    interpretation: str,
    narrated: Optional[NarratedGuard] = None,
) -> tuple[Workflow, Optional[int]]:
    """Recording ``t`` replayed under ``interpretation`` of ``point``.

    Every other step is the recording's own, so every other point keeps what
    that recording did. Returns the workflow and, when the step was copied in
    from another recording, its index (its anchor's files live in that
    recording's bundle).
    """

    wf = traces[t].workflow
    steps = [s.model_copy(deep=True) for s in wf.steps]
    inserted: Optional[int] = None
    if interpretation == "incidental":
        if t in point.index_in_trace:
            del steps[point.index_in_trace[t]]
    elif interpretation in ("conditional", NARRATED):
        guard = None
        if interpretation == NARRATED:
            if narrated is None:
                raise ValueError("the narrated interpretation needs a narrated guard")
            guard = Guard(predicate=narrated.guard, on_unmet="skip")
        if t in point.index_in_trace:
            k = point.index_in_trace[t]
            steps[k] = _guarded_copy(wf.steps[k], guard)
        else:
            inserted = point.insert_at[t]
            steps.insert(inserted, _guarded_copy(point.step, guard))
    else:
        raise ValueError(f"unknown interpretation {interpretation!r}")
    out = wf.model_copy(update={"steps": steps, "program": None})
    return out, inserted


def _stage_bundle(
    traces: list[ProbeTrace],
    point: OptionalPoint,
    t: int,
    workflow: Workflow,
    inserted: Optional[int],
    dest: Path,
) -> Workflow:
    """Copy recording ``t``'s bundle to ``dest`` and bring in the files an
    inserted step's anchor needs from the recording it came from."""

    if dest.exists():
        shutil.rmtree(dest)
    src = traces[t].bundle_dir
    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        dest.mkdir(parents=True)
    if inserted is None:
        return workflow
    origin = traces[point.source_trace].bundle_dir
    step = workflow.steps[inserted]
    prefix = f"probe-from-{point.source_trace}"
    for anchor in (step.anchor, getattr(step, "drag_end_anchor", None)):
        if anchor is None or not anchor.template:
            continue
        source = origin / anchor.template
        if not source.is_file():
            continue
        target = dest / prefix / anchor.template
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        anchor.template = f"{prefix}/{anchor.template}"
    return workflow


# ---------------------------------------------------------------------------
# Scoring: the serve-reward judge over the contract's own oracle
# ---------------------------------------------------------------------------


@dataclass
class _Scorer:
    bundle: Any  # openadapt_flow.reward.models.RewardBundle
    oracle: Any = field(init=False)

    def __post_init__(self) -> None:
        from openadapt_flow.reward.oracles import build_oracle

        self.oracle = build_oracle(self.bundle.oracle, self.bundle.directory)

    @property
    def substrate(self) -> str:
        return self.bundle.oracle.channel.value

    def baseline(self, identity: dict[str, str]) -> Any:
        from openadapt_flow.reward.oracles import effect_state_of

        return effect_state_of(self.oracle.read(identity), self.substrate)

    def judge(self, identity: dict[str, str], signal: str, before: Any) -> Any:
        from openadapt_flow.reward.worker import judge_episode

        observed = self.oracle.read(identity)
        return judge_episode(self.bundle, identity, signal, before, observed)


def _identity(trace: ProbeTrace, keys: tuple[str, ...]) -> dict[str, str]:
    if trace.identity is not None:
        return {k: str(v) for k, v in trace.identity.items()}
    params = trace.workflow.params or {}
    missing = [k for k in keys if params.get(k) in (None, "")]
    if missing:
        raise ValueError(
            f"recording {trace.label} has no value for the reward contract's "
            f"identity key(s) {', '.join(missing)} in its params"
        )
    return {k: str(params[k]) for k in keys}


def _params(trace: ProbeTrace) -> dict[str, str]:
    return {k: str(v) for k, v in (trace.workflow.params or {}).items()}


def _probe_once(
    *,
    target: ProbeTarget,
    scorer: _Scorer,
    traces: list[ProbeTrace],
    point: OptionalPoint,
    t: int,
    interpretation: str,
    identity: dict[str, str],
    work_dir: Path,
    narrated: Optional[NarratedGuard] = None,
) -> ProbeRun:
    slug = f"{point.location}-{interpretation}-{t}"
    workflow, inserted = interpretation_workflow(
        traces, point, t, interpretation, narrated
    )
    params = _params(traces[t])
    if interpretation == NARRATED and narrated is not None:
        # A guard on a recorded field compares the run parameter induction
        # names; give each run its recording's value for it.
        params.update(narrated.trace_params.get(t, {}))
    base: dict[str, Any] = {
        "location": point.location,
        "interpretation": interpretation,
        "context": traces[t].label,
    }
    try:
        workflow = _stage_bundle(
            traces, point, t, workflow, inserted, work_dir / "bundles" / slug
        )
        target.reset()
        before = scorer.baseline(identity)
    except Exception as exc:  # noqa: BLE001 - logged, and the run decides nothing
        return ProbeRun(
            **base,
            runtime_signal="failed_platform",
            outcome="failed_platform",
            produced_recorded_effect=False,
            conclusive=False,
            reason=f"could not prepare the run: {exc}",
        )
    try:
        signal = target.run(
            workflow,
            bundle_dir=work_dir / "bundles" / slug,
            run_dir=work_dir / "runs" / slug,
            params=params,
        )
    except Exception as exc:  # noqa: BLE001 - a crashed replay is a platform failure
        signal = RunSignal("failed_platform", detail=f"replay raised: {exc}")
    if signal.signal not in RUNTIME_SIGNALS:
        signal = RunSignal(
            "failed_platform",
            signal.model_calls,
            f"unknown runtime signal {signal.signal!r}",
        )
    judged = scorer.judge(identity, signal.signal, before)
    outcome = judged.outcome.value
    reason = judged.reason
    if signal.detail:
        reason = f"{reason}; {signal.detail}" if reason else signal.detail
    return ProbeRun(
        **base,
        runtime_signal=signal.signal,
        outcome=outcome,
        produced_recorded_effect=outcome == "verified",
        conclusive=outcome not in _INCONCLUSIVE_OUTCOMES,
        reason=reason,
        model_calls=signal.model_calls,
    )


def _probe_point(
    *,
    target: ProbeTarget,
    scorer: _Scorer,
    traces: list[ProbeTrace],
    identities: list[dict[str, str]],
    point: OptionalPoint,
    work_dir: Path,
    runs: list[ProbeRun],
    narrated: Optional[NarratedGuard] = None,
) -> ProbeVerdict:
    status: dict[str, str] = {}
    readings = INTERPRETATIONS + ((NARRATED,) if narrated is not None else ())
    for interpretation in readings:
        state = "passed"
        for t in range(len(traces)):
            run = _probe_once(
                target=target,
                scorer=scorer,
                traces=traces,
                point=point,
                t=t,
                interpretation=interpretation,
                identity=identities[t],
                work_dir=work_dir,
                narrated=narrated,
            )
            runs.append(run)
            if run.produced_recorded_effect:
                continue
            if run.conclusive:
                state = "failed"
                break  # one conclusive miss rules this interpretation out
            state = "inconclusive"  # keep looking for a conclusive miss
        status[interpretation] = state
    if narrated is not None:
        if status[NARRATED] == "passed" and status["incidental"] == "failed":
            return ProbeVerdict(
                location=point.location,
                kind=point.kind,
                interpretations=status,
                certified=NARRATED,
                detail=(
                    f"the narrated guard ({narrated.rule}) produced the recorded "
                    f"effect in all {len(traces)} recording contexts and dropping "
                    "the step did not"
                ),
            )
    base = {k: v for k, v in status.items() if k in INTERPRETATIONS}
    passed = [k for k, v in base.items() if v == "passed"]
    others_failed = all(v == "failed" for k, v in base.items() if k not in passed)
    if len(passed) == 1 and others_failed:
        return ProbeVerdict(
            location=point.location,
            kind=point.kind,
            interpretations=status,
            certified=passed[0],
            detail=(
                f"only {passed[0]} produced the recorded effect in all "
                f"{len(traces)} recording contexts"
            ),
        )
    if len(passed) > 1:
        detail = "more than one interpretation produced the recorded effect"
    elif not passed and all(v == "failed" for v in base.values()):
        detail = "no interpretation produced the recorded effect"
    else:
        detail = "a read-back could not decide"
    return ProbeVerdict(
        location=point.location,
        kind=point.kind,
        interpretations=status,
        detail=detail + "; the point stays refused",
    )


def probe_and_induce(
    traces: list[ProbeTrace],
    *,
    target: ProbeTarget,
    reward_contract: Union[Any, str, Path],
    work_dir: Path,
    propose: Optional[Proposer] = None,
    narration: Optional[list[Any]] = None,
    screen_texts: Any = None,
    interpreter: Any = None,
    accepted_narration: Optional[dict[str, str]] = None,
) -> InductionResult:
    """Induce, then probe each refused optional point on ``target``.

    ``narration`` (one per trace) and the other narration arguments are passed
    to :func:`induce_program`. Where narration proposed a guard that no
    recording contradicts, the ``narrated`` reading is probed as well.

    ``target`` must already have passed :func:`require_test_copy` (the CLI
    does this from the deployment file). ``reward_contract`` is a
    ``serve-reward`` contract bundle (a directory or a loaded
    :class:`~openadapt_flow.reward.models.RewardBundle`).

    The returned result carries every probe run and one verdict per refused
    point. Points probing did not settle stay refused, with their record-next
    items, exactly as plain induction leaves them.
    """

    from openadapt_flow.reward.models import RewardBundle

    bundle = (
        reward_contract
        if isinstance(reward_contract, RewardBundle)
        else RewardBundle.load(reward_contract)
    )
    workflows: list[TraceInput] = [tr.workflow for tr in traces]
    # The proposer is advisory and never decides a probe, so the first pass
    # runs without it; it is consulted once, in the pass whose result returns.
    spoken: dict[str, Any] = {
        "narration": narration if narration is not None else [None] * len(traces),
        "screen_texts": screen_texts,
        "interpreter": interpreter,
        "accepted_narration": accepted_narration,
    }
    first = induce_program(workflows, **spoken)
    name = str(getattr(target, "name", "test copy"))
    if not first.uncertainties:
        result = (
            induce_program(workflows, propose=propose, **spoken) if propose else first
        )
        result.probe_target = name
        return result

    runs: list[ProbeRun] = []
    verdicts: list[ProbeVerdict] = []
    resolutions: dict[str, str] = {}
    try:
        identities = [_identity(tr, bundle.identity_keys) for tr in traces]
        for identity in identities:
            bundle.check_identity(identity)
    except ValueError as exc:
        verdicts = [
            ProbeVerdict(
                location=u.location,
                kind=u.kind,
                detail=f"not probed: {exc}; the point stays refused",
            )
            for u in first.uncertainties
        ]
        identities = []

    if identities:
        scorer = _Scorer(bundle)
        points = optional_points(workflows)
        narrated = {
            ng.location: ng for ng in first.narrated_guards if ng.status == "proposed"
        }
        for u in first.uncertainties:
            point = points.get(u.location)
            if point is None or point.kind != u.kind:
                verdicts.append(
                    ProbeVerdict(
                        location=u.location,
                        kind=u.kind,
                        detail=(
                            f"not probed: {u.kind} has no replayable "
                            "interpretations; the point stays refused"
                        ),
                    )
                )
                continue
            verdict = _probe_point(
                target=target,
                scorer=scorer,
                traces=traces,
                identities=identities,
                point=point,
                work_dir=Path(work_dir),
                runs=runs,
                narrated=narrated.get(point.location),
            )
            verdicts.append(verdict)
            if verdict.certified == NARRATED:
                # Pin the exact guard that was probed; a later pass that
                # proposes a different one is not certified by this probe.
                ng = narrated[point.location]
                resolutions[point.location] = f"{NARRATED}@{ng.guard_id}"
            elif verdict.certified:
                resolutions[point.location] = verdict.certified
        try:
            target.reset()  # leave the copy at its baseline
        except Exception:  # noqa: BLE001 - every run already reset first
            pass

    result = induce_program(
        workflows, propose=propose, probe_resolutions=resolutions or None, **spoken
    )
    result.probe_target = name
    result.probe_runs = runs
    result.probe_verdicts = verdicts
    return result


# ---------------------------------------------------------------------------
# The deployment-file target
# ---------------------------------------------------------------------------


def runtime_signal_of(report: Any) -> str:
    """Map a replay report onto the serve-reward runtime signal."""

    if getattr(report, "success", False):
        return "completed"
    return "halted_before_effect"


class DeploymentProbeTarget:
    """The test copy a deployment file describes.

    Reset: ``test_copy.reset_url`` (HTTP POST) or ``test_copy.reset_command``
    (argv, no shell). Run: a new browser (``web``) or a new backend for every
    run, wired by :func:`openadapt_flow.deployment.build_replayer` with model
    grounding and egress off and no effect verifier (the probe's read-back
    scores the run).
    """

    def __init__(self, cfg: Any, *, reset_timeout_s: float = 30.0) -> None:
        require_test_copy(cfg)
        self.cfg = cfg
        self.name = str(cfg.name)
        self.reset_timeout_s = reset_timeout_s

    def reset(self) -> None:
        section = self.cfg.test_copy
        if section.reset_url:
            request = urllib.request.Request(
                section.reset_url,
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(  # noqa: S310 - operator-declared URL
                request, timeout=self.reset_timeout_s
            ) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"reset returned HTTP {response.status}")
        if section.reset_command:
            subprocess.run(  # noqa: S603 - argv list, no shell
                list(section.reset_command),
                check=True,
                timeout=self.reset_timeout_s,
                capture_output=True,
            )

    def _replay(self, backend: Any, workflow, bundle_dir, run_dir, params) -> RunSignal:
        from openadapt_flow.deployment import build_replayer

        replayer = build_replayer(
            backend,
            allow_egress=False,
            effect_verifier=None,
            api_actuator=None,
            durable=False,
            use_structural=True,
        )
        report = replayer.run(
            workflow, bundle_dir=bundle_dir, run_dir=run_dir, params=params
        )
        return RunSignal(
            runtime_signal_of(report),
            model_calls=int(getattr(report, "model_calls", 0) or 0),
        )

    def run(
        self,
        workflow: Workflow,
        *,
        bundle_dir: Path,
        run_dir: Path,
        params: dict[str, str],
    ) -> RunSignal:
        from openadapt_flow.backends.factory import build_backend

        backend_cfg = self.cfg.backend
        if backend_cfg.kind != "web":
            backend = build_backend(backend_cfg)
            try:
                return self._replay(backend, workflow, bundle_dir, run_dir, params)
            finally:
                close = getattr(backend, "close", None)
                if callable(close):
                    close()
        if not backend_cfg.url:
            raise ValueError("a web test copy needs backend.url")
        from playwright.sync_api import sync_playwright

        from openadapt_flow._browser_setup import launch_chromium

        with sync_playwright() as p:
            browser = launch_chromium(p.chromium, headless=not backend_cfg.headed)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 800})
                page = context.new_page()
                page.goto(backend_cfg.url)
                backend = build_backend(backend_cfg, page=page)
                return self._replay(backend, workflow, bundle_dir, run_dir, params)
            finally:
                browser.close()
