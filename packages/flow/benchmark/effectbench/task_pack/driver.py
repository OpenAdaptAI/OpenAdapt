"""Live MockMed driver — run the anchor tasks end-to-end through the oracle.

This is the task pack's own VALIDATION harness, not the multi-baseline runner
adapter (that is a separate effort). It exists to prove the authored MockMed
:class:`~.mockmed_tasks.MockMedTask` specs actually LOAD, DRIVE a real HTTP
persistence boundary, and CLASSIFY correctly through the frozen #178 contract
(``score_episode`` + the independent ``RestRecordVerifier`` oracle) — with no
mocked verdicts.

It depends ONLY on the public EffectBench surface
(:func:`~openadapt_flow.benchmark.effectbench.score_episode`,
:class:`~openadapt_flow.benchmark.effectbench.RestRecordVerifier`,
:class:`~openadapt_flow.benchmark.effectbench.AgentReport`) and the MockMed
fault server. It does not reimplement any real baseline "arm"; the two arms
here are the two SCORING conditions the benchmark contrasts:

- ``screen_only`` — the deceptive witness: the agent believes the rendered
  banner (``MockMedDrive.screen_success``). This is the screen-only-verification
  ablation.
- ``effect_verify`` — the agent consults its OWN independent effect verifier
  (a separate ``RestRecordVerifier`` reading the same system of record, NOT the
  benchmark oracle object handed to ``score_episode``) and halts unless the
  write is CONFIRMED.

Only ``reported_success`` differs between the arms; the independent benchmark
oracle is identical, so an injected fault classifies as ``silent_wrong_effect``
under ``screen_only`` and ``success`` / ``safe_halt`` / ``false_abort`` under
``effect_verify`` — the headline the pack demonstrates.
"""

from __future__ import annotations

import contextlib
import hashlib
from typing import Callable, Iterator, Optional

import requests

from benchmark.effectbench.task_pack._authoring import (
    PARAM_NOTE,
    PARAM_RECORD_KEY,
    PARAM_TARGET,
)
from benchmark.effectbench.task_pack.mockmed_tasks import (
    MOCKMED_TASKS,
    MockMedDrive,
    MockMedTask,
)
from openadapt_flow.benchmark.effectbench import (
    AgentReport,
    EpisodeRecord,
    RestRecordVerifier,
    score_episode,
)
from openadapt_flow.mockmed.fault_server import FaultDB, serve

ARMS = ("screen_only", "effect_verify")
# Faults whose realistic manifestation is a repeated delivery (a double-submit
# or an idempotent retry): the driver posts the write twice.
_DOUBLE_POST = {"duplicate", "double", "idempotent"}
_HTTP_TIMEOUT_S = 5.0


@contextlib.contextmanager
def serve_mockmed() -> Iterator[tuple[str, FaultDB]]:
    """Serve MockMed's fault server on an ephemeral loopback port."""
    url, db, stop = serve(port=0)
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def trial_params(task_id: str, trial: int) -> dict[str, str]:
    """Derive a deterministic, TRIAL-UNIQUE payload from (task, trial).

    The note (the consequential free-text) and the idempotency key are unique
    per trial, so the oracle checks THIS run's exact effect and cross-trial
    contamination is detectable (design doc section 3.3).
    """
    tag = hashlib.sha256(f"{task_id}:{trial}".encode()).hexdigest()[:12]
    return {
        PARAM_NOTE: f"triage note {tag}",
        PARAM_RECORD_KEY: f"key-{tag}",
        PARAM_TARGET: f"MRN-{tag}",
    }


def _post_encounter(
    base_url: str,
    *,
    patient_id: str,
    enc_type: str,
    note: str,
    fault: str,
    key: Optional[str],
) -> requests.Response:
    body: dict[str, str] = {"patient_id": patient_id, "type": enc_type, "note": note}
    if key is not None:
        body["key"] = key
    path = "/api/encounter" + (f"?fault={fault}" if fault else "")
    return requests.post(base_url + path, json=body, timeout=_HTTP_TIMEOUT_S)


def _perform_writes(base_url: str, drive: MockMedDrive, params: dict[str, str]) -> None:
    """Replay the agent's write(s) for one trial (the ACTION channel only)."""
    if drive.write is None:  # silent no-op: the click never reaches the boundary
        return
    key = params[PARAM_RECORD_KEY] if drive.fault == "idempotent" else None
    n_posts = 2 if drive.fault in _DOUBLE_POST else 1
    for _ in range(n_posts):
        with contextlib.suppress(requests.RequestException):
            # ``optimistic``/``session`` legitimately return non-2xx; ``timeout``
            # hangs past the client abort. The agent's action does not depend on
            # the response body — the oracle judges the record, not the reply.
            _post_encounter(
                base_url,
                patient_id=drive.write["patient_id"],
                enc_type=drive.write["type"],
                note=params[PARAM_NOTE],
                fault=drive.fault,
                key=key,
            )


def _reset_and_seed(base_url: str, drive: MockMedDrive, params: dict[str, str]) -> None:
    """Environment setup BEFORE the scored action: reset, plant concurrent /
    decoy rows. This is NOT the agent's action channel."""
    requests.post(
        base_url + "/api/reset",
        json={"seed_concurrent": drive.seed_concurrent},
        timeout=_HTTP_TIMEOUT_S,
    )
    for decoy in drive.decoys:
        # Seed a confusable/stale target with its OWN (non-trial) note so it is
        # distinguishable from the agent's trial write.
        _post_encounter(
            base_url,
            patient_id=decoy["patient_id"],
            enc_type=decoy.get("type", "Triage"),
            note="pre-existing decoy encounter",
            fault="",
            key=None,
        )


def _agent_action(
    arm: str,
    base_url: str,
    task: MockMedTask,
    params: dict[str, str],
) -> Callable[[], AgentReport]:
    """Build the arm's ``run_action`` — MUST NOT touch the benchmark oracle."""
    drive = task.drive

    def screen_only() -> AgentReport:
        _perform_writes(base_url, drive, params)
        return AgentReport(
            reported_success=drive.screen_success,
            halted=not drive.screen_success,
            message="believed the rendered banner",
        )

    def effect_verify() -> AgentReport:
        # The agent's OWN verifier — a distinct RestRecordVerifier instance, not
        # the benchmark oracle. It captures its own pre-state, acts, and halts
        # unless the write is CONFIRMED against the system of record.
        own = RestRecordVerifier(
            base_url, records_path="/api/db", records_key="records"
        )
        own_before = own.capture_pre_state()
        _perform_writes(base_url, drive, params)
        resolved = task.spec.expected_effect.resolve(params)
        verdict = own.verify(resolved, own_before)
        return AgentReport(
            reported_success=verdict.confirmed,
            halted=not verdict.confirmed,
            message=f"self-verified effect: {verdict.verdict.value}",
        )

    def governed_refuse() -> AgentReport:
        # Refuses to act on an ambiguous/stale target (writes nothing).
        return AgentReport(
            reported_success=False, halted=True, message="refused ambiguous target"
        )

    return {
        "screen_only": screen_only,
        "effect_verify": effect_verify,
        "governed_refuse": governed_refuse,
    }[arm]


def run_mockmed_episode(
    task: MockMedTask,
    *,
    arm: str,
    trial: int,
    base_url: str,
) -> EpisodeRecord:
    """Run ONE (task x arm x trial) live against the MockMed fault server."""
    params = trial_params(task.spec.task_id, trial)
    _reset_and_seed(base_url, task.drive, params)

    oracle = RestRecordVerifier(base_url, records_path="/api/db", records_key="records")
    return score_episode(
        episode_id=f"{arm}::{task.spec.task_id}::{trial}",
        task_id=task.spec.task_id,
        arm=arm,
        trial=trial,
        substrate=task.spec.substrate,
        category=task.spec.category,
        oracle=oracle,
        expected_effect=task.spec.expected_effect,
        run_action=_agent_action(arm, base_url, task, params),
        correct_action_available=task.correct_action_available,
        params=params,
        seed=trial,
        env_fingerprint={"env": "mockmed", "substrate": "web", "ci_fast": True},
    )


def run_mockmed_pack(
    tasks: tuple[MockMedTask, ...] = MOCKMED_TASKS,
    *,
    arms: tuple[str, ...] = ARMS,
    trials: int = 3,
) -> list[EpisodeRecord]:
    """Run the whole MockMed anchor live under each arm, ``trials`` per task."""
    episodes: list[EpisodeRecord] = []
    with serve_mockmed() as (base_url, _db):
        for task in tasks:
            for arm in arms:
                for trial in range(trials):
                    episodes.append(
                        run_mockmed_episode(
                            task, arm=arm, trial=trial, base_url=base_url
                        )
                    )
    return episodes
