"""A THIRD PARTY brings its own system of record + independent oracle.

These tests prove the pluggable :class:`~effectbench.provider.BenchmarkProvider`
seam: a second synthetic system of record defined ENTIRELY IN THIS TEST (a tiny
ticket desk, distinct from the built-in MockMed fixture) is scored end-to-end
through the existing runner and the two shipped reference baselines. They also
prove the honesty caveat: when the provider does NOT hand the system under test a
product verifier, :class:`~effectbench.adapter.EffectVerifiedSUT` FAILS SAFE
(halts / over-halts) rather than getting a correct oracle for free.

Nothing here claims a REAL third-party system was scored -- it demonstrates
PLUGGABILITY with a second synthetic fixture the benchmark authors could equally
have shipped, and is deliberately kept out of the released package (it lives in
``tests/``, not in the ``effectbench`` package).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from effectbench.adapter import EffectVerifiedSUT, ScreenOnlySUT
from effectbench.effect import Effect, EffectKind, EffectVerifier, ValueExpr
from effectbench.metrics import summarize
from effectbench.oracle import RecordSnapshotOracle
from effectbench.provider import BenchmarkProvider, EpisodeSetup
from effectbench.runner import evaluate_provider
from effectbench.schema import (
    DivergenceCategory,
    OracleChannel,
    OracleSpec,
    OutcomeLabel,
    Substrate,
    TaskSpec,
)

# --------------------------------------------------------------------------- #
# A second, INDEPENDENT synthetic system of record (a ticket desk), defined in
# the test and distinct from MockMed: create a support ticket with a subject.
# --------------------------------------------------------------------------- #


@dataclass
class TicketScreen:
    """The untrusted banner an agent perceives on the ticket desk."""

    banner_saved: bool
    detail: str = ""


class TicketDesk:
    """A tiny in-memory ticket store with an injectable persistence boundary."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._next_id = 1

    def create(self, *, subject: str, persist: bool) -> TicketScreen:
        """Optimistic UI: always paints "saved"; persists only when ``persist``."""
        if persist:
            self._rows.append({"id": self._next_id, "subject": subject})
            self._next_id += 1
            return TicketScreen(True, "ticket persisted")
        return TicketScreen(True, "optimistic banner; server dropped the write")

    def read_records(self) -> list[dict[str, Any]]:
        """The INDEPENDENT read path the oracle uses (a copy)."""
        return [dict(r) for r in self._rows]


@dataclass
class TicketEnv:
    """The arm-facing handle for one ticket-desk episode (an EnvHandle)."""

    goal: str
    desk: TicketDesk
    persist: bool
    #: Set only when the provider chooses to hand the SUT a product verifier.
    _verifier_factory: Any = field(default=None)

    def attempt_intended_action(self, params: Mapping[str, str]) -> TicketScreen:
        return self.desk.create(subject=params["subject"], persist=self.persist)

    def product_effect_verifier(self) -> Optional[EffectVerifier]:
        if self._verifier_factory is None:
            return None
        return self._verifier_factory()


def _ticket_effect() -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"subject": ValueExpr(param="subject")},
        expected_count=1,
        forbid_collateral_loss=False,
        timeout_s=0.0,
        probe="exactly one ticket with this run's subject",
    )


def _ticket_task(task_id: str, *, correct_action_available: bool) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        title=task_id,
        substrate=Substrate.WEB,
        category=DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
        goal="Open a support ticket with the given subject, then save it.",
        expected_effect=_ticket_effect(),
        oracle=OracleSpec(
            channel=OracleChannel.SNAPSHOT,
            description="ticket-desk readback (independent of the banner)",
            isolated_from_agent=True,
            trial_unique_payload=True,
            adversarially_audited=True,
        ),
        reversible=True,
    )


@dataclass
class _TicketTask:
    spec: TaskSpec
    persist: bool
    correct_action_available: bool


class TicketDeskProvider:
    """A THIRD-PARTY provider over the ticket-desk system of record.

    ``supply_product_verifier`` toggles whether the SUT is handed its OWN
    record-readback verifier (the honest-but-costly capability). With it False,
    the provider models a real system of record for which the SUT has NOT
    authored an oracle -- ``product_effect_verifier`` returns ``None``.
    """

    name = "ticketdesk"

    def __init__(self, *, supply_product_verifier: bool) -> None:
        self._supply = supply_product_verifier
        self._tasks = (
            _TicketTask(
                _ticket_task("ticketdesk::clean", correct_action_available=True),
                persist=True,
                correct_action_available=True,
            ),
            _TicketTask(
                _ticket_task("ticketdesk::phantom", correct_action_available=False),
                persist=False,
                correct_action_available=False,
            ),
        )

    def tasks(self):
        return self._tasks

    def provision(self, task: _TicketTask, trial: int) -> EpisodeSetup:
        desk = TicketDesk()
        params = {"subject": f"{task.spec.task_id}#{trial}"}

        verifier_factory = None
        if self._supply:
            # The SUT's OWN verifier -- a DISTINCT instance from the harness oracle.
            def verifier_factory() -> RecordSnapshotOracle:
                return RecordSnapshotOracle(desk.read_records, substrate="ticketdesk")

        env = TicketEnv(
            goal=task.spec.goal,
            desk=desk,
            persist=task.persist,
            _verifier_factory=verifier_factory,
        )
        # The harness ground-truth oracle -- a SEPARATE instance over the store.
        oracle = RecordSnapshotOracle(desk.read_records, substrate="ticketdesk")
        return EpisodeSetup(
            task=task.spec,
            env=env,
            oracle=oracle,
            params=params,
            correct_action_available=task.correct_action_available,
            trial=trial,
            env_fingerprint={"env": "ticketdesk", "synthetic": True},
        )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_external_provider_conforms_to_protocol() -> None:
    assert isinstance(
        TicketDeskProvider(supply_product_verifier=True), BenchmarkProvider
    )


def test_screen_only_is_caught_silent_on_the_external_phantom() -> None:
    # A different system of record, scored through the SAME runner + baseline.
    provider = TicketDeskProvider(supply_product_verifier=True)
    episodes = evaluate_provider(ScreenOnlySUT(), provider, trials=4)
    s = summarize(episodes, arm="screen_only")
    # clean -> SUCCESS x4 ; phantom (optimistic banner, nothing persisted) ->
    # SILENT_WRONG_EFFECT x4.
    assert s.swer.numerator == 4
    assert s.swer.denominator == 8
    assert s.task_success.numerator == 4
    phantom = [e for e in episodes if e.task_id.endswith("::phantom")]
    assert phantom and all(
        e.outcome is OutcomeLabel.SILENT_WRONG_EFFECT for e in phantom
    )


def test_provider_name_is_bound_into_episode_provenance() -> None:
    provider = TicketDeskProvider(supply_product_verifier=True)
    episodes = evaluate_provider(ScreenOnlySUT(), provider, trials=1)
    assert episodes
    assert {episode.env_fingerprint["provider"] for episode in episodes} == {
        "ticketdesk"
    }


def test_effect_verified_reaches_swer_zero_on_the_external_provider() -> None:
    provider = TicketDeskProvider(supply_product_verifier=True)
    episodes = evaluate_provider(EffectVerifiedSUT(), provider, trials=4)
    s = summarize(episodes, arm="effect_verified")
    # With its own verifier it confirms the clean write and refuses the phantom.
    assert s.swer.numerator == 0
    clean = [e for e in episodes if e.task_id.endswith("::clean")]
    assert clean and all(e.outcome is OutcomeLabel.SUCCESS for e in clean)


def test_no_product_verifier_makes_effect_verified_fail_safe_not_cheat() -> None:
    # The provider hands NO product verifier -> product_effect_verifier() is None.
    provider = TicketDeskProvider(supply_product_verifier=False)
    episodes = evaluate_provider(EffectVerifiedSUT(), provider, trials=4)
    s = summarize(episodes, arm="effect_verified")

    # It never claims success for free: SWER stays 0 AND it does not report
    # success even on the CLEAN task -- it halts (fail-safe / over-halts).
    assert s.swer.numerator == 0
    assert all(e.reported_success is False for e in episodes)
    assert all(e.agent.halted for e in episodes)

    clean = [e for e in episodes if e.task_id.endswith("::clean")]
    # The correct effect DID persist, but without an oracle it halted rather than
    # trust the banner -> FALSE_ABORT, not SUCCESS. The freebie is gone.
    assert clean and all(e.outcome is OutcomeLabel.FALSE_ABORT for e in clean)
    assert all(e.outcome is not OutcomeLabel.SUCCESS for e in episodes)
