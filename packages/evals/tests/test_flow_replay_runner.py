"""Tests for the demonstrate-then-replay runner (fully mocked; no flow, no VM)."""

import sys
from pathlib import Path
from types import SimpleNamespace

from openadapt_evals.flow.replay_runner import (
    FlowTask,
    aggregate_replay_metrics,
    run_demonstrate_then_replay,
)


def _fake_report(success=True, terminal="success", rungs=None, model_calls=0,
                 heal=0, n_steps=5):
    return SimpleNamespace(
        success=success,
        terminal_outcome=terminal,
        rung_counts=rungs or {"primary": n_steps},
        heal_count=heal,
        model_calls=model_calls,
        est_model_cost_usd=0.0,
        total_ms=1234.0,
        results=[object()] * n_steps,
    )


def test_healthy_replay_scored_by_waa_verifier(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    tasks = [FlowTask("waa_task_1", bundle_dir=bundle)]

    def replay_fn(bundle_dir, server_url, run_dir, params):
        assert Path(bundle_dir) == bundle
        assert server_url == "http://localhost:5001"
        return _fake_report(success=True, terminal="success", model_calls=0)

    def evaluator(task_id):
        assert task_id == "waa_task_1"
        return True, None

    metrics = run_demonstrate_then_replay(
        tasks, "http://localhost:5001", tmp_path / "runs",
        replay_fn=replay_fn, evaluator=evaluator,
    )
    m = metrics[0]
    assert m.waa_verified_success is True      # ground truth, not self-report
    assert m.replay_reported_success is True
    assert m.model_calls == 0                  # ~0 model calls on healthy replay
    assert m.halted is False
    assert m.rung_fire_rate > 0


def test_halted_replay_marked_and_verifier_can_disagree(tmp_path):
    bundle = tmp_path / "b"
    bundle.mkdir()
    tasks = [FlowTask("t", bundle_dir=bundle)]

    def replay_fn(*a):
        return _fake_report(success=False, terminal="halt")

    def evaluator(task_id):
        return False, "state check failed"

    m = run_demonstrate_then_replay(
        tasks, "http://x", tmp_path / "r", replay_fn=replay_fn, evaluator=evaluator
    )[0]
    assert m.halted is True
    assert m.waa_verified_success is False
    assert m.error == "state check failed"


def test_missing_bundle_is_flagged_not_faked(tmp_path):
    tasks = [FlowTask("no_bundle", bundle_dir=tmp_path / "does_not_exist")]
    calls = []

    def replay_fn(*a):
        calls.append(a)
        return _fake_report()

    m = run_demonstrate_then_replay(
        tasks, "http://x", tmp_path / "r", replay_fn=replay_fn
    )[0]
    assert not calls                      # replay never invoked for a missing bundle
    assert "no compiled bundle" in m.error
    assert m.waa_verified_success is None


def test_replay_exception_is_isolated(tmp_path):
    b1 = tmp_path / "b1"
    b1.mkdir()
    b2 = tmp_path / "b2"
    b2.mkdir()

    def replay_fn(bundle_dir, *a):
        if Path(bundle_dir) == b1:
            raise RuntimeError("backend blew up")
        return _fake_report()

    metrics = run_demonstrate_then_replay(
        [FlowTask("bad", bundle_dir=b1), FlowTask("good", bundle_dir=b2)],
        "http://x", tmp_path / "r", replay_fn=replay_fn,
    )
    assert metrics[0].error and "backend blew up" in metrics[0].error
    assert metrics[0].halted is True
    assert metrics[1].replay_reported_success is True   # second task still ran


def test_aggregate(tmp_path):
    b = tmp_path / "b"
    b.mkdir()

    def replay_fn(*a):
        return _fake_report(success=True, model_calls=0)

    metrics = run_demonstrate_then_replay(
        [FlowTask(f"t{i}", bundle_dir=b) for i in range(3)],
        "http://x", tmp_path / "r", replay_fn=replay_fn,
        evaluator=lambda tid: (True, None),
    )
    agg = aggregate_replay_metrics(metrics)
    assert agg["num_tasks"] == 3
    assert agg["waa_verified_success"] == 3
    assert agg["waa_verified_success_rate"] == 1.0
    assert agg["total_model_calls"] == 0
    assert agg["num_halted"] == 0


def test_no_openadapt_flow_import_during_mocked_run(tmp_path):
    b = tmp_path / "b"
    b.mkdir()
    run_demonstrate_then_replay(
        [FlowTask("t", bundle_dir=b)], "http://x", tmp_path / "r",
        replay_fn=lambda *a: _fake_report(),
    )
    assert "openadapt_flow" not in sys.modules
