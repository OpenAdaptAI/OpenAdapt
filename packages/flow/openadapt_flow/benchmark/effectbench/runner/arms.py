"""Agent arms — the common interface every baseline drives the task through.

An **arm** is one agent under test. The whole point of the multi-baseline runner
is that *every* arm — the OpenAdapt compiler, a screen-only ablation, a mock,
and (later, funded) Claude computer-use / OpenAI CUA / UI-TARS / Skyvern — runs
the **identical** task and is judged by the **identical** independent oracle, so
a difference between arms is the AGENT, never the harness.

The interface (one method):

.. code-block:: python

    class AgentArm(Protocol):
        name: str
        live: bool
        def run(self, task: TaskSpec, session: SubstrateSession,
                *, params: Mapping[str, str]) -> ArmResult: ...

An arm is handed ONLY the task's natural-language ``goal`` (intent, never a step
list — the fairness requirement) via ``task``, a
:class:`~openadapt_flow.benchmark.effectbench.runner.substrate.SubstrateSession`
to drive, and the run's ``params`` (the trial-unique payload it must write). It
returns an :class:`ArmResult` carrying the untrusted
:class:`~openadapt_flow.benchmark.effectbench.schema.AgentReport` plus any
recorded :class:`~openadapt_flow.benchmark.effectbench.schema.ModelCall` rows so
cost is auditable. The harness (:mod:`.harness`) adapts ``run`` into the
``run_action: Callable[[], AgentReport]`` that
:func:`~openadapt_flow.benchmark.effectbench.oracle.score_episode` calls, and
supplies the independent oracle — which the arm can NEVER reach.

Three concrete in-repo arms live here:

* :class:`CompilerArm` — the OpenAdapt record→compile→replay path: it performs
  the deterministic compiled action, then GATES success on its own effect
  verifier (the app's record readback). On a transactional fault it refuses
  (reports failure / halts) rather than trust the screen — so it is never
  *silently* wrong.
* :class:`ScreenOnlyArm` — the ablation: same deterministic action, but success
  is read from the SCREEN banner with NO effect check. This is the arm that
  exhibits silent wrong-effects (a green banner over a bad record).
* :class:`MockArm` — a deterministic, substrate-free arm for CI: it returns a
  scripted report without driving any server, so the harness/metrics pipeline
  can be unit-tested hermetically.

External paid baselines are scaffolded (not live) in :mod:`.baselines`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from openadapt_flow.benchmark.effectbench.runner.substrate import (
    ScreenObservation,
    SubstrateSession,
)
from openadapt_flow.benchmark.effectbench.schema import (
    AgentReport,
    ModelCall,
    TaskSpec,
)
from openadapt_flow.runtime.effects.effect import EffectVerifier


@dataclass
class ArmResult:
    """What an arm returns for one episode.

    ``report`` is the untrusted self-report the oracle crosses against the true
    effect. ``model_calls`` carries per-call cost/latency so ``cost_usd`` is
    ``sum(model_calls)`` and fully auditable — a zero-model arm (the compiler)
    returns an empty list.
    """

    report: AgentReport
    model_calls: list[ModelCall] = field(default_factory=list)
    #: Optional per-arm environment fingerprint additions (model/tool versions).
    env_fingerprint: dict[str, Any] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.model_calls)


@runtime_checkable
class AgentArm(Protocol):
    """The common arm interface (see module docstring)."""

    #: Stable arm name recorded on every ``EpisodeRecord`` (``"compiler"`` …).
    name: str
    #: ``True`` if this arm actually executes here; ``False`` for a scaffolded
    #: external baseline that needs credentials + a funded run (:mod:`.baselines`).
    live: bool

    def run(
        self, task: TaskSpec, session: SubstrateSession, *, params: Mapping[str, str]
    ) -> ArmResult:
        """Drive the task through ``session`` and return the untrusted report."""
        ...


# ---------------------------------------------------------------------------
# Concrete, in-repo arms.
# ---------------------------------------------------------------------------


class ScreenOnlyArm:
    """The silent-wrong-effect ablation: report success from the SCREEN alone.

    Performs the SAME deterministic action as the compiler arm, then reports
    success iff the app painted a "saved" banner — with NO independent effect
    check. This is precisely the vision/pixel posture EffectBench indicts: a
    partial save, a duplicate, a phantom optimistic-UI success, or a lost update
    all leave the banner green, so this arm reports success while the record is
    wrong or empty. It should surface a non-zero SWER.
    """

    name = "screen_only"
    live = True

    def run(
        self, task: TaskSpec, session: SubstrateSession, *, params: Mapping[str, str]
    ) -> ArmResult:
        obs: ScreenObservation = session.attempt_intended_action(params)
        # The whole ablation: trust the banner. No effect verification.
        return ArmResult(
            report=AgentReport(
                reported_success=obs.banner_saved,
                halted=not obs.banner_saved,
                message=f"screen-only verdict from banner: {obs.detail}",
            )
        )


class CompilerArm:
    """The OpenAdapt compiler arm: deterministic replay GATED on the effect.

    Models the record→compile→replay product path. It performs the same
    deterministic compiled action as the ablation, but decides success by
    VERIFYING the effect against the app's own system of record (its
    ``product_effect_verifier`` — the record readback the compiler mined at demo
    time), not the screen. Fail-safe: a REFUTED or INDETERMINATE effect makes it
    report failure / halt rather than trust a green banner. Consequently it is
    never *silently* wrong — on a fault it either surfaces a flagged wrong-action
    or halts; on a clean run it confirms and succeeds (and it even RECOVERS a
    false-abort, e.g. a committed-but-timed-out write, that the screen missed).

    Fairness / isolation: the verifier this arm uses is the AGENT's own product
    capability, a different object and transport from the benchmark oracle the
    harness scores with. This arm never receives, imports, or can reach that
    oracle. It captures its own pre-state BEFORE acting so its at-most-once /
    collateral-loss deltas are honest.
    """

    name = "compiler"
    live = True

    def run(
        self, task: TaskSpec, session: SubstrateSession, *, params: Mapping[str, str]
    ) -> ArmResult:
        verifier: Optional[EffectVerifier] = session.product_effect_verifier()
        if verifier is None:
            # No app record API to verify against: fail safe — the compiler does
            # not trust the screen, so it halts rather than guess success.
            session.attempt_intended_action(params)
            return ArmResult(
                report=AgentReport(
                    reported_success=False,
                    halted=True,
                    message="no product effect verifier available; halting (fail-safe)",
                )
            )

        # 1) snapshot the app's own record BEFORE the action (its own baseline).
        before = verifier.capture_pre_state()
        # 2) perform the deterministic compiled action.
        obs = session.attempt_intended_action(params)
        # 3) verify the effect (record + note) against the app's system of record.
        resolved = task.expected_effect.resolve(dict(params))
        verdict = verifier.verify(resolved, before)

        if verdict.confirmed:
            return ArmResult(
                report=AgentReport(
                    reported_success=True,
                    halted=False,
                    message=(
                        f"effect CONFIRMED by product verifier "
                        f"({verifier.substrate}); screen said {obs.banner_saved}"
                    ),
                )
            )
        # REFUTED or INDETERMINATE -> refuse to claim success (halt).
        return ArmResult(
            report=AgentReport(
                reported_success=False,
                halted=True,
                message=(
                    f"effect {verdict.verdict.value} by product verifier "
                    f"({verdict.reason}); halting rather than trust banner "
                    f"{obs.banner_saved}"
                ),
            )
        )


class MockArm:
    """A deterministic, substrate-free arm for hermetic CI.

    Does NOT drive any server. It returns a scripted
    :class:`AgentReport` computed by ``decide`` (default: always report
    success). Paired with an in-memory oracle over a fixed record list, it lets
    the harness/metrics pipeline be unit-tested without HTTP — and demonstrates
    that an always-"success" agent is classified SILENT_WRONG_EFFECT the moment
    the oracle sees a bad record.

    Args:
        decide: ``(task, params) -> AgentReport``. Defaults to always-success.
        name: Arm name (default ``"mock"``); override to script several mocks.
        model_calls: Optional fixed cost rows to attribute to every episode
            (e.g. to exercise cost accounting deterministically).
    """

    live = True

    def __init__(
        self,
        decide: Optional[Callable[[TaskSpec, Mapping[str, str]], AgentReport]] = None,
        *,
        name: str = "mock",
        model_calls: Optional[list[ModelCall]] = None,
    ) -> None:
        self.name = name
        self._decide = decide or (
            lambda _task, _params: AgentReport(
                reported_success=True, halted=False, message="mock: always success"
            )
        )
        self._model_calls = list(model_calls or [])

    def run(
        self, task: TaskSpec, session: SubstrateSession, *, params: Mapping[str, str]
    ) -> ArmResult:
        report = self._decide(task, dict(params))
        return ArmResult(
            report=report,
            model_calls=[c.model_copy() for c in self._model_calls],
            env_fingerprint={"arm_kind": "mock"},
        )


__all__ = [
    "ArmResult",
    "AgentArm",
    "ScreenOnlyArm",
    "CompilerArm",
    "MockArm",
]
