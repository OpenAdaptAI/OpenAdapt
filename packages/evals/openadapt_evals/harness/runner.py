"""The meta-runner: one driver, one metrics row, across every environment.

:func:`run_meta` drives the demonstration-compiler loop --
record -> compile -> replay (-> heal) -> **verify** -- against ANY
:class:`~openadapt_evals.harness.protocol.Environment`, and emits ONE JSONL row
per ``(env, task, mode)``. The row's ``replay_success`` is the environment's
OWN verifier verdict (:meth:`Environment.verify`), never the policy's
self-report -- the meta-benchmark measures real success, not claimed success.

A *policy* is any callable ``(env, task, obs) -> PolicyOutcome`` that drives the
env forward from the reset observation. Two builders wrap the EXISTING policies:

- :func:`make_replay_policy` -- the demonstrate-then-replay path
  (:mod:`openadapt_evals.flow.replay_runner`): replays a compiled bundle with
  ~0 model calls and reports structural-rung usage + heal events.
- :func:`make_hybrid_policy` -- the compiled-first / agent-fallback-on-halt
  path (:class:`openadapt_evals.flow.hybrid_agent.HybridFlowAgent`).

Plus :func:`make_agent_policy`, a generic ``BenchmarkAgent`` driver over
``env.act`` (used for WAA/mock adapter envs and the tests).

This module is stdlib-only at import time; the flow policies import
``openadapt_flow`` lazily inside their builders.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from openadapt_types import BenchmarkAction, BenchmarkObservation, BenchmarkTask

from openadapt_evals.harness.protocol import Environment, Observation

logger = logging.getLogger(__name__)

#: A policy drives the env forward and reports what it did (NOT whether it
#: succeeded -- success is the env verifier's job). Signature:
#: ``(env, task, reset_observation) -> PolicyOutcome``.
Policy = Callable[[Environment, BenchmarkTask, Observation], "PolicyOutcome"]


@dataclass
class PolicyOutcome:
    """What a policy reports after driving one task (self-reported, not scored).

    Attributes:
        reported_success: The policy's OWN belief it finished (diagnostic only;
            the env verifier is ground truth).
        model_calls: VLM/LLM calls made (~0 for a healthy compiled replay).
        structural_rung_counts: Per-rung structural-verification fire counts.
        num_steps: Steps executed.
        heal_count: Self-heal events triggered.
        cost_usd: Model spend in dollars (from the cost layer / step logs).
        terminal_outcome: ``"success" | "halt" | "error" | ...`` if known.
        error: Error string if the policy aborted.
    """

    reported_success: bool = False
    model_calls: int = 0
    structural_rung_counts: dict[str, int] = field(default_factory=dict)
    num_steps: int = 0
    heal_count: int = 0
    cost_usd: float = 0.0
    terminal_outcome: Optional[str] = None
    error: Optional[str] = None

    @property
    def structural_rung_fire_total(self) -> int:
        return sum(self.structural_rung_counts.values())

    @property
    def structural_rung_rate(self) -> float:
        return (
            self.structural_rung_fire_total / self.num_steps if self.num_steps else 0.0
        )


@dataclass
class MetaMetricsRow:
    """ONE row per ``(env, task, mode)`` -- the meta-benchmark's unit of output.

    The required schema (identical across WAA, Parallels, MockMed, OpenEMR):

    - ``env`` / ``task_id`` / ``mode``: which family, task, and policy.
    - ``replay_success``: the env VERIFIER's verdict (ground truth).
    - ``structural_rung_rate``: structural-verification fires per step.
    - ``model_calls``: VLM/LLM calls (compiled replay ~0).
    - ``effect_verdict``: three-valued effect verdict when the env is
      effect-verified (``confirmed`` / ``refuted`` / ``indeterminate``), else
      None -- the "silent wrong-action" signal.
    - ``wall_ms`` / ``cost_usd``: wall-clock and model spend.

    Diagnostic extras (``reported_success``, ``verifier_score``, ``num_steps``,
    ``heal_count``, ``verifier_source``, ``error``) sit alongside; they never
    substitute for ``replay_success``.
    """

    env: str
    task_id: str
    mode: str
    #: Tri-state. ``True``/``False`` are the verifier's verdict; ``None`` means
    #: the verifier did not run, which is NOT a failure. ``flow/replay_runner``
    #: already models unscored tasks this way and excludes them from its rate;
    #: this row must too, or a crashed verifier is published as a failed task.
    replay_success: Optional[bool]
    structural_rung_rate: float
    model_calls: int
    effect_verdict: Optional[str]
    wall_ms: float
    cost_usd: float
    # Diagnostics (never override replay_success).
    reported_success: bool = False
    #: ``None`` when ``replay_success`` is ``None`` -- there is no score.
    verifier_score: Optional[float] = 0.0
    verifier_source: Optional[str] = None
    #: Why the verifier could not produce a verdict, when it could not.
    verifier_error: Optional[str] = None
    num_steps: int = 0
    heal_count: int = 0
    structural_rung_counts: dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def scored(self) -> bool:
        """True only when the env verifier actually produced a verdict."""
        return self.replay_success is not None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["structural_rung_rate"] = round(self.structural_rung_rate, 4)
        d["wall_ms"] = round(self.wall_ms, 2)
        d["cost_usd"] = round(self.cost_usd, 6)
        d["verifier_score"] = (
            round(self.verifier_score, 4) if self.verifier_score is not None else None
        )
        d["scored"] = self.scored
        return d


def run_meta(
    env: Environment,
    task: BenchmarkTask,
    policy: Policy,
    *,
    mode: Optional[str] = None,
    jsonl_path: Optional[Path | str] = None,
) -> MetaMetricsRow:
    """Drive record->compile->replay(->heal)->verify for one task; emit a row.

    Args:
        env: The environment (any :class:`Environment`).
        task: The task to run (a :class:`~openadapt_evals.harness.protocol.MetaTask`
            carries ``env`` / ``verifier`` / ``demo``).
        policy: Callable that drives the env from the reset observation and
            returns a :class:`PolicyOutcome`.
        mode: Row label for the policy arm (default: ``getattr(policy, "mode",
            policy.__name__)`` or ``"policy"``).
        jsonl_path: When set, the row is appended as one JSON line here.

    Returns:
        The :class:`MetaMetricsRow`. ``replay_success`` is the env verifier's
        verdict -- computed here, after the policy runs -- not the policy's
        self-report.
    """
    env_name = getattr(task, "env", "") or getattr(env, "name", "") or "unknown"
    mode_label = mode or getattr(policy, "mode", None) or getattr(
        policy, "__name__", "policy"
    )

    start = time.monotonic()
    outcome: PolicyOutcome
    error: Optional[str] = None
    try:
        obs = env.reset(task)
        outcome = policy(env, task, obs)
        error = outcome.error
    except Exception as e:  # noqa: BLE001 - one bad task must not abort a suite
        logger.exception("policy failed for %s", getattr(task, "task_id", "?"))
        outcome = PolicyOutcome(terminal_outcome="error", error=str(e))
        error = str(e)

    # Ground truth: the ENV's own verifier, never the policy's self-report.
    # A verifier that CRASHED did not say "the task failed" -- it said nothing.
    # Collapsing that into replay_success=False / verifier_score=0.0 makes an
    # unscored task indistinguishable from a scored failure, and the accuracy
    # computed over these rows silently gains a wrong denominator.
    verifier_result = None
    verifier_error: Optional[str] = None
    try:
        verifier_result = env.verify(task)
        if verifier_result is None:
            verifier_error = "verify() returned None"
    except Exception as e:  # noqa: BLE001
        logger.warning("verify() failed for %s: %s", getattr(task, "task_id", "?"), e)
        verifier_error = f"verify error: {e}"
        # Keep the verify failure even when the policy already errored; `error`
        # used to swallow it whole via `error = error or ...`.
        error = f"{error}; {verifier_error}" if error else verifier_error

    wall_ms = (time.monotonic() - start) * 1000.0

    if verifier_error is not None:
        success: Optional[bool] = None
        score: Optional[float] = None
        details: dict = {}
    else:
        success = bool(getattr(verifier_result, "success", False))
        score = float(getattr(verifier_result, "score", 0.0) or 0.0)
        details = getattr(verifier_result, "details", {}) or {}

    row = MetaMetricsRow(
        env=env_name,
        task_id=getattr(task, "task_id", "?"),
        mode=str(mode_label),
        replay_success=success,
        structural_rung_rate=outcome.structural_rung_rate,
        model_calls=outcome.model_calls,
        effect_verdict=details.get("effect_verdict"),
        wall_ms=wall_ms,
        cost_usd=outcome.cost_usd,
        reported_success=outcome.reported_success,
        verifier_score=score,
        verifier_source=details.get("source"),
        verifier_error=verifier_error,
        num_steps=outcome.num_steps,
        heal_count=outcome.heal_count,
        structural_rung_counts=dict(outcome.structural_rung_counts),
        error=error,
    )

    if jsonl_path is not None:
        path = Path(jsonl_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row.as_dict()) + "\n")

    return row


def run_meta_suite(
    envs_tasks: "list[tuple[Environment, BenchmarkTask, Policy]]",
    *,
    jsonl_path: Optional[Path | str] = None,
) -> list[MetaMetricsRow]:
    """Run many ``(env, task, policy)`` triples, emitting one row each."""
    return [
        run_meta(env, task, policy, jsonl_path=jsonl_path)
        for env, task, policy in envs_tasks
    ]


# ---------------------------------------------------------------------------
# Policies wrapping the EXISTING replay / hybrid / agent drivers.
# ---------------------------------------------------------------------------


def _outcome_from_run_report(report: Any) -> PolicyOutcome:
    """Map a flow ``RunReport``-like object to a :class:`PolicyOutcome`.

    Same field surface the flow replay_runner / hybrid_agent already read
    (``rung_counts`` / ``model_calls`` / ``heal_count`` / ``results`` / ...).
    """
    rung_counts = dict(getattr(report, "rung_counts", {}) or {})
    results = getattr(report, "results", []) or []
    terminal = getattr(report, "terminal_outcome", None)
    reported = bool(getattr(report, "success", False))
    return PolicyOutcome(
        reported_success=reported,
        model_calls=int(getattr(report, "model_calls", 0) or 0),
        structural_rung_counts=rung_counts,
        num_steps=len(results),
        heal_count=int(getattr(report, "heal_count", 0) or 0),
        cost_usd=float(getattr(report, "est_model_cost_usd", 0.0) or 0.0),
        terminal_outcome=terminal,
    )


def make_replay_policy(
    server_url: str,
    *,
    run_root: Path | str = "meta_runs",
    replay_fn: Optional[Callable] = None,
) -> Policy:
    """A policy that replays ``task.demo``'s compiled bundle (the replay leg).

    Wraps :func:`openadapt_evals.flow.replay_runner._default_replay_fn` (a
    ``WindowsBackend`` replay against the WAA/win_agent ``/screenshot`` +
    ``/execute_windows`` contract). ``replay_fn`` is injectable for tests -- no
    flow extra, no VM needed.
    """

    def _policy(env: Environment, task: BenchmarkTask, obs: Observation) -> PolicyOutcome:
        bundle_dir = getattr(task, "demo", None)
        if bundle_dir is None:
            return PolicyOutcome(
                terminal_outcome="error",
                error="replay policy needs task.demo (a compiled bundle_dir)",
            )
        fn = replay_fn
        if fn is None:  # pragma: no cover - lazy real path
            from openadapt_evals.flow.replay_runner import _default_replay_fn

            fn = _default_replay_fn
        run_dir = Path(run_root) / getattr(task, "task_id", "task")
        run_dir.mkdir(parents=True, exist_ok=True)
        report = fn(Path(bundle_dir), server_url, run_dir, getattr(task, "raw_config", {}))
        return _outcome_from_run_report(report)

    _policy.mode = "replay"  # type: ignore[attr-defined]
    return _policy


def make_hybrid_policy(base_agent: Any, server_url: str, **hybrid_kwargs: Any) -> Policy:
    """A policy wrapping :class:`HybridFlowAgent` (compiled-first, agent fallback).

    The hybrid agent replays the compiled bundle and, only on a detected halt
    (and only if the shared spend ledger allows), hands the live env to
    ``base_agent``. Metrics are read from the agent's ``metrics`` dict after it
    declares done.
    """

    def _policy(env: Environment, task: BenchmarkTask, obs: Observation) -> PolicyOutcome:
        from openadapt_evals.flow.hybrid_agent import HybridFlowAgent

        bundle_dir = getattr(task, "demo", None)
        if bundle_dir is None:
            return PolicyOutcome(
                terminal_outcome="error",
                error="hybrid policy needs task.demo (a compiled bundle_dir)",
            )
        agent = HybridFlowAgent(
            base_agent=base_agent,
            bundle_dir=bundle_dir,
            server_url=server_url,
            **hybrid_kwargs,
        )
        agent.reset()
        outcome = _drive_agent_over_env(agent, env, task, obs)
        m = agent.metrics
        outcome.model_calls = int(m.get("compiled_model_calls", outcome.model_calls) or 0)
        outcome.heal_count = int(m.get("compiled_heal_count", outcome.heal_count) or 0)
        outcome.cost_usd = float(m.get("fallback_cost_usd", outcome.cost_usd) or 0.0)
        outcome.structural_rung_counts = dict(
            m.get("compiled_rung_counts", outcome.structural_rung_counts) or {}
        )
        outcome.terminal_outcome = m.get("compiled_terminal_outcome", outcome.terminal_outcome)
        return outcome

    _policy.mode = "hybrid"  # type: ignore[attr-defined]
    return _policy


def make_agent_policy(agent: Any, *, max_steps: int = 15) -> Policy:
    """A generic policy driving any ``BenchmarkAgent`` over ``env.act``.

    The natural policy for adapter-backed envs (WAA live/mock, Local). Runs the
    ``act`` -> ``env.act`` loop until the agent (or a step) signals done or
    ``max_steps`` is hit, accumulating any per-step ``cost_usd`` the agent
    exposes via ``_last_step_logs``.
    """

    def _policy(env: Environment, task: BenchmarkTask, obs: Observation) -> PolicyOutcome:
        agent_reset = getattr(agent, "reset", None)
        if callable(agent_reset):
            agent_reset()
        return _drive_agent_over_env(agent, env, task, obs, max_steps=max_steps)

    _policy.mode = getattr(agent, "mode", "agent")  # type: ignore[attr-defined]
    return _policy


def _drive_agent_over_env(
    agent: Any,
    env: Environment,
    task: BenchmarkTask,
    obs: Observation,
    *,
    max_steps: int = 15,
) -> PolicyOutcome:
    """Run the ``agent.act`` <-> ``env.act`` loop; collect a PolicyOutcome."""
    history: list[tuple[BenchmarkObservation, BenchmarkAction]] = []
    steps = 0
    cost = 0.0
    reported = False
    terminal: Optional[str] = None
    for _ in range(max_steps):
        action = agent.act(obs, task, history)
        steps += 1
        logs = getattr(agent, "_last_step_logs", None)
        if isinstance(logs, dict):
            cost += float(logs.get("cost_usd", logs.get("cost", 0.0)) or 0.0)
        next_obs, done, _info = env.act(action)
        history.append((obs, action))
        obs = next_obs
        if action.type == "done":
            reported = True
            terminal = "success"
            break
        if action.type == "error":
            terminal = "error"
            break
        if done:
            terminal = terminal or "done"
            break
    return PolicyOutcome(
        reported_success=reported,
        num_steps=steps,
        cost_usd=cost,
        terminal_outcome=terminal,
    )


__all__ = [
    "Policy",
    "PolicyOutcome",
    "MetaMetricsRow",
    "run_meta",
    "run_meta_suite",
    "make_replay_policy",
    "make_hybrid_policy",
    "make_agent_policy",
]
