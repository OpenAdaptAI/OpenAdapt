"""MockMed reference task pack + env factory — the CI-fast dry-run substrate.

These are the reference tasks the multi-baseline runner exercises end-to-end
without Docker or spend: the bundled MockMed transactional-fault surface
(``openadapt_flow.mockmed.fault_server``) turned into
:class:`~openadapt_flow.benchmark.effectbench.schema.TaskSpec` s across the
divergence categories, plus a :func:`mockmed_env_factory` that provisions one
isolated fault server per episode with an INDEPENDENT in-process oracle.

They are the runner's counterpart to
``benchmark/effectbench/reference_fault_model.py``: that module re-expresses the
fault study from KNOWN pre/post states to pin the metrics; this one DRIVES the
live server over real HTTP through both concrete arms, proving the ablation
surfaces silent wrong-effects (green banner, bad record) while the compiler arm
does not (it verifies the effect and refuses / recovers). The consequential-save
contract is the #63 one: ``record_written`` (exactly one target encounter,
at-most-once, no collateral loss) AND ``field_equals`` on the note (a partial
save drops it). The note is a per-trial payload so the oracle checks THIS run's
exact effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from openadapt_flow.benchmark.effectbench.oracle import RecordSnapshotOracle
from openadapt_flow.benchmark.effectbench.runner.compound import (
    CompoundEffectVerifier,
)
from openadapt_flow.benchmark.effectbench.runner.harness import EpisodeEnv
from openadapt_flow.benchmark.effectbench.runner.substrate import (
    MockMedFault,
    MockMedSession,
    mockmed_session,
)
from openadapt_flow.benchmark.effectbench.schema import (
    DivergenceCategory,
    OracleChannel,
    OracleSpec,
    Substrate,
    TaskSpec,
)
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    ValueExpr,
)

#: The recorded target every reference task writes (mirrors the fault matrix).
TARGET: dict[str, str] = {"patient_id": "p1", "type": "Triage"}
#: Idempotency key the recommended-fix (idempotent) scenario carries.
IDEMPOTENCY_KEY = "effectbench-idem-key"
#: How long a verifier polls the system of record per effect. Small so the
#: ABSENT / REFUTED faults (which poll to the deadline) stay CI-fast.
EFFECT_TIMEOUT_S = 0.1

#: The shared natural-language intent — handed IDENTICALLY to every arm (never a
#: step list). Fairness requirement.
GOAL = (
    "Record a Triage encounter for the referred patient p1 with the provided "
    "clinical note, and save it."
)


def make_params(seed: int) -> dict[str, str]:
    """The run's trial-unique payload (a per-trial note is the unique value)."""
    return {
        "patient_id": TARGET["patient_id"],
        "type": TARGET["type"],
        "note": f"EffectBench triage note (trial {seed})",
    }


def _record_effect(*, keyed: bool) -> Effect:
    """The primary ``record_written`` contract: exactly one target encounter,
    at-most-once, no collateral loss (the keyed scenario counts by key)."""
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={k: ValueExpr(literal=v) for k, v in TARGET.items()},
        expected_count=1,
        idempotency_key=ValueExpr(literal=IDEMPOTENCY_KEY) if keyed else None,
        forbid_collateral_loss=True,
        risk="irreversible",
        timeout_s=EFFECT_TIMEOUT_S,
    )


def _note_effect() -> Effect:
    """The ``field_equals`` note read-back (a partial save drops the note).

    ``value`` binds to the run ``note`` param, so each trial's oracle checks the
    exact note THAT trial wrote (trial-unique payload)."""
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={k: ValueExpr(literal=v) for k, v in TARGET.items()},
        field="note",
        value=ValueExpr(param="note"),
        risk="irreversible",
        timeout_s=EFFECT_TIMEOUT_S,
    )


@dataclass(frozen=True)
class _Ref:
    """One reference fault: its ``fault_server`` mode + how ``app.js`` delivers
    the write + whether a correct effect was attainable this episode."""

    mode: str
    category: DivergenceCategory
    delivery: str
    keyed: bool
    seed_concurrent: bool
    correct_action_available: bool
    reversible: bool
    title: str


# The nine reference scenarios (mirrors the fault_model / silent_wrong_action
# suite). ``correct_action_available`` is False for every genuine fault (the
# fault makes a correct effect unattainable) and True for the clean/idempotent
# controls and the recoverable timeout (the row DID commit).
_REFS: tuple[_Ref, ...] = (
    _Ref(
        "ok",
        DivergenceCategory.CONTROL,
        "single",
        False,
        False,
        True,
        True,
        "clean accepted write (control)",
    ),
    _Ref(
        "partial",
        DivergenceCategory.C1_PARTIAL_SAVE,
        "single",
        False,
        False,
        False,
        False,
        "partial save: row persists, note dropped",
    ),
    _Ref(
        "duplicate",
        DivergenceCategory.C2_DUPLICATE_SUBMISSION,
        "double",
        False,
        False,
        False,
        False,
        "duplicate submission: two rows",
    ),
    _Ref(
        "timeout",
        DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
        "single",
        False,
        False,
        True,
        False,
        "committed then timed out (screen shows error)",
    ),
    _Ref(
        "optimistic",
        DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
        "single",
        False,
        False,
        False,
        False,
        "optimistic UI then server reject: phantom",
    ),
    _Ref(
        "session",
        DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
        "single",
        False,
        False,
        False,
        False,
        "session expired: nothing persisted",
    ),
    _Ref(
        "stale",
        DivergenceCategory.C4_STALE_OVERWRITE,
        "single",
        False,
        True,
        False,
        False,
        "stale last-write-wins: concurrent row lost",
    ),
    _Ref(
        "double",
        DivergenceCategory.C5_DOUBLE_DELIVERED_INPUT,
        "double",
        False,
        False,
        False,
        False,
        "double-delivered click: two rows",
    ),
    _Ref(
        "idempotent",
        DivergenceCategory.CONTROL,
        "double",
        True,
        False,
        True,
        True,
        "recommended fix: idempotency key de-dupes (control)",
    ),
)


def _oracle_spec() -> OracleSpec:
    """The independent oracle description shared by every reference task.

    The oracle reads the in-process ``FaultDB`` snapshot directly — a path the
    rendered screen never surfaces and the agent's HTTP write channel is
    separate from. ``refusal_controls`` / ``adversarially_audited`` are False:
    these reference tasks pin the pipeline and metrics, they are not the
    release-eligible authored task pack (that is downstream effort #3)."""
    return OracleSpec(
        channel=OracleChannel.SNAPSHOT,
        description=(
            "In-process MockMed FaultDB snapshot (records list) read directly, "
            "isolated from the agent's HTTP /api/encounter write channel; "
            "compound record_written + note field_equals contract."
        ),
        isolated_from_agent=True,
        trial_unique_payload=True,
        refusal_controls=False,
        adversarially_audited=False,
    )


def _task(ref: _Ref) -> TaskSpec:
    return TaskSpec(
        task_id=f"mockmed::{ref.mode}",
        title=ref.title,
        substrate=Substrate.WEB,
        category=ref.category,
        goal=GOAL,
        expected_effect=_record_effect(keyed=ref.keyed),
        oracle=_oracle_spec(),
        initial_state={
            "fault": ref.mode,
            "delivery": ref.delivery,
            "keyed": ref.keyed,
            "seed_concurrent": ref.seed_concurrent,
            "correct_action_available": ref.correct_action_available,
        },
        reversible=ref.reversible,
        notes=ref.title,
    )


def reference_tasks() -> list[TaskSpec]:
    """The nine MockMed reference tasks the dry-run drives end-to-end."""
    return [_task(r) for r in _REFS]


def _fault_of(task: TaskSpec) -> MockMedFault:
    st: Mapping[str, object] = task.initial_state
    return MockMedFault(
        mode=str(st["fault"]),
        delivery=str(st["delivery"]),
        keyed=bool(st["keyed"]),
        seed_concurrent=bool(st["seed_concurrent"]),
    )


def mockmed_env_factory(task: TaskSpec, seed: int) -> EpisodeEnv:
    """Provision one isolated MockMed episode for ``task`` at ``seed``.

    Brings up a FRESH in-process fault server per episode (maximal isolation),
    resolves the trial-unique params, and wires the compound consequential-save
    contract into BOTH the arm-facing session (for the compiler arm's own
    product verifier, over HTTP) and the INDEPENDENT benchmark oracle (over the
    in-process snapshot) — two different objects and read paths, so an arm can
    never reach the oracle that judges it.

    For a whole matrix, prefer :class:`MockMedEnvProvider`, which serves ONE
    server and resets it per episode (the same reset-per-run isolation the
    ``silent_wrong_action`` benchmark uses, minus the per-episode bring-up cost).
    """
    fault = _fault_of(task)
    params = make_params(seed)
    note_effect = _note_effect().resolve(params)

    prov = mockmed_session(goal=task.goal, fault=fault, extra_effects=[note_effect])
    oracle = CompoundEffectVerifier(
        RecordSnapshotOracle(prov.read_records, substrate="snapshot"),
        extra_effects=[note_effect],
    )
    return EpisodeEnv(
        session=prov.session,
        oracle=oracle,
        params=params,
        correct_action_available=bool(task.initial_state["correct_action_available"]),
        close=prov.close,
        env_fingerprint=prov.fingerprint,
    )


class MockMedEnvProvider:
    """Serve ONE MockMed fault server and reset it per episode (fast + isolated).

    Reset-per-episode is exactly the isolation the ``silent_wrong_action``
    benchmark relies on: :meth:`~openadapt_flow.mockmed.fault_server.FaultDB.reset`
    clears the store (and re-seeds a concurrent-actor row when a fault needs it)
    before each episode, so episodes cannot contaminate one another — without
    paying a ``ThreadingHTTPServer`` bring-up/teardown per episode. Use it as a
    context manager and pass :attr:`factory` to :func:`run_matrix`.

    The arm-facing session still writes over HTTP and the independent oracle
    still reads the in-process snapshot; only the server lifecycle is shared.
    """

    def __init__(self, *, client_abort_s: float = 0.3) -> None:
        from openadapt_flow.mockmed.fault_server import serve

        self._url, self._db, self._stop = serve(port=0)
        self._abort_s = client_abort_s

    def _read_records(self) -> Optional[list[dict[str, object]]]:
        snap = self._db.snapshot()
        recs = snap.get("records")
        return list(recs) if isinstance(recs, list) else None

    def factory(self, task: TaskSpec, seed: int) -> EpisodeEnv:
        fault = _fault_of(task)
        params = make_params(seed)
        note_effect = _note_effect().resolve(params)
        # Reset-per-episode isolation on the shared server.
        self._db.reset(seed_concurrent=fault.seed_concurrent)

        session = MockMedSession(
            base_url=self._url,
            goal=task.goal,
            fault=fault,
            extra_effects=[note_effect],
            client_abort_s=self._abort_s,
        )
        oracle = CompoundEffectVerifier(
            RecordSnapshotOracle(self._read_records, substrate="snapshot"),
            extra_effects=[note_effect],
        )
        return EpisodeEnv(
            session=session,
            oracle=oracle,
            params=params,
            correct_action_available=bool(
                task.initial_state["correct_action_available"]
            ),
            close=lambda: None,  # server is shared; closed by the provider.
            env_fingerprint={
                "substrate": "web",
                "app": "mockmed.fault_server",
                "fault": fault.mode,
                "delivery": fault.delivery,
                "keyed": fault.keyed,
                "seed_concurrent": fault.seed_concurrent,
                "shared_server": True,
            },
        )

    def close(self) -> None:
        self._stop()

    def __enter__(self) -> "MockMedEnvProvider":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = [
    "GOAL",
    "TARGET",
    "make_params",
    "reference_tasks",
    "mockmed_env_factory",
    "MockMedEnvProvider",
]
