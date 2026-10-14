"""Tests for the Parallels local-VM environment (fully mocked; no prlctl, no VM).

Snapshot-safety is asserted structurally: the fake VM raises if anything ever
tries to stop or delete it, and the session must snapshot before running and
revert after.
"""

import sys
from types import SimpleNamespace

import pytest

from openadapt_evals.flow.parallels_env import (
    PARALLELS_ENV_VAR,
    ParallelsConfig,
    ParallelsSession,
    ParallelsTask,
    builtin_tasks,
    parallels_enabled,
    run_parallels_replay,
    verify_calculator_open,
    verify_notepad_file,
)


class FakeParallelsVM:
    """Records lifecycle calls; explodes if anyone stops or deletes the VM."""

    def __init__(self, exec_stdout=""):
        self.calls = []
        self._exec_stdout = exec_stdout
        self.reverted_to = None

    def ensure_running(self, *, settle_s=6.0):
        self.calls.append("ensure_running")

    def snapshot(self, name, description=""):
        self.calls.append(f"snapshot:{name}")
        return "snap-123"

    def launch_agent(self, *, port, host, token, wait_s=25.0, host_ip=None):
        self.calls.append(f"launch_agent:{host}:{port}")
        self.token = token
        return f"http://guest:{port}"

    def revert(self, snapshot_id):
        self.calls.append(f"revert:{snapshot_id}")
        self.reverted_to = snapshot_id

    def exec_ps(self, cmd):
        return SimpleNamespace(stdout=self._exec_stdout, returncode=0, stderr="")

    def exec_cmd(self, cmd):
        return SimpleNamespace(stdout=self._exec_stdout, returncode=0, stderr="")

    # Snapshot-safety: these must NEVER be called by the harness.
    def stop(self, *a, **k):
        raise AssertionError("harness must never stop the user's VM")

    def delete(self, *a, **k):
        raise AssertionError("harness must never delete the user's VM")


def _report(success=True, terminal="success"):
    return SimpleNamespace(
        success=success, terminal_outcome=terminal, rung_counts={"primary": 3},
        heal_count=0, model_calls=0, est_model_cost_usd=0.0, total_ms=10.0,
        results=[object()] * 3,
    )


def test_opt_in_gate_default_off(monkeypatch):
    monkeypatch.delenv(PARALLELS_ENV_VAR, raising=False)
    assert parallels_enabled() is False
    # Session refuses to enter when not opted in.
    with pytest.raises(RuntimeError, match="opt-in"):
        with ParallelsSession(ParallelsConfig(), vm=FakeParallelsVM()):
            pass


def test_opt_in_via_env(monkeypatch):
    monkeypatch.setenv(PARALLELS_ENV_VAR, "1")
    assert parallels_enabled() is True


def test_opt_in_via_config():
    assert parallels_enabled(ParallelsConfig(enabled=True)) is True


def test_session_is_snapshot_safe():
    vm = FakeParallelsVM()
    cfg = ParallelsConfig(enabled=True, snapshot_name="clean")
    with ParallelsSession(cfg, vm=vm) as sess:
        assert sess.server_url == "http://guest:5000"
    # snapshot BEFORE launch, revert on exit, never stop/delete.
    assert vm.calls == [
        "ensure_running",
        "snapshot:clean",
        "launch_agent:0.0.0.0:5000",
        "revert:snap-123",
    ]
    assert vm.reverted_to == "snap-123"


def test_session_reverts_even_on_error():
    vm = FakeParallelsVM()
    cfg = ParallelsConfig(enabled=True)
    with pytest.raises(ValueError):
        with ParallelsSession(cfg, vm=vm):
            raise ValueError("boom during run")
    assert any(c.startswith("revert:") for c in vm.calls)  # clean reset still happened


def test_run_parallels_replay_scores_with_our_verifier(tmp_path):
    vm = FakeParallelsVM(exec_stdout="openadapt-flow replay ok")
    bundle = tmp_path / "notepad_write"
    bundle.mkdir()
    task = ParallelsTask(
        task_id="notepad_write",
        instruction="write a file",
        verify=verify_notepad_file,
        bundle_dir=bundle,
    )
    metrics, summary = run_parallels_replay(
        [task], ParallelsConfig(enabled=True), vm=vm,
        replay_fn=lambda *a: _report(success=True),
        run_root=tmp_path / "runs",
    )
    m = metrics[0]
    assert m.replay_reported_success is True
    assert m.waa_verified_success is True       # OUR ground-truth verifier passed
    assert m.model_calls == 0                   # replay is model-free / $0
    assert summary["waa_verified_success"] == 1
    assert vm.reverted_to == "snap-123"         # snapshot-safe reset happened


def test_notepad_verifier_reads_in_guest_state():
    ok, reason = verify_notepad_file(FakeParallelsVM(exec_stdout="openadapt-flow replay ok"))
    assert ok is True
    bad, _ = verify_notepad_file(FakeParallelsVM(exec_stdout="MISSING"))
    assert bad is False


def test_calculator_verifier():
    ok, _ = verify_calculator_open(FakeParallelsVM(exec_stdout="CalculatorApp.exe  1234 Console"))
    assert ok is True
    bad, _ = verify_calculator_open(FakeParallelsVM(exec_stdout="INFO: No tasks."))
    assert bad is False


def test_builtin_tasks_flag_missing_bundles(tmp_path):
    tasks = builtin_tasks(bundles_dir=tmp_path)  # empty dir -> no bundles
    assert {t.task_id for t in tasks} == {"notepad_write", "calculator_open"}
    assert all(t.bundle_dir is None for t in tasks)


def test_no_openadapt_flow_import(tmp_path):
    vm = FakeParallelsVM(exec_stdout="openadapt-flow replay ok")
    b = tmp_path / "notepad_write"
    b.mkdir()
    run_parallels_replay(
        [ParallelsTask("notepad_write", "x", verify_notepad_file, bundle_dir=b)],
        ParallelsConfig(enabled=True), vm=vm,
        replay_fn=lambda *a: _report(),
        run_root=tmp_path / "r",
    )
    assert "openadapt_flow" not in sys.modules
