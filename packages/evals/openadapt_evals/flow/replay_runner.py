"""Demonstrate-then-replay runner: the paradigm-correct WAA eval for a
demonstration compiler.

For each WAA task we:

  (a) obtain ONE demonstration and ``compile`` it into an openadapt-flow bundle
      (offline, done ahead of time -- see ``scripts/record_waa_demos.py`` to
      record and ``python -m openadapt_flow compile`` to compile);
  (b) ``replay`` that bundle via :class:`openadapt_flow.backends.windows_backend.WindowsBackend`
      pointed at the WAA in-guest Flask server (the SAME ``/screenshot`` +
      ``/execute_windows`` contract WAA already exposes -- no adapter layer);
  (c) let **WAA's own task evaluator** score success (NOT the replayer's
      self-report).

Per-task metrics emitted: WAA-verified success, replay self-report, structural
rung fire rate, model calls (~0 on a healthy replay), wall-clock, and any
halt/heal events.

Everything heavy (``openadapt_flow``) is imported lazily inside the default
callables, and both the replay step and the WAA evaluator are injectable, so
the whole runner is unit-testable with mocks -- no VM, no ``flow`` extra, no $.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

# A replay callable: (bundle_dir, server_url, run_dir, params) -> a RunReport-like
# object exposing .success, .rung_counts, .heal_count, .model_calls,
# .est_model_cost_usd, .total_ms, .terminal_outcome, .results.
ReplayFn = Callable[[Path, str, Path, Optional[dict]], Any]

# A WAA evaluator callable: task_id -> (verified_success, reason).
EvaluatorFn = Callable[[str], "tuple[bool, Optional[str]]"]


@dataclass
class FlowTask:
    """One WAA task paired with its compiled openadapt-flow bundle."""

    task_id: str
    bundle_dir: Optional[Path] = None
    params: dict[str, str] = field(default_factory=dict)

    @property
    def has_bundle(self) -> bool:
        return self.bundle_dir is not None and Path(self.bundle_dir).exists()


@dataclass
class PerTaskReplayMetrics:
    """Per-task metrics for one demonstrate-then-replay run."""

    task_id: str
    bundle_dir: Optional[str]
    # Ground truth (WAA's verifier) vs. the replayer's self-report.
    waa_verified_success: Optional[bool]
    replay_reported_success: bool
    # Structural verification rung usage.
    structural_rung_counts: dict[str, int]
    structural_rung_fire_total: int
    rung_fire_rate: float
    # Model usage (should be ~0 on a healthy replay).
    model_calls: int
    est_model_cost_usd: float
    # Timing + resilience.
    wall_clock_s: float
    heal_count: int
    halted: bool
    terminal_outcome: Optional[str]
    num_steps: int
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "bundle_dir": self.bundle_dir,
            "waa_verified_success": self.waa_verified_success,
            "replay_reported_success": self.replay_reported_success,
            "structural_rung_counts": self.structural_rung_counts,
            "structural_rung_fire_total": self.structural_rung_fire_total,
            "rung_fire_rate": round(self.rung_fire_rate, 4),
            "model_calls": self.model_calls,
            "est_model_cost_usd": round(self.est_model_cost_usd, 5),
            "wall_clock_s": round(self.wall_clock_s, 3),
            "heal_count": self.heal_count,
            "halted": self.halted,
            "terminal_outcome": self.terminal_outcome,
            "num_steps": self.num_steps,
            "error": self.error,
        }


def _default_replay_fn(
    bundle_dir: Path, server_url: str, run_dir: Path, params: Optional[dict]
) -> Any:
    """Replay a bundle once via the WindowsBackend against the WAA server.

    Imports openadapt-flow lazily. Requires the ``flow`` extra
    (``pip install 'openadapt-evals[flow]'``) and a reachable WAA server.
    """
    try:
        from openadapt_flow.backends.windows_backend import WindowsBackend
        from openadapt_flow.ir import Workflow
        from openadapt_flow.runtime import Replayer
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "openadapt-flow is required for live replay. Install the extra: "
            "pip install 'openadapt-evals[flow]'"
        ) from e

    workflow = Workflow.load(bundle_dir)
    backend = WindowsBackend(server_url=server_url)
    try:
        return Replayer(backend).run(
            workflow, params=params or {}, bundle_dir=bundle_dir, run_dir=run_dir
        )
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


def _report_metrics(task: FlowTask, report: Any, wall_clock_s: float) -> PerTaskReplayMetrics:
    """Extract per-task structural/heal/model metrics from a RunReport-like object."""
    rung_counts = dict(getattr(report, "rung_counts", {}) or {})
    rung_total = sum(rung_counts.values())
    results = getattr(report, "results", []) or []
    num_steps = len(results)
    terminal = getattr(report, "terminal_outcome", None)
    reported_success = bool(getattr(report, "success", False))
    halted = terminal == "halt" or (not reported_success and terminal != "success")
    return PerTaskReplayMetrics(
        task_id=task.task_id,
        bundle_dir=str(task.bundle_dir) if task.bundle_dir else None,
        waa_verified_success=None,
        replay_reported_success=reported_success,
        structural_rung_counts=rung_counts,
        structural_rung_fire_total=rung_total,
        rung_fire_rate=(rung_total / num_steps if num_steps else 0.0),
        model_calls=int(getattr(report, "model_calls", 0) or 0),
        est_model_cost_usd=float(getattr(report, "est_model_cost_usd", 0.0) or 0.0),
        wall_clock_s=wall_clock_s,
        heal_count=int(getattr(report, "heal_count", 0) or 0),
        halted=halted,
        terminal_outcome=terminal,
        num_steps=num_steps,
    )


def run_demonstrate_then_replay(
    tasks: Sequence[FlowTask],
    server_url: str,
    run_root: Path | str,
    *,
    replay_fn: Optional[ReplayFn] = None,
    evaluator: Optional[EvaluatorFn] = None,
    on_task: Optional[Callable[[PerTaskReplayMetrics], None]] = None,
) -> list[PerTaskReplayMetrics]:
    """Replay each task's compiled bundle and score it with WAA's verifier.

    Args:
        tasks: Tasks paired with their compiled bundles. A task without a
            bundle is recorded as an error (it must be recorded + compiled
            first) and skipped -- never faked.
        server_url: WAA in-guest Flask server URL (e.g. ``http://localhost:5001``).
        run_root: Directory that receives one ``<task_id>/`` replay dir per task.
        replay_fn: Override the replay step (defaults to a real WindowsBackend
            replay; inject a fake for tests).
        evaluator: WAA ground-truth verifier ``task_id -> (success, reason)``.
            When None, ``waa_verified_success`` stays None (replay ran but was
            not independently scored).
        on_task: Optional per-task callback.

    Returns:
        One :class:`PerTaskReplayMetrics` per task, in input order.
    """
    replay_fn = replay_fn or _default_replay_fn
    root = Path(run_root)
    root.mkdir(parents=True, exist_ok=True)
    out: list[PerTaskReplayMetrics] = []

    for task in tasks:
        if not task.has_bundle:
            metrics = PerTaskReplayMetrics(
                task_id=task.task_id,
                bundle_dir=str(task.bundle_dir) if task.bundle_dir else None,
                waa_verified_success=None,
                replay_reported_success=False,
                structural_rung_counts={},
                structural_rung_fire_total=0,
                rung_fire_rate=0.0,
                model_calls=0,
                est_model_cost_usd=0.0,
                wall_clock_s=0.0,
                heal_count=0,
                halted=False,
                terminal_outcome=None,
                num_steps=0,
                error=(
                    "no compiled bundle -- record a demo (scripts/record_waa_demos.py) "
                    "and compile it (python -m openadapt_flow compile) first"
                ),
            )
            out.append(metrics)
            if on_task:
                on_task(metrics)
            continue

        run_dir = root / task.task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        try:
            report = replay_fn(Path(task.bundle_dir), server_url, run_dir, task.params)
            wall = time.monotonic() - start
            metrics = _report_metrics(task, report, wall)
        except Exception as e:  # noqa: BLE001 - one bad task must not abort the run
            logger.exception("replay failed for %s", task.task_id)
            metrics = PerTaskReplayMetrics(
                task_id=task.task_id,
                bundle_dir=str(task.bundle_dir),
                waa_verified_success=None,
                replay_reported_success=False,
                structural_rung_counts={},
                structural_rung_fire_total=0,
                rung_fire_rate=0.0,
                model_calls=0,
                est_model_cost_usd=0.0,
                wall_clock_s=time.monotonic() - start,
                heal_count=0,
                halted=True,
                terminal_outcome="error",
                num_steps=0,
                error=str(e),
            )
            out.append(metrics)
            if on_task:
                on_task(metrics)
            continue

        # WAA's own verifier is the source of truth for success.
        if evaluator is not None:
            try:
                verified, reason = evaluator(task.task_id)
                # `None` means the verifier could not run. Leave
                # waa_verified_success unset so aggregate_replay_metrics
                # excludes this task from the rate's denominator rather than
                # counting an unperformed check as a failure.
                metrics.waa_verified_success = (
                    None if verified is None else bool(verified)
                )
                if reason and not metrics.error:
                    metrics.error = reason if not verified else None
            except Exception as e:  # noqa: BLE001
                logger.warning("WAA evaluator failed for %s: %s", task.task_id, e)
                metrics.error = metrics.error or f"evaluator error: {e}"

        out.append(metrics)
        if on_task:
            on_task(metrics)

    return out


def aggregate_replay_metrics(metrics: Sequence[PerTaskReplayMetrics]) -> dict:
    """Aggregate per-task replay metrics into a summary dict."""
    n = len(metrics)
    if n == 0:
        return {"num_tasks": 0}
    verified = [m for m in metrics if m.waa_verified_success is not None]
    verified_success = sum(1 for m in verified if m.waa_verified_success)
    reported_success = sum(1 for m in metrics if m.replay_reported_success)
    return {
        "num_tasks": n,
        "num_with_bundle": sum(1 for m in metrics if m.bundle_dir is not None),
        "waa_verified_num_scored": len(verified),
        "waa_verified_success": verified_success,
        "waa_verified_success_rate": (
            verified_success / len(verified) if verified else None
        ),
        "replay_reported_success": reported_success,
        "replay_reported_success_rate": reported_success / n,
        "total_model_calls": sum(m.model_calls for m in metrics),
        "total_est_model_cost_usd": round(sum(m.est_model_cost_usd for m in metrics), 5),
        "total_heal_events": sum(m.heal_count for m in metrics),
        "num_halted": sum(1 for m in metrics if m.halted),
        "total_wall_clock_s": round(sum(m.wall_clock_s for m in metrics), 3),
        "num_missing_bundle": sum(
            1 for m in metrics if m.error and "no compiled bundle" in (m.error or "")
        ),
    }
