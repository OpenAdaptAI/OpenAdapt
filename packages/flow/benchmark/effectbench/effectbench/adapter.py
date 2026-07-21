"""The adapter interface -- plug YOUR agent / system into EffectBench.

A third party makes their system runnable under EffectBench by implementing one
method: :class:`SystemUnderTest.run_task`. The benchmark owns the reference
system of record (synthetic MockMed), the tasks, and the independent oracle; the
system under test only has to *attempt each task's goal* against the reference
system and return its own (untrusted) self-report. EffectBench reads the TRUE
effect through the oracle -- never through the system under test -- and computes
its SWER.

The contract, in full::

    @runtime_checkable
    class SystemUnderTest(Protocol):
        name: str
        def run_task(self, task: TaskSpec, env: EnvHandle,
                     *, params: Mapping[str, str]) -> AgentReport: ...

The system under test receives:

- ``task`` -- the natural-language ``goal`` (intent, NEVER a step list -- a
  fairness requirement) plus the substrate and category;
- ``env`` -- an :class:`EnvHandle` to drive the reference system and read back
  ONLY the screen banner (the untrusted witness). ``env`` never exposes the
  oracle's read path;
- ``params`` -- the run's trial-unique payload (the note / key it must write).

It returns an :class:`~effectbench.schema.AgentReport` -- what it asserts /
what the screen rendered. The oracle, not this report, decides the outcome.

Two reference baselines ship here so a third party has a runnable template and
so the reference result is reproducible:

- :class:`ScreenOnlySUT` -- trusts the banner (no effect check). Exhibits a
  non-zero SWER: the arm the benchmark indicts.
- :class:`EffectVerifiedSUT` -- consults its OWN independent record-readback
  verifier and refuses to claim success unless the write is CONFIRMED. Reaches
  SWER 0 -- at an over-halt cost the benchmark also reports.

An honest run reports SWER and over-halt JOINTLY: an agent trivially reaches
SWER 0 by halting on everything (over-halt 100%).
"""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from effectbench.effect import EffectVerifier
from effectbench.fixtures.mockmed import ScreenObservation
from effectbench.schema import AgentReport, TaskSpec


@runtime_checkable
class EnvHandle(Protocol):
    """The arm-facing action + perception channel for one episode.

    Exposes only what a real agent could do and perceive; the oracle is not
    reachable through it.
    """

    goal: str

    def attempt_intended_action(self, params: Mapping[str, str]) -> ScreenObservation:
        """Perform the task's intended action once and return the screen banner."""
        ...

    def product_effect_verifier(self) -> EffectVerifier | None:
        """The agent's OWN independent record-readback verifier, or ``None``.

        A distinct object and read path from the harness oracle -- using it is
        the honest way an agent refuses to trust the screen.

        REFERENCE CONVENIENCE, NOT A FREEBIE. On the built-in synthetic MockMed
        fixture the reference provider hands a working verifier here so the
        reference result is reproducible. On a REAL system of record, AUTHORING
        this verifier is the actual cost the benchmark does NOT measure for you:
        a :class:`~effectbench.provider.BenchmarkProvider` that does not supply
        one returns ``None`` here, and :class:`EffectVerifiedSUT` then FAILS SAFE
        (halts / over-halts) rather than getting a correct oracle for free.
        """
        ...


@runtime_checkable
class SystemUnderTest(Protocol):
    """The one-method interface a third party implements (see module docstring)."""

    #: Stable name recorded as the arm on every episode row.
    name: str

    def run_task(
        self, task: TaskSpec, env: EnvHandle, *, params: Mapping[str, str]
    ) -> AgentReport:
        """Attempt ``task``'s goal through ``env`` and return the untrusted report."""
        ...


class ScreenOnlySUT:
    """Reference baseline: report success from the SCREEN banner alone.

    Performs the intended action, then reports success iff the app painted a
    "saved" banner -- with NO independent effect check. A partial save, a
    duplicate, a phantom optimistic success, or a lost update all leave the
    banner green, so this baseline reports success while the record is wrong or
    empty. It surfaces the benchmark's headline SWER.
    """

    name = "screen_only"

    def run_task(
        self, task: TaskSpec, env: EnvHandle, *, params: Mapping[str, str]
    ) -> AgentReport:
        obs = env.attempt_intended_action(params)
        return AgentReport(
            reported_success=obs.banner_saved,
            halted=not obs.banner_saved,
            message=f"screen-only verdict from banner: {obs.detail}",
        )


class EffectVerifiedSUT:
    """Reference baseline: deterministic action GATED on an independent effect.

    Performs the same intended action, but decides success by VERIFYING the
    effect against its own record readback (a distinct verifier instance, not the
    benchmark oracle). Fail-safe: a REFUTED or INDETERMINATE effect makes it
    report failure / halt rather than trust a green banner. It is therefore never
    *silently* wrong -- on a fault it surfaces a flagged wrong-action or halts;
    on a clean run it confirms and succeeds (and it even RECOVERS a committed-but-
    timed-out write the screen missed).
    """

    name = "effect_verified"

    def run_task(
        self, task: TaskSpec, env: EnvHandle, *, params: Mapping[str, str]
    ) -> AgentReport:
        verifier = env.product_effect_verifier()
        if verifier is None:
            env.attempt_intended_action(params)
            return AgentReport(
                reported_success=False,
                halted=True,
                message="no product effect verifier available; halting (fail-safe)",
            )
        before = verifier.capture_pre_state()
        obs = env.attempt_intended_action(params)
        resolved = task.expected_effect.resolve(dict(params))
        verdict = verifier.verify(resolved, before)
        if verdict.confirmed:
            return AgentReport(
                reported_success=True,
                halted=False,
                message=(
                    f"effect CONFIRMED by product verifier ({verifier.substrate}); "
                    f"screen said {obs.banner_saved}"
                ),
            )
        return AgentReport(
            reported_success=False,
            halted=True,
            message=(
                f"effect {verdict.verdict.value} by product verifier "
                f"({verdict.reason}); halting rather than trust banner "
                f"{obs.banner_saved}"
            ),
        )
