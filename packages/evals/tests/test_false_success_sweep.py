"""Regression tests for the "failure rendered as a successful empty result" class.

Every test here pins a site where an operation that COULD NOT RUN used to
produce a value indistinguishable from an operation that ran and found nothing.
In an evaluation repo the consequence is not a crash but a wrong published
number -- a backend that is unreachable, scored 0%, and reported as a
legitimate 0%.

Each test fails against the pre-fix implementation of its site.
"""

from __future__ import annotations

import json
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openadapt_types import BenchmarkAction, BenchmarkObservation, BenchmarkTask

from openadapt_evals.adapters.base import (
    BenchmarkResult,
    EvaluationUnavailableError,
)
from openadapt_evals.adapters.rl_env import ResetConfig, RLEnvironment
from openadapt_evals.harness.inspect_export import to_inspect_eval_log
from openadapt_evals.harness.runner import MetaMetricsRow, run_meta


def _task(task_id: str = "t1") -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id, instruction="do it", domain="test", raw_config={},
    )


# ---------------------------------------------------------------------------
# RLEnvironment.evaluate -- an unreachable VM is not a measured 0%
# ---------------------------------------------------------------------------


def _adapter(result: BenchmarkResult) -> MagicMock:
    adapter = MagicMock()
    adapter.list_tasks.return_value = [
        _task()
    ]
    adapter.load_task.return_value = _task()
    adapter.reset.return_value = BenchmarkObservation(screenshot=b"png")
    adapter.observe.return_value = BenchmarkObservation(screenshot=b"png")
    adapter.step.return_value = (BenchmarkObservation(screenshot=b"png"), False, {})
    adapter.evaluate.return_value = result
    return adapter


class TestRLEnvironmentEvaluate:
    def test_infrastructure_failure_raises_instead_of_returning_zero(self) -> None:
        """WAALiveAdapter marks infra failures; flattening to float lost it."""
        adapter = _adapter(
            BenchmarkResult(
                task_id="t1",
                success=False,
                score=0.0,
                error_type="infrastructure",
                reason="evaluate timed out after 3 retries",
            )
        )
        env = RLEnvironment(adapter)
        env.reset(ResetConfig(task_id="t1"))

        with pytest.raises(EvaluationUnavailableError) as excinfo:
            env.evaluate()

        assert excinfo.value.error_type == "infrastructure"
        assert "was not scored" in str(excinfo.value)

    def test_genuine_zero_is_still_a_measurement(self) -> None:
        """The fix must not turn real agent failures into exceptions."""
        adapter = _adapter(
            BenchmarkResult(task_id="t1", success=False, score=0.0, error_type=None)
        )
        env = RLEnvironment(adapter)
        env.reset(ResetConfig(task_id="t1"))

        assert env.evaluate() == 0.0

    @pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1])
    def test_invalid_score_is_not_a_reward(self, score: float) -> None:
        adapter = _adapter(
            BenchmarkResult(
                task_id="t1", success=False, score=score, error_type=None
            )
        )
        env = RLEnvironment(adapter)
        env.reset(ResetConfig(task_id="t1"))
        env.step(BenchmarkAction(type="click", x=1, y=1))
        env.trajectory[-1].reward = -1.0

        with pytest.raises(EvaluationUnavailableError, match="score"):
            env.evaluate()

        assert env.trajectory[-1].reward == -1.0

    def test_any_error_type_is_not_flattened_to_a_score(self) -> None:
        adapter = _adapter(
            BenchmarkResult(
                task_id="t1",
                success=False,
                score=0.0,
                error_type="agent",
                reason="agent action could not be evaluated",
            )
        )
        env = RLEnvironment(adapter)
        env.reset(ResetConfig(task_id="t1"))

        with pytest.raises(EvaluationUnavailableError) as excinfo:
            env.evaluate()

        assert excinfo.value.error_type == "agent"

    def test_evaluate_result_exposes_the_tristate(self) -> None:
        adapter = _adapter(
            BenchmarkResult(
                task_id="t1", success=False, score=0.0, error_type="infrastructure"
            )
        )
        env = RLEnvironment(adapter)
        env.reset(ResetConfig(task_id="t1"))

        result = env.evaluate_result()
        assert result.error_type == "infrastructure"

    def test_unmeasured_score_is_not_backfilled_as_a_reward(self) -> None:
        """An infra 0.0 must never become a GRPO terminal reward."""
        adapter = _adapter(
            BenchmarkResult(
                task_id="t1", success=False, score=0.0, error_type="infrastructure"
            )
        )
        env = RLEnvironment(adapter)
        env.reset(ResetConfig(task_id="t1"))
        env.step(BenchmarkAction(type="click", x=1, y=1))
        env.trajectory[-1].reward = -1.0  # sentinel

        env.evaluate_result()

        assert env.trajectory[-1].reward == -1.0, (
            "an unscored evaluation overwrote the trajectory reward"
        )

    def test_per_step_eval_records_error_type_not_a_zero_score(self) -> None:
        adapter = _adapter(
            BenchmarkResult(
                task_id="t1", success=False, score=0.0, error_type="infrastructure"
            )
        )
        env = RLEnvironment(adapter, evaluate_every_step=True)
        env.reset(ResetConfig(task_id="t1"))
        step = env.step(BenchmarkAction(type="click", x=1, y=1))

        assert "evaluation_score" not in step.info
        assert step.info["evaluation_error_type"] == "infrastructure"


# ---------------------------------------------------------------------------
# harness.run_meta -- a crashed verifier said nothing, not "failed"
# ---------------------------------------------------------------------------


class _Env:
    name = "testenv"

    def __init__(self, verify_exc: Exception | None = None) -> None:
        self._verify_exc = verify_exc

    def reset(self, task):  # noqa: ANN001
        return SimpleNamespace(screenshot=b"")

    def verify(self, task):  # noqa: ANN001
        if self._verify_exc is not None:
            raise self._verify_exc
        return SimpleNamespace(success=True, score=1.0, details={"source": "oracle"})


def _policy(env, task, obs):  # noqa: ANN001
    from openadapt_evals.harness.runner import PolicyOutcome

    return PolicyOutcome(reported_success=True, num_steps=1)


class TestRunMetaVerifier:
    def test_verifier_crash_is_unscored_not_a_failed_task(self) -> None:
        task = _task()
        row = run_meta(_Env(RuntimeError("oracle unreachable")), task, _policy)

        assert row.replay_success is None, (
            "a verifier that crashed was recorded as a task the replay failed"
        )
        assert row.scored is False
        assert row.verifier_score is None
        assert "oracle unreachable" in (row.verifier_error or "")

    def test_verify_error_survives_a_policy_error(self) -> None:
        """`error = error or ...` used to discard the verify failure entirely."""

        def _failing_policy(env, task, obs):  # noqa: ANN001
            raise RuntimeError("policy blew up")

        task = _task()
        row = run_meta(_Env(RuntimeError("oracle unreachable")), task, _failing_policy)

        assert "policy blew up" in (row.error or "")
        assert "oracle unreachable" in (row.error or "")

    def test_healthy_verify_still_scores(self) -> None:
        task = _task()
        row = run_meta(_Env(), task, _policy)

        assert row.replay_success is True
        assert row.scored is True
        assert row.verifier_error is None


class TestInspectExportAccuracy:
    def _row(self, replay_success):  # noqa: ANN001, ANN202
        return MetaMetricsRow(
            env="e",
            task_id=f"t{replay_success}",
            mode="m",
            replay_success=replay_success,
            structural_rung_rate=0.0,
            model_calls=0,
            effect_verdict=None,
            wall_ms=1.0,
            cost_usd=0.0,
        )

    def test_unscored_rows_leave_the_accuracy_denominator(self) -> None:
        rows = [self._row(True), self._row(False), self._row(None)]
        doc = to_inspect_eval_log(rows)

        metric = doc["results"]["scores"][0]["metrics"]["accuracy"]
        # 1 of 2 SCORED, not 1 of 3.
        assert metric["value"] == pytest.approx(0.5)
        assert metric["scored_samples"] == 2
        assert metric["unscored_samples"] == 1
        assert doc["results"]["completed_samples"] == 2

    def test_a_run_that_scored_nothing_is_not_a_zero_accuracy_success(self) -> None:
        doc = to_inspect_eval_log([self._row(None), self._row(None)])

        assert doc["results"]["scores"][0]["metrics"]["accuracy"]["value"] is None
        assert doc["status"] == "error"


# ---------------------------------------------------------------------------
# Azure orchestrator -- a stub must not fabricate a 0% benchmark run
# ---------------------------------------------------------------------------


def test_azure_worker_results_stub_refuses_to_fabricate_scores() -> None:
    from openadapt_evals.benchmarks.azure import (
        AzureWAAOrchestrator,
        WorkerState,
    )

    orchestrator = AzureWAAOrchestrator.__new__(AzureWAAOrchestrator)
    worker = WorkerState(worker_id=0, compute_name="w0", assigned_tasks=["a", "b"])

    with pytest.raises(NotImplementedError) as excinfo:
        orchestrator._fetch_worker_results(worker)

    assert "fabricate" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Azure ML health checks -- "could not look" is not "unhealthy"
# ---------------------------------------------------------------------------


class TestHealthChecker:
    def _checker(self):  # noqa: ANN202
        from openadapt_evals.benchmarks.health_checker import ContainerHealthChecker

        ml_client = MagicMock()
        ml_client.client.jobs.get.return_value = SimpleNamespace(status="Running")
        return ContainerHealthChecker(ml_client)

    def test_log_stub_raises_instead_of_returning_an_empty_log(self) -> None:
        from openadapt_evals.benchmarks.health_checker import JobLogsUnavailableError

        with pytest.raises(JobLogsUnavailableError):
            self._checker()._get_job_logs("job-1")

    def test_unreadable_logs_give_an_unknown_verdict_not_unhealthy(self) -> None:
        result = self._checker().check_container_running("job-1")

        assert result.healthy is None, (
            "an unreadable log was reported as a container health failure"
        )
        assert result.known is False

    def test_stuck_detector_does_not_cancel_on_an_unknown_verdict(self) -> None:
        from openadapt_evals.benchmarks.health_checker import StuckJobDetector

        ml_client = MagicMock()
        ml_client.client.jobs.get.return_value = SimpleNamespace(status="Running")
        detector = StuckJobDetector(ml_client)

        is_stuck, message = detector.check_and_handle_stuck_job(
            "job-1", auto_cancel=True,
        )

        assert is_stuck is False
        assert "NOT checked" in message
        ml_client.client.jobs.cancel.assert_not_called()


# ---------------------------------------------------------------------------
# Resource tracker -- "could not ask Azure" must not read as "$0 running"
# ---------------------------------------------------------------------------


class TestResourceTracker:
    def test_az_failure_raises_rather_than_returning_no_vms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openadapt_evals.infrastructure import resource_tracker

        monkeypatch.setattr(
            resource_tracker.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="az login"),
        )

        with pytest.raises(resource_tracker.AzureQueryFailed):
            resource_tracker.get_azure_vms()

    def test_report_says_unknown_not_all_deallocated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openadapt_evals.infrastructure import resource_tracker

        monkeypatch.setattr(
            resource_tracker.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(
                returncode=1, stdout="", stderr="az: command not found"
            ),
        )
        monkeypatch.setattr(resource_tracker, "get_paused_pool", lambda: None)

        status = resource_tracker.check_resources()
        assert status["query_failures"], "a failed az query left no trace"

        written: dict[str, str] = {}
        monkeypatch.setattr(
            resource_tracker.Path,
            "write_text",
            lambda self, text, *a, **k: written.setdefault("body", text),
        )
        resource_tracker.update_resources_file(status)

        body = written["body"]
        assert "All Azure resources are deallocated or stopped." not in body
        assert "UNKNOWN" in body

    def test_healthy_query_still_reports_no_running_resources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openadapt_evals.infrastructure import resource_tracker

        monkeypatch.setattr(
            resource_tracker.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(
                returncode=0, stdout=json.dumps([]), stderr=""
            ),
        )
        monkeypatch.setattr(resource_tracker, "get_paused_pool", lambda: None)

        status = resource_tracker.check_resources()
        assert status["query_failures"] == []


# ---------------------------------------------------------------------------
# AWS auto-shutdown -- a "success" boolean derived from a checked outcome
# ---------------------------------------------------------------------------


def test_set_auto_shutdown_reports_failure_when_the_command_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openadapt_evals.infrastructure import aws_vm, azure_vm

    manager = aws_vm.AWSVMManager.__new__(aws_vm.AWSVMManager)
    monkeypatch.setattr(
        aws_vm.AWSVMManager, "ssh_username", property(lambda self: "ubuntu"),
    )
    monkeypatch.setattr(
        aws_vm.AWSVMManager, "get_vm_ip", lambda self, name: "1.2.3.4",
    )
    monkeypatch.setattr(
        azure_vm,
        "ssh_run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="sudo: a terminal is required"
        ),
    )

    assert manager.set_auto_shutdown("vm-1", hours=4) is False, (
        "the cost safety net reported itself armed without checking"
    )


def test_set_auto_shutdown_reports_success_when_the_command_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openadapt_evals.infrastructure import aws_vm, azure_vm

    manager = aws_vm.AWSVMManager.__new__(aws_vm.AWSVMManager)
    monkeypatch.setattr(
        aws_vm.AWSVMManager, "ssh_username", property(lambda self: "ubuntu"),
    )
    monkeypatch.setattr(
        aws_vm.AWSVMManager, "get_vm_ip", lambda self, name: "1.2.3.4",
    )
    monkeypatch.setattr(
        azure_vm,
        "ssh_run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Shutdown scheduled", stderr=""
        ),
    )

    assert manager.set_auto_shutdown("vm-1", hours=4) is True


# ---------------------------------------------------------------------------
# Parallels ground-truth verifiers
# ---------------------------------------------------------------------------


class _FakeVM:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self._stdout = stdout
        self._returncode = returncode

    def exec_ps(self, cmd):  # noqa: ANN001
        return SimpleNamespace(
            stdout=self._stdout, returncode=self._returncode, stderr="boom"
        )

    exec_cmd = exec_ps


class _TasklistVM:
    """A VM whose `tasklist` behaves like the real one.

    Windows ANDs multiple ``/FI`` filters, so a command asking for
    ``IMAGENAME eq CalculatorApp.exe`` AND ``IMAGENAME eq Calculator.exe`` can
    never match a process. A fake that echoes fixed stdout regardless of the
    command hides that -- which is exactly why the original bug survived its
    own test.
    """

    def __init__(self, running: str) -> None:
        self.running = running
        self.commands: list[str] = []

    def exec_cmd(self, cmd):  # noqa: ANN001
        self.commands.append(cmd)
        wanted = re.findall(r"IMAGENAME eq ([^\"]+)", cmd)
        if len(wanted) == 1 and wanted[0] == self.running:
            out = (
                "Image Name                     PID Session Name\n"
                f"{self.running}                4242 Console\n"
            )
        else:
            out = "INFO: No tasks are running which match the specified criteria."
        return SimpleNamespace(stdout=out, returncode=0, stderr="")

    exec_ps = exec_cmd


class TestParallelsVerifiers:
    def test_calculator_check_can_actually_pass(self) -> None:
        """`tasklist` ANDs /FI filters, so the old command could never match."""
        from openadapt_evals.flow.parallels_env import verify_calculator_open

        vm = _TasklistVM(running="CalculatorApp.exe")
        ok, _ = verify_calculator_open(vm)

        assert ok is True, (
            "verify_calculator_open cannot return True against a real "
            "tasklist, so this task's verified success rate is pinned at 0%"
        )
        # Each probe must carry exactly one IMAGENAME filter.
        for cmd in vm.commands:
            assert len(re.findall(r"IMAGENAME eq", cmd)) == 1

    def test_calculator_check_still_reports_a_genuine_absence(self) -> None:
        from openadapt_evals.flow.parallels_env import verify_calculator_open

        ok, _ = verify_calculator_open(_TasklistVM(running="notepad.exe"))
        assert ok is False

    def test_dead_exec_channel_is_unscored_not_a_failed_task(self) -> None:
        from openadapt_evals.flow.parallels_env import (
            verify_calculator_open,
            verify_notepad_file,
        )

        vm = _FakeVM(stdout="", returncode=255)
        assert verify_notepad_file(vm)[0] is None
        assert verify_calculator_open(vm)[0] is None

    def test_runner_records_an_unscored_verdict_as_unscored(self, tmp_path) -> None:  # noqa: ANN001
        """`bool(verified)` turned the None sentinel into a measured failure."""
        from openadapt_evals.flow.replay_runner import (
            FlowTask,
            aggregate_replay_metrics,
            run_demonstrate_then_replay,
        )

        bundle = tmp_path / "bundle"
        bundle.mkdir()

        def replay_fn(bundle_dir, server_url, run_dir, params):  # noqa: ANN001
            return SimpleNamespace(
                success=True,
                terminal_outcome="success",
                rung_counts={"primary": 1},
                heal_count=0,
                model_calls=0,
                est_model_cost_usd=0.0,
                total_ms=1.0,
                results=[object()],
            )

        def evaluator(task_id):  # noqa: ANN001
            return None, "exec channel dead (exit 255)"

        metrics = run_demonstrate_then_replay(
            [FlowTask("t1", bundle_dir=bundle)],
            "http://localhost:5001",
            tmp_path / "runs",
            replay_fn=replay_fn,
            evaluator=evaluator,
        )

        assert metrics[0].waa_verified_success is None, (
            "a verifier that could not run was recorded as a failed task"
        )
        agg = aggregate_replay_metrics(metrics)
        assert agg["waa_verified_num_scored"] == 0
        assert agg["waa_verified_success_rate"] is None

    def test_unscored_verdict_leaves_the_success_rate_denominator(self) -> None:
        from openadapt_evals.flow.replay_runner import (
            PerTaskReplayMetrics,
            aggregate_replay_metrics,
        )

        def _m(task_id: str, verified):  # noqa: ANN001, ANN202
            return PerTaskReplayMetrics(
                task_id=task_id,
                bundle_dir=None,
                waa_verified_success=verified,
                replay_reported_success=False,
                structural_rung_counts={},
                structural_rung_fire_total=0,
                rung_fire_rate=0.0,
                model_calls=0,
                est_model_cost_usd=0.0,
                wall_clock_s=0.0,
                heal_count=0,
                halted=False,
                terminal_outcome="success",
                num_steps=1,
            )

        metrics = [_m("a", True), _m("b", None)]
        agg = aggregate_replay_metrics(metrics)
        assert agg["waa_verified_success_rate"] == pytest.approx(1.0)
        assert agg["waa_verified_num_scored"] == 1


# ---------------------------------------------------------------------------
# WAA /evaluate endpoint -- an unreachable VM is not a 0% task
# ---------------------------------------------------------------------------


class TestEvaluateEndpointNotScored:
    def test_missing_getter_is_not_scored(self) -> None:
        from openadapt_evals.server.evaluate_endpoint import (
            EvaluationNotRunError,
            get_actual_value,
        )

        with pytest.raises(EvaluationNotRunError):
            get_actual_value(
                {"result": {"type": "totally_unknown_thing"}},
                env=MagicMock(),
                getters=SimpleNamespace(),
            )

    def test_getter_that_raises_is_infrastructure_not_a_failed_task(self) -> None:
        from openadapt_evals.server.evaluate_endpoint import (
            EvaluationNotRunError,
            get_actual_value,
        )

        def _boom(env, spec):  # noqa: ANN001
            raise ConnectionError("172.30.0.2 refused")

        with pytest.raises(EvaluationNotRunError) as excinfo:
            get_actual_value(
                {"result": {"type": "thing"}},
                env=MagicMock(),
                getters=SimpleNamespace(get_thing=_boom),
            )
        assert excinfo.value.error_type == "infrastructure"

    def test_unknown_metric_is_not_silently_swapped_for_exact_match(self) -> None:
        from openadapt_evals.server.evaluate_endpoint import (
            EvaluationNotRunError,
            run_metric,
        )

        metrics = SimpleNamespace(exact_match=lambda a, b: 1.0)
        with pytest.raises(EvaluationNotRunError):
            run_metric("check_csv_content", "a", "a", {}, metrics)

    def test_evaluate_task_state_marks_the_row_unscored(self) -> None:
        from openadapt_evals.server import evaluate_endpoint

        def _boom(env, spec):  # noqa: ANN001
            raise ConnectionError("172.30.0.2 refused")

        getters = SimpleNamespace(get_thing=_boom)
        metrics = SimpleNamespace(exact_match=lambda a, b: 1.0)

        # Inject the evaluators so the loader is not consulted.
        evaluate_endpoint._getters_module = getters
        evaluate_endpoint._metrics_module = metrics
        try:
            result = evaluate_endpoint.evaluate_task_state(
                {
                    "evaluator": {
                        "result": {"type": "thing"},
                        "expected": {"value": "x"},
                    }
                },
                env=MagicMock(),
            )
        finally:
            evaluate_endpoint._getters_module = None
            evaluate_endpoint._metrics_module = None

        assert result["scored"] is False
        assert result["error_type"] == "infrastructure"


def test_live_adapter_does_not_accept_an_unscored_200_as_a_result() -> None:
    """A 200 body carrying `scored: false` must not become a measured 0.0."""
    from openadapt_evals.adapters.waa.live import WAALiveAdapter

    adapter = WAALiveAdapter.__new__(WAALiveAdapter)
    adapter._step_count = 3

    task = _task()
    result = adapter._result_from_evaluate_response(
        task,
        {
            "success": False,
            "score": 0.0,
            "scored": False,
            "error_type": "infrastructure",
            "reason": "getter could not run on the VM",
        },
    )
    assert result.error_type == "infrastructure"

    measured = adapter._result_from_evaluate_response(
        task, {"success": False, "score": 0.0, "scored": True, "error_type": None},
    )
    assert measured.error_type is None


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"success": 1, "score": 1.0},
        {"success": True, "score": True},
        {"success": False, "score": float("nan")},
        {"success": False, "score": float("inf")},
        {"success": False, "score": -0.1},
        {"success": True, "score": 1.1},
        {"success": True, "score": 0.9},
        {"success": False, "score": 1.0},
        {"success": False, "score": 0.0, "scored": "false"},
        {"success": False, "score": 0.0, "error_type": "agent"},
        {
            "success": False,
            "score": 0.0,
            "scored": True,
            "error_type": "infrastructure",
        },
        {
            "success": False,
            "score": 0.5,
            "scored": False,
            "error_type": "evaluation",
        },
    ],
)
def test_live_adapter_rejects_malformed_evaluate_responses(body: object) -> None:
    from openadapt_evals.adapters.waa.live import WAALiveAdapter

    adapter = WAALiveAdapter.__new__(WAALiveAdapter)
    adapter._step_count = 2

    result = adapter._result_from_evaluate_response(_task(), body)

    assert result.success is False
    assert result.score == 0.0
    assert result.error_type == "evaluation"
    assert result.reason and result.reason.startswith("Malformed evaluation response:")


def test_live_adapter_accepts_canonical_partial_and_complete_measurements() -> None:
    from openadapt_evals.adapters.waa.live import WAALiveAdapter

    adapter = WAALiveAdapter.__new__(WAALiveAdapter)
    adapter._step_count = 2
    task = _task()

    partial = adapter._result_from_evaluate_response(
        task,
        {"success": False, "score": 0.5, "scored": True, "error_type": None},
    )
    complete = adapter._result_from_evaluate_response(
        task,
        {"success": True, "score": 1.0},
    )

    assert (partial.success, partial.score, partial.error_type) == (False, 0.5, None)
    assert (complete.success, complete.score, complete.error_type) == (True, 1.0, None)
