"""Phase-2 stubs for external interactive benchmarks (OSWorld, BrowserGym).

These are **interface skeletons only** -- deliberately unimplemented. Phase 1
consolidates the OpenAdapt-native families (WAA, Parallels, MockMed, OpenEMR)
behind :class:`~openadapt_evals.harness.protocol.Environment`. Phase 2 wires two
external suites into the SAME protocol, reusing their NATIVE execution verifiers
(the whole reason to adopt them: real, community-maintained task graders) --
never re-scoring with our own.

Nothing here installs or imports OSWorld / BrowserGym. Each method raises
:class:`NotImplementedError` with the concrete phase-2 wiring plan so the shape
of the work is captured without pulling the dependencies into phase 1.
"""

from __future__ import annotations

from typing import Any

from openadapt_types import BenchmarkAction, BenchmarkTask

from openadapt_evals.harness.protocol import Observation, VerificationResult

_PHASE2 = "phase 2: not implemented in the lightweight meta-benchmark (phase 1)"


class OSWorldAdapter:
    """STUB -- OSWorld (Ubuntu desktop, 369 tasks) as an :class:`Environment`.

    Phase-2 wiring plan:

    - ``reset`` boots OSWorld's ``DesktopEnv`` VM to the task's snapshot and
      returns its screenshot + a11y observation (map into
      :class:`~openadapt_types.BenchmarkObservation`).
    - ``act`` translates a canonical :class:`~openadapt_types.BenchmarkAction`
      into OSWorld's ``pyautogui``-style action and steps the env.
    - ``verify`` delegates to OSWorld's NATIVE ``env.evaluate()`` -- each task
      ships a ``getter`` + a ``metric`` function that inspects real machine
      state (files, configs, command output). Map its ``[0, 1]`` reward into a
      :class:`VerificationResult` (``success = reward >= 1.0``). We do NOT
      substitute our own scoring: the point of adopting OSWorld is its curated
      execution verifiers.
    - Heavy (a VM per task); build lazily, keep injectable/mockable exactly like
      the WAA + Parallels envs.

    Do NOT install OSWorld in phase 1.
    """

    name = "osworld"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(_PHASE2)

    def reset(self, task: BenchmarkTask) -> Observation:
        raise NotImplementedError(_PHASE2)

    def observe(self) -> Observation:
        raise NotImplementedError(_PHASE2)

    def act(self, action: BenchmarkAction) -> "tuple[Observation, bool, dict[str, Any]]":
        raise NotImplementedError(_PHASE2)

    def verify(self, task: BenchmarkTask) -> VerificationResult:
        raise NotImplementedError(_PHASE2)

    def close(self) -> None:
        raise NotImplementedError(_PHASE2)


class BrowserGymAdapter:
    """STUB -- BrowserGym (WebArena/MiniWoB/WorkArena) as an :class:`Environment`.

    Phase-2 wiring plan:

    - ``reset`` creates the BrowserGym ``gym.make("browsergym/<task>")`` env and
      returns its observation (screenshot + AXTree + DOM) mapped into
      :class:`~openadapt_types.BenchmarkObservation`.
    - ``act`` renders a canonical action into BrowserGym's high-level action
      string (e.g. ``click(bid)`` / ``fill(bid, text)``) and steps the env.
    - ``verify`` delegates to BrowserGym's NATIVE per-task reward: the underlying
      WebArena/WorkArena validators return terminal reward ``1.0`` on success.
      Read the last ``step`` reward (or call the task's validator) and map into a
      :class:`VerificationResult`. As with OSWorld, we reuse the suite's own
      execution verifier rather than re-grading.
    - Reuse the flow ``PlaywrightBackend`` where a raw browser is preferable to
      the gym wrapper; keep the env lazy + mockable.

    Do NOT install BrowserGym in phase 1.
    """

    name = "browsergym"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(_PHASE2)

    def reset(self, task: BenchmarkTask) -> Observation:
        raise NotImplementedError(_PHASE2)

    def observe(self) -> Observation:
        raise NotImplementedError(_PHASE2)

    def act(self, action: BenchmarkAction) -> "tuple[Observation, bool, dict[str, Any]]":
        raise NotImplementedError(_PHASE2)

    def verify(self, task: BenchmarkTask) -> VerificationResult:
        raise NotImplementedError(_PHASE2)

    def close(self) -> None:
        raise NotImplementedError(_PHASE2)


__all__ = ["OSWorldAdapter", "BrowserGymAdapter"]
