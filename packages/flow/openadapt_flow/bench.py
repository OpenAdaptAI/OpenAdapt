"""Benchmark harness: replay a compiled workflow bundle N times and aggregate.

``run_bench`` replays the bundle ``n`` times, each iteration against a fresh
backend produced by ``backend_factory`` and a freshly loaded ``Workflow``
(heals are applied in memory during a replay and must not leak between
iterations), and aggregates success rate, latency percentiles, rung usage,
heal counts, and model call/cost totals into a ``bench.json`` written under
``run_root``.

The Replayer is imported lazily (``from openadapt_flow.runtime import
Replayer``) inside :func:`run_bench`, so unit tests can monkeypatch
``sys.modules["openadapt_flow.runtime"]`` with a fake.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from openadapt_flow.ir import RunReport, Workflow


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile of ``values`` (``pct`` in [0, 100])."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _run_one(
    replayer_cls: Any,
    backend: Any,
    workflow: Workflow,
    *,
    params: dict[str, str] | None,
    bundle_dir: Path,
    run_dir: Path,
) -> RunReport:
    replayer = replayer_cls(backend)
    return replayer.run(workflow, params=params, bundle_dir=bundle_dir, run_dir=run_dir)


def run_bench(
    bundle_dir: Path | str,
    backend_factory: Callable[[], Any],
    n: int,
    *,
    params: dict[str, str] | None = None,
    run_root: Path | str,
) -> dict[str, Any]:
    """Replay a bundle ``n`` times and aggregate results into ``bench.json``.

    Args:
        bundle_dir: Workflow bundle directory (contains ``workflow.json``).
        backend_factory: Zero-arg callable invoked once per iteration. It may
            return a backend directly, or a context manager that yields the
            backend (in which case setup/teardown happen per iteration). A
            plain backend with a ``close()`` method is closed after use.
        n: Number of replay iterations.
        params: Parameter substitutions forwarded to every replay.
        run_root: Directory that receives one run dir per iteration
            (``iter_000``, ``iter_001``, ...) plus the ``bench.json``.

    Returns:
        The aggregate results dict (also serialized to
        ``<run_root>/bench.json``): keys include ``n``, ``success_count``,
        ``success_rate``, ``total_ms_p50``, ``total_ms_p95``,
        ``rung_counts``, ``heal_count``, ``model_calls``,
        ``est_model_cost_usd``, and per-iteration entries in ``iterations``.
    """
    # Lazy import so tests can monkeypatch openadapt_flow.runtime entirely.
    from openadapt_flow.runtime import Replayer

    bundle = Path(bundle_dir)
    root = Path(run_root)
    root.mkdir(parents=True, exist_ok=True)
    workflow_name = Workflow.load(bundle).name

    reports: list[RunReport] = []
    iterations: list[dict[str, Any]] = []
    for i in range(n):
        run_dir = root / f"iter_{i:03d}"
        # A FRESH Workflow per iteration: Replayer.run applies heals to the
        # in-memory object, so reusing one instance would leak iteration
        # i's healed anchors into iteration i+1 (while the on-disk template
        # crops stay stale) — iterations must be independent replays of the
        # bundle exactly as it exists on disk.
        workflow = Workflow.load(bundle)
        produced = backend_factory()
        if hasattr(produced, "__enter__"):
            with produced as backend:
                report = _run_one(
                    Replayer,
                    backend,
                    workflow,
                    params=params,
                    bundle_dir=bundle,
                    run_dir=run_dir,
                )
        else:
            backend = produced
            try:
                report = _run_one(
                    Replayer,
                    backend,
                    workflow,
                    params=params,
                    bundle_dir=bundle,
                    run_dir=run_dir,
                )
            finally:
                close = getattr(backend, "close", None)
                if callable(close):
                    close()
        reports.append(report)
        iterations.append(
            {
                "i": i,
                "success": report.success,
                "total_ms": report.total_ms,
                "heal_count": report.heal_count,
                "model_calls": report.model_calls,
                "est_model_cost_usd": report.est_model_cost_usd,
                "run_dir": str(run_dir),
            }
        )

    total_ms_values = [r.total_ms for r in reports]
    rung_counts: dict[str, int] = {}
    for report in reports:
        for rung, count in report.rung_counts.items():
            rung_counts[rung] = rung_counts.get(rung, 0) + count

    success_count = sum(1 for r in reports if r.success)
    result: dict[str, Any] = {
        "bundle": str(bundle),
        "workflow_name": workflow_name,
        "n": n,
        "params": dict(params or {}),
        "success_count": success_count,
        "success_rate": (success_count / n) if n else 0.0,
        "total_ms_p50": _percentile(total_ms_values, 50.0),
        "total_ms_p95": _percentile(total_ms_values, 95.0),
        "rung_counts": rung_counts,
        "heal_count": sum(r.heal_count for r in reports),
        "model_calls": sum(r.model_calls for r in reports),
        "est_model_cost_usd": sum(r.est_model_cost_usd for r in reports),
        "iterations": iterations,
    }

    (root / "bench.json").write_text(json.dumps(result, indent=2))
    return result
