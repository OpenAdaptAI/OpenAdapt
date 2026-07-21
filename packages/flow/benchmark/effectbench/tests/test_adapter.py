"""A third-party SystemUnderTest plugs in through one method; the oracle scores
it independently and the harness never leaks the oracle to the SUT."""

from __future__ import annotations

from typing import Mapping

from effectbench import evaluate, summarize
from effectbench.adapter import EnvHandle, SystemUnderTest
from effectbench.fixtures.mockmed import ScreenObservation
from effectbench.schema import AgentReport, OutcomeLabel, TaskSpec


class AlwaysSuccessSUT:
    """A minimal third-party adapter: perform the action, always claim success.

    This is the shape of an external integration -- one method, no access to the
    oracle. It must be caught SILENT the moment the record is wrong.
    """

    name = "always_success"

    def run_task(
        self, task: TaskSpec, env: EnvHandle, *, params: Mapping[str, str]
    ) -> AgentReport:
        env.attempt_intended_action(params)
        return AgentReport(reported_success=True, message="always claims success")


def test_custom_sut_conforms_to_protocol() -> None:
    assert isinstance(AlwaysSuccessSUT(), SystemUnderTest)


def test_always_success_sut_is_caught_silent_on_faults() -> None:
    episodes = evaluate(AlwaysSuccessSUT(), trials=3)
    s = summarize(episodes, arm="always_success")
    # An always-success agent is caught SILENT on every fault whose record is
    # wrong or empty; it is a true success only where the record actually landed
    # correct: the ok + idempotent controls AND timeout (a committed write the
    # screen errored on). Three tasks x 3 trials = 9 effect-verified successes,
    # the remaining six faults x 3 = 18 silent wrong-effects.
    assert s.swer.numerator == 18
    assert s.task_success.numerator == 9


def test_oracle_is_not_reachable_from_the_env() -> None:
    # The env exposes only the action + perception surface and the SUT's OWN
    # optional verifier -- never the harness oracle's read path.
    surface = set(dir(EnvHandle))
    assert "attempt_intended_action" in surface
    assert "product_effect_verifier" in surface
    assert not any("oracle" in name for name in surface)


def test_effect_success_is_effect_verified_not_reported() -> None:
    episodes = evaluate(AlwaysSuccessSUT(), trials=1)
    for e in episodes:
        if e.task_id.endswith("::partial"):
            # The screen said saved, but the note was dropped -> silent wrong.
            assert e.reported_success is True
            assert e.outcome is OutcomeLabel.SILENT_WRONG_EFFECT
            assert e.is_effect_success is False
