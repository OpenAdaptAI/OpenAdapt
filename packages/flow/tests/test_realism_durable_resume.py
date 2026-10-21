"""Resume after a save that landed but could not be confirmed.

The fakes in ``test_durable_runtime`` refute a step whose write never
happened. A real system of record also fails the other way: the click lands,
the row is written, and the read-back times out (a slow EMR, a dropped
connection). The pause then looks ordinary, the operator approves the default
"resume" option, and resume presses the save again.

Here the backend writes a row on every press of the save key, and the record
check times out once. Resume must either refuse without an explicit
uncertain-retry authorization, or leave exactly one row.
See docs/audits/REALISM_AUDIT.md.
"""

from __future__ import annotations

import pytest

from openadapt_flow.runtime.durable import ApprovalRequired, CheckpointStore, resume
from openadapt_flow.runtime.effects import Effect, EffectState, EffectVerdict, Verdict
from openadapt_flow.runtime.replayer import Replayer
from tests.test_durable_runtime import (
    _approval,
    _dirs,
    _three_step_workflow,
    _vision_ok,
)
from tests.test_replayer import FakeBackend

_STEP_FOR_KEY = {"A": "s0", "B": "s1", "C": "s2"}


class _Store:
    """A system of record: one row per delivered press."""

    def __init__(self) -> None:
        self.rows: list[str] = []

    def records(self) -> list[dict]:
        return [{"step": _STEP_FOR_KEY.get(key, key)} for key in self.rows]


class _WritingBackend(FakeBackend):
    def __init__(self, store: _Store) -> None:
        super().__init__()
        self.store = store

    def key(self, key, *args, **kwargs):  # type: ignore[override]
        self.store.rows.append(str(key))
        return super().key(key, *args, **kwargs)

    def press(self, key, *args, **kwargs):  # type: ignore[override]
        self.store.rows.append(str(key))
        return super().press(key, *args, **kwargs)


class _SlowReadback:
    """Reads the store, but the first read-back of s2 times out."""

    substrate = "fake"

    def __init__(self, store: _Store) -> None:
        self.store = store
        self.timeouts = {"s2": 1}

    def capture_pre_state(self, context=None) -> EffectState:
        return EffectState(
            substrate=self.substrate, reachable=True, records=self.store.records()
        )

    def verify(self, expected: Effect, before: EffectState, context=None):
        step = dict(expected.match).get("step")
        if self.timeouts.get(step, 0) > 0:
            self.timeouts[step] -= 1
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason="the system of record did not answer before the timeout",
                unavailable=True,
            )
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=expected.kind,
            substrate=self.substrate,
            reason="the record is present",
        )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "REALISM_AUDIT durable-1: a delivered save whose read-back timed out "
        "pauses without delivery_uncertainty, so resume re-delivers it"
    ),
)
def test_resume_never_repeats_a_save_whose_readback_timed_out(tmp_path) -> None:
    store = _Store()
    workflow = _three_step_workflow(with_effects=True)
    bundle, run_dir = _dirs(tmp_path)
    workflow.save(bundle)
    verifier = _SlowReadback(store)

    first = Replayer(
        _WritingBackend(store),
        vision=_vision_ok(),
        effect_verifier=verifier,
        durable=True,
        poll_interval_s=0.01,
    ).run(workflow, params={"who": "alice"}, bundle_dir=bundle, run_dir=run_dir)
    assert first.success is False
    assert store.rows.count("C") == 1  # the save landed before the timeout
    assert CheckpointStore(run_dir).read_pending() is not None

    resumed_backend = _WritingBackend(store)
    try:
        resume(
            run_dir,
            Replayer(
                resumed_backend,
                vision=_vision_ok(),
                effect_verifier=verifier,
                poll_interval_s=0.01,
            ),
            approval=_approval(bundle),
        )
    except ApprovalRequired:
        return  # refused: the operator must reconcile first
    assert store.rows.count("C") == 1, store.rows
