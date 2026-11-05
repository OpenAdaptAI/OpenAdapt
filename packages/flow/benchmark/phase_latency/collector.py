"""Numeric-only timing observations; no fixture control or artifact publishing.

Step spans are exclusive. Outer intervals are inclusive and remain a separate
basis. Uncovered time is unattributed, never inferred engine overhead.
"""

from __future__ import annotations

import math
import re
import statistics
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterator, Mapping, Sequence

from openadapt_flow.runtime.step_timing import SPANS

STEP_GROUPS = {
    "settle_before": "settle",
    "settle_after": "settle",
    "identity": "identity",
    "actuate": "actions",
    "postcondition": "verification",
    "readback": "verification",
    "resolve": "resolution",
    "screenshot": "capture_ocr",
    "ocr": "capture_ocr",
    "artifact_write": "evidence",
    "other": "unattributed_step",
}
OUTER_PHASES = frozenset(
    {"navigation", "session", "authentication", "admission", "replay",
     "oracle", "finalization", "engine_bookkeeping"}
)
OUTCOMES = frozenset(
    {"VERIFIED", "HALTED", "FAILED", "COMPLETED_UNVERIFIED", "ROLLED_BACK"}
)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


@dataclass(frozen=True, order=True)
class Trial:
    """Opaque app label and a preregistered matrix slot; no task values."""

    app: str
    mode: str
    block: int
    profiled: bool

    def __post_init__(self) -> None:
        if not re.fullmatch(r"app_[a-z0-9]+", self.app):
            raise ValueError("use an opaque app label")
        if self.mode not in {"cold", "warm"} or type(self.block) is not int or self.block < 1 or type(self.profiled) is not bool:
            raise ValueError("invalid matrix slot")


@dataclass(frozen=True)
class Observation:
    trial: Trial
    attempt: int
    source_head: str
    wall_ms: float | None
    status: str
    outcome: str
    oracle: str
    step_count: int
    measured_steps: int
    step_exclusive_ms: dict[str, float]
    step_issues: tuple[tuple[int, str], ...]
    outer_inclusive_ms: dict[str, float]
    outer_overlap_ms: float | None
    unattributed_outer_ms: float | None
    load_samples: tuple[dict[str, float], ...]
    load_missing: bool
    human_active_s: float | None
    human_wait_s: float | None

    @property
    def verified(self) -> bool:
        return self.status == "completed" and self.outcome == "VERIFIED" and self.oracle == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "openadapt.phase-observation/v1", **asdict(self)}


def untimed_failure(trial: Trial, *, attempt: int, source_head: str, status: str) -> Observation:
    """Retain a started attempt even when setup or its clock produced no data."""
    if type(attempt) is not int or attempt < 1 or not re.fullmatch(r"[0-9a-f]{40}", source_head):
        raise ValueError("invalid attempt provenance")
    if status not in {"setup_error", "timeout", "error"}:
        raise ValueError("an untimed attempt cannot claim completion")
    return Observation(trial, attempt, source_head, None, status, "unknown", "unrun",
                       0, 0, {}, ((-1, "missing_outer_clock"),), {}, None, None, (), True, None, None)


def _steps(report: Mapping[str, Any] | None) -> tuple[int, int, dict[str, float], tuple[tuple[int, str], ...]]:
    results = report.get("results") if report is not None else None
    if not isinstance(results, list) or not results:
        return 0, 0, {}, ((-1, "missing_steps"),)
    totals: dict[str, float] = {}
    issues: list[tuple[int, str]] = []
    measured = 0
    known = set(SPANS) | {"other"}
    for i, result in enumerate(results):
        if not isinstance(result, Mapping) or not isinstance(result.get("timings"), Mapping):
            issues.append((i, "missing_timings"))
            continue
        spans = result["timings"]
        elapsed = result.get("elapsed_ms")
        if not _number(elapsed) or any(not _number(v) for v in spans.values()):
            issues.append((i, "invalid_duration"))
            continue
        if any(k not in known or k not in STEP_GROUPS for k in spans):
            issues.append((i, "unknown_span"))
            continue
        # StepTimer rounds each span to 0.1 ms. Never renormalize a mismatch.
        if abs(sum(spans.values()) - elapsed) > 0.05 * len(spans) + 0.05:
            issues.append((i, "span_elapsed_mismatch"))
            continue
        measured += 1
        for span, duration in spans.items():
            group = STEP_GROUPS[span]
            totals[group] = totals.get(group, 0.0) + duration
    return len(results), measured, totals, tuple(issues)


class AttemptClock:
    """Monotonic outer clock. A finished attempt cannot be reused."""

    def __init__(self, *, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._start = clock()
        if isinstance(self._start, bool) or not isinstance(self._start, (int, float)) or not math.isfinite(self._start):
            raise ValueError("invalid monotonic clock")
        self._last = self._start
        self._intervals: list[tuple[str, float, float]] = []
        self._loads: list[dict[str, float]] = []
        self._open = 0
        self._finished = False

    def _now(self) -> float:
        if self._finished:
            raise ValueError("attempt already finished")
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < self._last:
            raise ValueError("clock did not remain monotonic")
        self._last = now
        return (now - self._start) * 1000.0

    @contextmanager
    def span(self, phase: str) -> Iterator[None]:
        if phase not in OUTER_PHASES:
            raise ValueError("unknown outer phase")
        start = self._now()
        self._open += 1
        try:
            yield
        finally:
            self._intervals.append((phase, start, self._now()))
            self._open -= 1

    def load(self, *, load1: float, cpu_percent: float, available_bytes: int) -> None:
        if not all(_number(v) for v in (load1, cpu_percent, available_bytes)) or cpu_percent > 100 or type(available_bytes) is not int:
            raise ValueError("invalid numeric resource observation")
        self._loads.append({"at_ms": self._now(), "load1": load1, "cpu_percent": cpu_percent, "available_bytes": available_bytes})

    def finish(
        self,
        trial: Trial,
        *,
        attempt: int,
        source_head: str,
        status: str,
        oracle: str,
        report: Mapping[str, Any] | None = None,
        human_active_s: float | None = None,
        human_wait_s: float | None = None,
    ) -> Observation:
        if self._open:
            raise ValueError("outer phase still open")
        if type(attempt) is not int or attempt < 1 or not re.fullmatch(r"[0-9a-f]{40}", source_head):
            raise ValueError("invalid attempt provenance")
        if status not in {"completed", "setup_error", "timeout", "error"} or oracle not in {"pass", "fail", "unknown", "unrun"}:
            raise ValueError("invalid attempt verdict")
        if any(v is not None and not _number(v) for v in (human_active_s, human_wait_s)):
            raise ValueError("invalid human-time observation")
        wall = self._now()
        self._finished = True
        inclusive: dict[str, float] = {}
        union_end = 0.0
        covered = 0.0
        for phase, start, end in sorted(self._intervals, key=lambda row: row[1]):
            inclusive[phase] = inclusive.get(phase, 0.0) + end - start
            covered += max(0.0, end - max(start, union_end))
            union_end = max(union_end, end)
        step_count, measured, totals, issues = _steps(report)
        outcome = report.get("execution_outcome") if report is not None else None
        outcome = outcome if isinstance(outcome, str) and outcome in OUTCOMES else "unknown"
        points = [0.0, *(row["at_ms"] for row in self._loads), wall]
        missing_load = not self._loads or any(b - a > 2000.0 for a, b in zip(points, points[1:]))
        return Observation(
            trial, attempt, source_head, wall, status, outcome, oracle,
            step_count, measured, totals, issues, inclusive,
            max(0.0, sum(inclusive.values()) - covered), max(0.0, wall - covered),
            tuple(self._loads), missing_load, human_active_s, human_wait_s,
        )


def analyze(plan: Sequence[Trial], rows: Sequence[Observation]) -> dict[str, Any]:
    """Keep all attempts; pair only unretried, independently verified slots."""
    planned = set(plan)
    if len(planned) != len(plan):
        raise ValueError("duplicate planned slot")
    if len({r.attempt for r in rows}) != len(rows) or any(r.trial not in planned for r in rows):
        raise ValueError("duplicate attempt or unplanned observation")
    if any(r.wall_ms is not None and not _number(r.wall_ms) for r in rows):
        raise ValueError("invalid authoritative wall duration")
    if len({r.source_head for r in rows}) > 1:
        raise ValueError("profiling pairs require a single frozen source")
    by_slot = {slot: [r for r in rows if r.trial == slot] for slot in plan}
    pairs: list[dict[str, Any]] = []
    pair_keys = {(t.app, t.mode, t.block) for t in plan}
    for app, mode, block in sorted(pair_keys):
        arms = [by_slot.get(Trial(app, mode, block, state), []) for state in (False, True)]
        if all(len(a) == 1 and a[0].verified and a[0].wall_ms is not None for a in arms):
            off, on = arms[0][0].wall_ms, arms[1][0].wall_ms
            if off is not None and on is not None:
                pairs.append({"app": app, "mode": mode, "block": block,
                              "delta_ms": on - off})

    def distribution(values: list[float | None]) -> dict[str, Any]:
        measured_values = [v for v in values if v is not None]
        return {"n": len(measured_values), "median_ms": statistics.median(measured_values) if measured_values else None,
                "min_ms": min(measured_values) if measured_values else None, "max_ms": max(measured_values) if measured_values else None}

    groups: list[dict[str, Any]] = []
    for app, mode, profiled in sorted({(t.app, t.mode, t.profiled) for t in plan}):
        observed = [r for r in rows if (r.trial.app, r.trial.mode, r.trial.profiled) == (app, mode, profiled)]
        good = [r for r in observed if r.verified]
        valid = [r for r in good if not r.step_issues]
        costs: list[dict[str, Any]] = []
        for phase in sorted(set(STEP_GROUPS.values()) - {"unattributed_step"}):
            values = [r.step_exclusive_ms.get(phase, 0.0) for r in valid]
            costs.append({"phase": phase, "observed_median_ms": statistics.median(values) if values else None,
                          "covered_attempts": len(values)})
        costs.sort(key=lambda row: -(row["observed_median_ms"] or 0))
        groups.append({"app": app, "mode": mode, "profiled": profiled,
                       "planned_slots": sum((t.app, t.mode, t.profiled) == (app, mode, profiled) for t in plan),
                       "attempts": len(observed), "verified_attempts": len(good),
                       "all_attempt_wall": distribution([r.wall_ms for r in observed]),
                       "verified_wall": distribution([r.wall_ms for r in good]), "phase_costs": costs})

    def human(field: str) -> dict[str, Any]:
        values = [getattr(r, field) for r in rows]
        known = sum(v for v in values if v is not None) / 60.0
        unknown = sum(v is None for v in values)
        return {"known_minutes": known, "total_minutes": None if unknown else known,
                "unknown_attempts": unknown, "scope": "recorded_attempts_only"}

    return {
        "schema": "openadapt.phase-analysis/v1",
        "planned_slots": len(plan),
        "started_attempts": len(rows),
        "unrun_slots": [asdict(t) for t in plan if not by_slot[t]],
        "retried_slots": sum(len(v) > 1 for v in by_slot.values()),
        "verified_attempts": sum(r.verified for r in rows),
        "statuses": {status: sum(r.status == status for r in rows) for status in ("completed", "setup_error", "timeout", "error")},
        "outcomes": {outcome: sum(r.outcome == outcome for r in rows) for outcome in sorted(OUTCOMES | {"unknown"})},
        "oracle_results": {value: sum(r.oracle == value for r in rows) for value in ("pass", "fail", "unknown", "unrun")},
        "timing_incomplete_attempts": sum(bool(r.step_issues) for r in rows),
        "wall_missing_attempts": sum(r.wall_ms is None for r in rows),
        "outer_overlap_attempts": sum(r.outer_overlap_ms is not None and r.outer_overlap_ms > 0 for r in rows),
        "load_missing_attempts": sum(r.load_missing for r in rows),
        "paired_slots": len(pair_keys),
        "eligible_pairs": len(pairs),
        "excluded_pairs": len(pair_keys) - len(pairs),
        "paired_profiler_delta_ms": pairs,
        "human_active": human("human_active_s"),
        "human_wait": human("human_wait_s"),
        "groups": groups,
        "interpretation": "observed_exclusive_cost_not_achieved_saving",
    }
