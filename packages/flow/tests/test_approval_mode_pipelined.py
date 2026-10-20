"""problems_only with pipelined read-back (docs/THROUGHPUT.md, approval modes).

An item's record check may run while the next item starts. Its approval
decision then waits for the settled verdict: deciding on the held report
would read a save still being checked as an uncertain result and switch
problems_only off. Meanwhile the next item's gate doesn't read the held item
as interrupted. After a restart nothing is held, so an item that never
finished still reads as interrupted.

The record system is scripted; the values are made up.
"""

from __future__ import annotations

import threading
from typing import Any

from openadapt_flow.approval_mode.gate import ApprovalGate
from openadapt_flow.approval_mode.model import ItemDecisionKind
from openadapt_flow.run_gate import build_runtime_authorization
from openadapt_flow.runtime.readback_pipeline import ReadbackPipeline
from tests._approval_mode_support import (
    ENVIRONMENT_DIGEST,
    RUNNER_ID,
    USER_KEY,
    good_item,
)
from tests.test_approval_mode_replayer import Item
from tests.test_compatibility_matrix import RestRecordSystem
from tests.test_execution_profiles import _KEY, Replayer, _ReadyVision
from tests.test_replayer import FakeBackend


class HeldCheck(RestRecordSystem):
    """A REST-read record system whose first verdict waits for a release."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.calls = 0

    def verify(self, effect: Any, before: Any, context: Any = None) -> Any:
        self.calls += 1
        if self.calls == 1:
            assert self.release.wait(30)
        return super().verify(effect, before, context)


def _gate(item: Item) -> ApprovalGate:
    return ApprovalGate(
        "problems_only",
        record=item.record,
        log=item.log,
        pinned_status_keys=item.gate._pinned_keys,
        fetch_status=item.cloud,
        environment_digest=lambda: ENVIRONMENT_DIGEST,
        runner_id=RUNNER_ID,
        signed_in_user=item.user,
        dedicated_user_key=USER_KEY,
        item=good_item(),
    )


def _run(item: Item, gate: ApprovalGate, name: str, pipeline: ReadbackPipeline):
    return Replayer(
        FakeBackend(),
        vision=_ReadyVision(),
        effect_verifier=item.verifier,
        governed_authorization=build_runtime_authorization(
            item.workflow, item.gate_result
        ),
        durable=True,
        checkpoint_key=_KEY,
        require_settled=True,
        approval_gate=gate,
        readback_pipeline=pipeline,
    ).run(
        item.workflow,
        bundle_dir=item.bundle,
        run_dir=item.tmp_path / name,
    )


def test_a_held_check_keeps_the_switch_on_for_the_next_item(tmp_path):
    verifier = HeldCheck()
    item = Item(tmp_path, verifier=verifier)
    pipeline = ReadbackPipeline()
    try:
        first = _run(item, _gate(item), "first", pipeline)
        held = pipeline.take()
        assert held is not None, pipeline.inline_reason
        # The first item's decision waits for its record check.
        assert first.approval is None
        second = _run(item, _gate(item), "second", pipeline)
        # The next item isn't stopped by the held one: it reaches its own
        # save, and its decision in turn waits for its record check.
        assert second.approval is None
        save = item.workflow.steps[0].id
        assert any(r.step_id == save and r.ok for r in second.results), [
            (r.step_id, r.error) for r in second.results
        ]
        verifier.release.set()
        held.settled.result(timeout=30)
        later = pipeline.take()
        assert later is not None
        later.settled.result(timeout=30)
    finally:
        verifier.release.set()
        pipeline.close()

    for report in (first, second):
        assert report.approval is not None
        assert report.approval.decision is ItemDecisionKind.NO_PERSON, report.approval
    assert not item.log.state().is_off


def test_after_a_restart_a_held_item_reads_as_interrupted(tmp_path):
    verifier = HeldCheck()
    item = Item(tmp_path, verifier=verifier)
    pipeline = ReadbackPipeline()
    try:
        _run(item, _gate(item), "first", pipeline)
        assert pipeline.take() is not None
        # The process stops: nothing is held any more.
        from openadapt_flow.approval_mode import gate as gate_module

        with gate_module._READBACK_IN_FLIGHT_LOCK:
            gate_module._READBACK_IN_FLIGHT.clear()
        second = _run(item, _gate(item), "second", ReadbackPipeline())
    finally:
        verifier.release.set()
        pipeline.close()

    assert second.approval is not None
    assert second.approval.decision is ItemDecisionKind.APPROVAL_REQUIRED
    assert item.log.state().is_off


def test_a_settle_that_fails_still_decides_the_item(tmp_path, monkeypatch):
    from openadapt_flow.approval_mode import gate as gate_module

    def broken(self, entry, error):
        raise RuntimeError("evidence copy failed")

    monkeypatch.setattr(Replayer, "_copy_pipelined_evidence", broken)
    item = Item(tmp_path, verifier=RestRecordSystem())
    pipeline = ReadbackPipeline()
    try:
        first = _run(item, _gate(item), "first", pipeline)
        held = pipeline.take()
        assert held is not None, pipeline.inline_reason
        try:
            held.settled.result(timeout=30)
        except RuntimeError:
            pass
    finally:
        pipeline.close()

    assert first.approval is not None
    assert first.approval.decision is not ItemDecisionKind.NO_PERSON
    assert first.approval.needs_person
    with gate_module._READBACK_IN_FLIGHT_LOCK:
        assert not gate_module._READBACK_IN_FLIGHT
