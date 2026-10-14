"""Tests for the hybrid-as-agent adapter (fully mocked; no flow, no VM)."""

import sys
from types import SimpleNamespace

from openadapt_evals.adapters.base import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)
from openadapt_evals.agents.base import BenchmarkAgent
from openadapt_evals.flow.cost import CostGuardConfig, SpendLedger
from openadapt_evals.flow.hybrid_agent import HybridFlowAgent


class ScriptedAgent(BenchmarkAgent):
    """A base computer-use agent that emits a fixed action sequence."""

    def __init__(self, actions, step_cost=0.05, step_tokens=1000):
        self._actions = list(actions)
        self._i = 0
        self._step_cost = step_cost
        self._step_tokens = step_tokens
        self._last_step_logs = None
        self.reset_called = 0

    def act(self, observation, task, history=None):
        self._last_step_logs = {"cost_usd": self._step_cost, "total_tokens": self._step_tokens}
        if self._i < len(self._actions):
            a = self._actions[self._i]
            self._i += 1
            return a
        return BenchmarkAction(type="done")

    def reset(self):
        self.reset_called += 1
        self._i = 0


def _obs():
    return BenchmarkObservation(viewport=(1920, 1080))


def _task():
    return BenchmarkTask(task_id="waa_t", instruction="do it", domain="notepad")


def _report(success, terminal, model_calls=0):
    return SimpleNamespace(
        success=success, terminal_outcome=terminal, model_calls=model_calls,
        heal_count=0, rung_counts={"primary": 3}, est_model_cost_usd=0.0,
        results=[object()] * 3,
    )


def test_healthy_replay_returns_done_without_paying(tmp_path):
    base = ScriptedAgent([])
    agent = HybridFlowAgent(
        base, bundle_dir=tmp_path, server_url="http://x",
        replay_fn=lambda *a: _report(True, "success"),
    )
    action = agent.act(_obs(), _task())
    assert action.type == "done"
    assert agent.metrics["fallback_used"] is False
    assert agent.ledger.spent == 0.0        # compiled arm is free


def test_halt_triggers_agent_fallback(tmp_path):
    click = BenchmarkAction(type="click", x=10, y=10)
    base = ScriptedAgent([click])
    agent = HybridFlowAgent(
        base, bundle_dir=tmp_path, server_url="http://x",
        replay_fn=lambda *a: _report(False, "halt"),
    )
    # First act: replay halts -> a no-op WAIT primes a fresh post-replay obs.
    a1 = agent.act(_obs(), _task())
    assert a1.type == "wait"
    assert agent.metrics["fallback_used"] is True
    # Second act: the base computer-use agent acts on the fresh observation.
    a2 = agent.act(_obs(), _task())
    assert a2 is click
    assert agent._last_step_logs["cost_usd"] == 0.05     # cost surfaced to the runner

    # Ledger records the episode's fallback spend on reset/finalize.
    agent.finalize_episode()
    assert agent.ledger.spent > 0
    assert agent.ledger.tokens_used == 1000


def test_fallback_blocked_when_ledger_cannot_start(tmp_path):
    ledger = SpendLedger(CostGuardConfig(per_run_usd=0.5, total_usd=0.4))  # cap < per-run
    base = ScriptedAgent([BenchmarkAction(type="click", x=1, y=1)])
    agent = HybridFlowAgent(
        base, bundle_dir=tmp_path, server_url="http://x", ledger=ledger,
        replay_fn=lambda *a: _report(False, "halt"),
    )
    action = agent.act(_obs(), _task())
    assert action.type == "error"
    assert "blocked" in action.raw_action["reason"]
    assert agent.metrics["fallback_used"] is False


def test_per_task_token_cap_stops_fallback(tmp_path):
    ledger = SpendLedger(CostGuardConfig(per_task_tokens=1500, total_usd=100))
    base = ScriptedAgent(
        [BenchmarkAction(type="click", x=1, y=1) for _ in range(5)],
        step_tokens=2000,
    )
    agent = HybridFlowAgent(
        base, bundle_dir=tmp_path, server_url="http://x", ledger=ledger,
        replay_fn=lambda *a: _report(False, "halt"),
    )
    prime = agent.act(_obs(), _task())    # WAIT primer (no base step yet)
    assert prime.type == "wait"
    a1 = agent.act(_obs(), _task())       # 2000 tokens used
    assert a1.type == "click"
    a2 = agent.act(_obs(), _task())       # 2000 > 1500 cap -> error
    assert a2.type == "error"
    assert "token cap" in a2.raw_action["reason"]


def test_reset_finalizes_previous_episode_and_resets_base(tmp_path):
    base = ScriptedAgent([BenchmarkAction(type="click", x=1, y=1)])
    agent = HybridFlowAgent(
        base, bundle_dir=tmp_path, server_url="http://x",
        replay_fn=lambda *a: _report(False, "halt"),
    )
    agent.act(_obs(), _task())            # WAIT primer
    agent.act(_obs(), _task())            # real fallback step (spends)
    agent.reset()
    assert base.reset_called == 1
    assert agent.ledger.spent > 0         # previous episode recorded exactly once
    assert agent._fallback_mode is False


def test_no_openadapt_flow_import(tmp_path):
    base = ScriptedAgent([])
    agent = HybridFlowAgent(
        base, bundle_dir=tmp_path, server_url="http://x",
        replay_fn=lambda *a: _report(True, "success"),
    )
    agent.act(_obs(), _task())
    assert "openadapt_flow" not in sys.modules
