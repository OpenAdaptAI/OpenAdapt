"""Agent failures must remain distinct from a model-issued completion."""

from __future__ import annotations

from types import MethodType

from openadapt_types import BenchmarkObservation, BenchmarkTask

from openadapt_evals.agents.api_agent import ApiAgent
from openadapt_evals.agents.base import parse_action_response
from openadapt_evals.agents.baseline_agent import BaselineAgent
from openadapt_evals.agents.policy_agent import PolicyAgent
from openadapt_evals.agents.qwen3vl_agent import parse_qwen_action
from openadapt_evals.agents.smol_agent import parse_smol_action


def _task():
    return BenchmarkTask(task_id="t", instruction="do it", domain="desktop")


def test_parse_failures_are_error_actions():
    assert parse_action_response("not an action").type == "error"
    assert parse_qwen_action("not an action").type == "error"
    assert parse_smol_action("not an action").type == "error"

    agent = ApiAgent.__new__(ApiAgent)
    action = agent._parse_computer_action("computer.unknown()", BenchmarkObservation())
    assert action.type == "error"
    assert action.raw_action["error_type"] == "agent"


def test_api_agent_keeps_terminal_decisions_distinct():
    agent = ApiAgent.__new__(ApiAgent)
    agent.predict = MethodType(
        lambda self, instruction, obs: ("", ["FAIL"], {}, {}), agent
    )
    assert agent.act(BenchmarkObservation(), _task()).type == "error"

    agent.predict = MethodType(
        lambda self, instruction, obs: ("", ["WAIT"], {}, {}), agent
    )
    assert agent.act(BenchmarkObservation(), _task()).type == "wait"

    agent.predict = MethodType(
        lambda self, instruction, obs: ("", ["# parse failed"], {}, {}), agent
    )
    assert agent.act(BenchmarkObservation(), _task()).type == "error"


def test_missing_observation_and_inference_failure_are_errors(monkeypatch):
    baseline = BaselineAgent.__new__(BaselineAgent)
    baseline._step_count = 0
    assert baseline.act(BenchmarkObservation(), _task()).type == "error"

    policy = PolicyAgent()
    monkeypatch.setattr(policy, "_load_model", lambda: None)
    monkeypatch.setattr(
        policy,
        "_run_inference",
        lambda observation, prompt: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert policy.act(BenchmarkObservation(), _task()).type == "error"
