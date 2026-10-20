"""Pipelined read-back under durable and governed runs (docs/THROUGHPUT.md).

A durable run defers its last step's record check, so the check runs while
the next item starts. Before it hands the check off, the run writes the check
into the durable pause: the snapshot taken before the save, the save's result
as sent, and ``delivery_uncertainty``. Then:

- the verdict arrives in this process: a confirmed check writes the save's
  checkpoint and consumes the pause; any other verdict leaves a
  reconciliation pause;
- the process dies first: a resume re-runs only the check and never sends the
  save again.

The record system is a scripted verifier at INDEPENDENT_SYSTEM; the values
are made up.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openadapt_flow import compatibility as matrix
from openadapt_flow.runtime.durable import CheckpointStore, resume
from openadapt_flow.runtime.durable.approval import (
    ApprovalRequired,
    PauseExpired,
    issue_resume_approval,
)
from openadapt_flow.runtime.durable.program_checkpoint import bundle_version
from openadapt_flow.runtime.readback_pipeline import ReadbackPipeline
from tests.test_compatibility_matrix import Run
from tests.test_execution_profiles import _KEY


def _store(run: Run) -> CheckpointStore:
    return CheckpointStore(run.run_dir, key=_KEY)


def _approval(run: Run, **extra):
    store = _store(run)
    manifest, pending = store.read_manifest(), store.read_pending()
    assert manifest is not None and pending is not None
    return issue_resume_approval(
        pending,
        approver="operator@example.com",
        resolution="the process stopped during the record check",
        bundle_version=bundle_version(run.bundle),
        run_id=manifest.run_id,
        workflow_name=manifest.workflow_name,
        run_dir=run.run_dir,
        **extra,
    )


def _run_and_settle(run: Run, pipeline: ReadbackPipeline):
    report = run.run()
    handoff = pipeline.take()
    assert handoff is not None, pipeline.inline_reason
    return report, handoff.settled.result(timeout=30)


@pytest.fixture
def crash(monkeypatch):
    """The process dies once the check is handed off: it never settles."""

    monkeypatch.setattr(ReadbackPipeline, "hand_off", lambda self, entry, settle: None)


@pytest.mark.parametrize("profile", ["durable", "governed", "qualified"])
def test_a_durable_run_defers_its_last_record_check_and_settles_it(tmp_path, profile):
    pipeline = ReadbackPipeline()
    try:
        run = Run(tmp_path, profile, pipeline=pipeline)
        report, settled = _run_and_settle(run, pipeline)
    finally:
        pipeline.close()

    assert settled is report and report.success is True
    assert report.results[-1].effect_verified is True
    if profile in {"governed", "qualified"}:
        assert report.execution_outcome == "VERIFIED"
    store = _store(run)
    # The confirmed check wrote the save's checkpoint and consumed the pause.
    assert store.read_pending() is None
    assert store.checkpoints()[-1].step_id == report.results[-1].step_id
    assert store.checkpoints()[-1].effect_verified is True


def test_the_pause_holds_the_check_until_it_settles(tmp_path, crash):
    pipeline = ReadbackPipeline()
    try:
        run = Run(tmp_path, "governed", pipeline=pipeline)
        report = run.run()
    finally:
        pipeline.close()

    assert report.success is False and report.execution_completed is False
    pending = _store(run).read_pending()
    assert pending is not None and pending.category == "readback_pending"
    assert pending.delivery_uncertainty is not None
    deferred = pending.deferred_readback
    assert deferred is not None and deferred.step_id == report.results[-1].step_id
    assert deferred.result.delivery_attempted is True
    # The save's own checkpoint waits for the verdict.
    assert [c.step_id for c in _store(run).checkpoints()] == [
        r.step_id for r in report.results[:-1]
    ]
    # The deferred check keeps record rows, so it is written only sealed.
    assert all(
        p.name.endswith(".enc") for p in run.run_dir.glob("pending_escalation.json*")
    )


def test_a_crash_mid_window_resumes_the_check_and_never_resends(tmp_path, crash):
    pipeline = ReadbackPipeline()
    try:
        run = Run(tmp_path, "governed", pipeline=pipeline)
        run.run()
    finally:
        pipeline.close()
    posts = list(run.transport.posts)
    reads = run.verifier.reads

    resumed = resume(run.run_dir, run.replayer(), approval=_approval(run), key=_KEY)

    assert resumed.success is True and resumed.execution_outcome == "VERIFIED"
    # The save was sent once; the resume re-ran only its record check.
    assert run.transport.posts == posts and len(posts) <= 1
    assert run.verifier.reads > reads
    store = _store(run)
    assert store.read_pending() is None
    assert store.checkpoints()[-1].effect_verified is True


def test_a_check_that_fails_on_resume_leaves_a_reconciliation_pause(tmp_path, crash):
    pipeline = ReadbackPipeline()
    try:
        run = Run(tmp_path, "governed", pipeline=pipeline)
        run.run()
    finally:
        pipeline.close()
    posts = list(run.transport.posts)
    run.verifier.answer = "mismatch"

    with pytest.raises(ApprovalRequired, match="never sent again"):
        resume(run.run_dir, run.replayer(), approval=_approval(run), key=_KEY)

    pending = _store(run).read_pending()
    assert pending is not None and pending.deferred_readback is None
    assert pending.delivery_uncertainty is not None
    assert run.transport.posts == posts
    # An ordinary resume still can't repeat a save that may have landed.
    run.verifier.answer = "match"
    with pytest.raises(ApprovalRequired, match="may already have actuated"):
        resume(run.run_dir, run.replayer(), approval=_approval(run), key=_KEY)
    assert run.transport.posts == posts


def test_a_check_refuted_in_process_is_never_redone_by_resume(tmp_path):
    pipeline = ReadbackPipeline()
    try:
        run = Run(tmp_path, "governed", pipeline=pipeline)
        run.verifier.answer = "mismatch"
        _report, settled = _run_and_settle(run, pipeline)
    finally:
        pipeline.close()

    assert settled.success is False
    assert settled.transaction_outcome == "RECONCILIATION_REQUIRED"
    pending = _store(run).read_pending()
    assert pending is not None and pending.deferred_readback is None
    assert pending.delivery_uncertainty is not None
    posts = list(run.transport.posts)
    with pytest.raises(ApprovalRequired, match="may already have actuated"):
        resume(run.run_dir, run.replayer(), approval=_approval(run), key=_KEY)
    assert run.transport.posts == posts


def test_an_expired_pause_is_not_resumed(tmp_path, crash):
    pipeline = ReadbackPipeline()
    try:
        run = Run(tmp_path, "governed", pipeline=pipeline)
        run.run()
    finally:
        pipeline.close()
    approval = _approval(run)
    later = datetime.now(timezone.utc) + timedelta(days=30)

    with pytest.raises(PauseExpired):
        resume(run.run_dir, run.replayer(), approval=approval, key=_KEY, now=later)
    assert _store(run).read_pending().deferred_readback is not None


@pytest.mark.parametrize(
    "key, step_index, reason",
    [
        (
            None,
            -1,
            "a durable run without an encrypted checkpoint store verifies inline "
            "(the deferred check keeps record rows)",
        ),
        (
            _KEY,
            0,
            "a durable run defers only its last step's record check; this one "
            "verifies inline",
        ),
    ],
)
def test_a_durable_run_verifies_inline_when_the_pause_cant_hold_the_check(
    tmp_path, key, step_index, reason
):
    pipeline = ReadbackPipeline()
    try:
        run = Run(tmp_path, "durable", pipeline=pipeline)
        replayer = run.replayer()
        replayer.checkpoint_key = key
        replayer._linear_final_step_id = run.workflow.steps[-1].id
        step = run.workflow.steps[step_index]
        assert replayer._may_pipeline_readback(run.verifier, step) is False
        assert pipeline.inline_reason == reason
    finally:
        pipeline.close()


def test_the_matrix_says_durable_runs_pipeline():
    for profile in ("durable", "governed", "qualified"):
        assert matrix.cell("pipelined_readback", profile).status == "works"


def test_the_check_starts_only_once_the_pause_holds_it(tmp_path):
    pipeline = ReadbackPipeline()
    try:
        run = Run(tmp_path, "governed", pipeline=pipeline)
        seen = []
        check = run.verifier.verify

        def verify(*args, **kwargs):
            # A crash from here on must find the check in the durable pause.
            pending = _store(run).read_pending()
            seen.append(pending is not None and pending.deferred_readback is not None)
            return check(*args, **kwargs)

        run.verifier.verify = verify
        _report, _settled = _run_and_settle(run, pipeline)
    finally:
        pipeline.close()

    assert seen and all(seen)
