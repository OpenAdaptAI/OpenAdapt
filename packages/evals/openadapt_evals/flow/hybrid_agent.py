"""Hybrid-as-agent adapter: compiled replay first, computer-use agent fallback
on a detected halt.

:class:`HybridFlowAgent` implements the ``BenchmarkAgent`` interface so the
existing WAA runner (``evaluate_agent_on_benchmark``) can drive it exactly like
any other agent -- which makes it **directly comparable to a pure agent
baseline on the same tasks, scored by the same WAA verifier**.

Behaviour (mirrors openadapt-flow's hybrid benchmark, arm D):

- On the first ``act`` of an episode it runs the compiled bundle end-to-end via
  :class:`openadapt_flow.backends.windows_backend.WindowsBackend` against the
  WAA in-guest server. This is the compiled arm: ~0 model calls, $0.
- If the replay completes, it returns ``done`` (WAA's verifier / the runner's
  done-gate confirms real success -- never self-report).
- If the replay **halts**, it hands the SAME live VM to the wrapped
  computer-use agent for the remaining steps, but only if the shared
  :class:`~openadapt_evals.flow.cost.SpendLedger` allows a paid run. Otherwise
  the halt is surfaced as an error with the skip reason disclosed.

Heavy imports (``openadapt_flow``) are lazy and the replay step is injectable,
so this is unit-testable with a fake replay + a scripted base agent -- no VM,
no ``flow`` extra, no $.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Optional

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.base import BenchmarkAgent
from openadapt_evals.flow.cost import CostGuardConfig, SpendLedger
from openadapt_evals.flow.replay_runner import ReplayFn, _default_replay_fn

logger = logging.getLogger(__name__)


def _step_cost_from_logs(logs: Optional[dict]) -> tuple[float, int]:
    """Best-effort (cost_usd, tokens) from an agent's ``_last_step_logs``."""
    if not isinstance(logs, dict):
        return 0.0, 0
    cost = float(logs.get("cost_usd", logs.get("cost", 0.0)) or 0.0)
    tokens = int(
        logs.get("total_tokens")
        or (int(logs.get("input_tokens", 0) or 0) + int(logs.get("output_tokens", 0) or 0))
        or 0
    )
    return cost, tokens


class HybridFlowAgent(BenchmarkAgent):
    """Compiled-replay-first, agent-fallback-on-halt, as a ``BenchmarkAgent``.

    Args:
        base_agent: The computer-use agent used only on a compiled halt (the
            paid fallback arm) -- e.g. a ``PlannerGrounderAgent`` or
            ``ClaudeComputerUseAgent``. The pure-agent baseline is this same
            agent run alone on the same tasks.
        bundle_dir: Compiled openadapt-flow bundle for the task.
        server_url: WAA in-guest Flask server URL (the compiled arm drives it
            directly -- the same server the WAALiveAdapter uses).
        ledger: Shared paid-spend guardrail. Created from ``guard_config`` when
            omitted.
        guard_config: Guardrails used to build a ledger when ``ledger`` is None.
        replay_fn: Override the compiled replay (defaults to a real
            WindowsBackend replay; inject a fake for tests).
        params: Parameter substitutions forwarded to the replay.
    """

    def __init__(
        self,
        base_agent: BenchmarkAgent,
        bundle_dir: Path | str,
        server_url: str,
        *,
        ledger: Optional[SpendLedger] = None,
        guard_config: Optional[CostGuardConfig] = None,
        replay_fn: Optional[ReplayFn] = None,
        params: Optional[dict[str, str]] = None,
    ) -> None:
        self.base_agent = base_agent
        self.bundle_dir = Path(bundle_dir)
        self.server_url = server_url
        self.ledger = ledger or SpendLedger(guard_config)
        self.replay_fn: ReplayFn = replay_fn or _default_replay_fn
        self.params = params or {}
        self._last_step_logs: Optional[dict] = None
        self._reset_episode_state()

    def _reset_episode_state(self) -> None:
        self._replay_done = False
        self._fallback_mode = False
        self._episode_recorded = True  # nothing pending yet
        self._fallback_cost_usd = 0.0
        self._fallback_tokens = 0
        self._fallback_steps = 0
        self.metrics: dict[str, Any] = {}

    def reset(self) -> None:
        """Reset for a new episode; record the previous episode's paid spend."""
        self.finalize_episode()
        self.base_agent.reset()
        self._last_step_logs = None
        self._reset_episode_state()

    def finalize_episode(self) -> None:
        """Record the just-finished episode's fallback spend to the ledger once."""
        if getattr(self, "_episode_recorded", True):
            return
        error = self.metrics.get("fallback_error")
        self.ledger.record(
            self._fallback_cost_usd, tokens=self._fallback_tokens, error=error
        )
        self._episode_recorded = True

    # -- BenchmarkAgent -----------------------------------------------------

    def act(
        self,
        observation: BenchmarkObservation,
        task: BenchmarkTask,
        history: Optional[list[tuple[BenchmarkObservation, BenchmarkAction]]] = None,
    ) -> BenchmarkAction:
        if not self._replay_done:
            return self._run_compiled_then_maybe_fallback(observation, task, history)
        if self._fallback_mode:
            return self._fallback_step(observation, task, history)
        # Compiled arm already declared done; nothing more to do.
        return BenchmarkAction(type="done")

    # -- internals ----------------------------------------------------------

    def _run_compiled_then_maybe_fallback(
        self, observation, task, history
    ) -> BenchmarkAction:
        self._replay_done = True
        self._episode_recorded = False  # an episode is now in progress
        run_dir = Path(tempfile.mkdtemp(prefix=f"hybrid_{task.task_id}_"))
        reported_success = False
        halted = True
        terminal = None
        try:
            report = self.replay_fn(self.bundle_dir, self.server_url, run_dir, self.params)
            reported_success = bool(getattr(report, "success", False))
            terminal = getattr(report, "terminal_outcome", None)
            halted = terminal == "halt" or (not reported_success and terminal != "success")
            self.metrics.update(
                {
                    "compiled_reported_success": reported_success,
                    "compiled_terminal_outcome": terminal,
                    "compiled_model_calls": int(getattr(report, "model_calls", 0) or 0),
                    "compiled_heal_count": int(getattr(report, "heal_count", 0) or 0),
                    "compiled_rung_counts": dict(getattr(report, "rung_counts", {}) or {}),
                    "compiled_est_cost_usd": float(
                        getattr(report, "est_model_cost_usd", 0.0) or 0.0
                    ),
                }
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("compiled replay raised for %s: %s", task.task_id, e)
            self.metrics["compiled_error"] = str(e)
            reported_success = False
            halted = True

        if reported_success and not halted:
            self.metrics["fallback_used"] = False
            return BenchmarkAction(type="done")

        # Compiled arm halted -> consider the paid fallback under the ledger.
        if not self.ledger.can_start():
            reason = self.ledger.blocked_reason()
            self.metrics.update({"fallback_used": False, "fallback_blocked_reason": reason})
            return BenchmarkAction(
                type="error",
                raw_action={
                    "error_type": "agent",
                    "reason": f"compiled replay halted; paid fallback blocked: {reason}",
                },
            )

        self._fallback_mode = True
        self.metrics.update({"fallback_used": True, "halt_terminal_outcome": terminal})
        # The observation handed to this first act() predates the replay's VM
        # mutations (the replay drove the WAA server directly, bypassing the
        # adapter). Prime a fresh post-replay screenshot with a no-op WAIT so the
        # runner fetches current state before the paid agent's first real step.
        return BenchmarkAction(type="wait")

    def _fallback_step(self, observation, task, history) -> BenchmarkAction:
        # Per-task token cap: stop the fallback if it blows the budget.
        if self.ledger.token_cap_exceeded(self._fallback_tokens):
            self.metrics["fallback_error"] = (
                f"per-task token cap exceeded ({self._fallback_tokens} > "
                f"{self.ledger.config.per_task_tokens})"
            )
            return BenchmarkAction(
                type="error",
                raw_action={
                    "error_type": "agent",
                    "reason": self.metrics["fallback_error"],
                },
            )

        action = self.base_agent.act(observation, task, history)
        self._fallback_steps += 1
        logs = getattr(self.base_agent, "_last_step_logs", None)
        self._last_step_logs = logs
        cost, tokens = _step_cost_from_logs(logs)
        self._fallback_cost_usd += cost
        self._fallback_tokens += tokens
        self.metrics.update(
            {
                "fallback_steps": self._fallback_steps,
                "fallback_cost_usd": round(self._fallback_cost_usd, 5),
                "fallback_tokens": self._fallback_tokens,
            }
        )
        return action
