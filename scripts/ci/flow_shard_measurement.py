#!/usr/bin/env python3
"""Measure a normal Flow fast shard without changing its tests or time limits.

The CLI launches the existing pytest command with this module as an observer.
Only the xdist controller writes phase records. Resource samples contain sums
for the launched process and its observed descendants, never command lines,
environments, captured output, locals, or unrelated processes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import resource
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

REFS = {
    "parent": "79833c3c18e5e4723635f0e4844b0957e3c18d9c",
    "current": "f81a0bcee5efacdb9387bc64d8480bc81aba7acb",
}
IDENTITY_PATHS = (
    ".github/workflows/packages.yml",
    "scripts/ci/locked_install.py",
    "scripts/ci/flow_shards.py",
    "scripts/ci/flow_test_durations.json",
    "packages/flow/uv.lock",
    "packages/capture/uv.lock",
    "packages/flow/tests/conftest.py",
    "packages/flow/tests/nightly_only.txt",
    "packages/flow/tests/test_transform_work_bounds.py",
    "packages/flow/openadapt_flow/transforms.py",
)
PACKAGES = ("pytest", "pytest-xdist", "pydantic", "playwright", "mcp", "psutil")
ORIGINS = {
    "openadapt_flow": "packages/flow/openadapt_flow/__init__.py",
    "openadapt_flow.transforms": "packages/flow/openadapt_flow/transforms.py",
    "openadapt_types": "packages/types/openadapt_types/__init__.py",
    "openadapt_capture": "packages/capture/openadapt_capture/__init__.py",
}
_RECORDS: Any = None
_COUNTS: Counter[str] = Counter()
SAMPLE_INTERVAL = 1.0
MAX_SAMPLE_GAP = 3.0
MAX_MEASUREMENT_BYTES = 32 * 1024 * 1024


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def _event(value: dict[str, object]) -> None:
    if _RECORDS is not None:
        _RECORDS.write(json.dumps(value, sort_keys=True) + "\n")
        _RECORDS.flush()


def pytest_configure(config: Any) -> None:
    """Workers send ordinary reports; only the controller retains metadata."""
    global _RECORDS
    if not hasattr(config, "workerinput"):
        output = Path(os.environ["OPENADAPT_SHARD_MEASUREMENT_OUTPUT"])
        _RECORDS = (output / "phases.jsonl").open("x", encoding="utf-8")
        _event({"kind": "session", "started_at": time.time()})


def pytest_xdist_node_collection_finished(node: Any, ids: list[str]) -> None:
    _event(
        {
            "kind": "collection",
            "worker": node.gateway.id,
            "count": len(ids),
            "nodeids": ids,
            "nodeids_sha256": hashlib.sha256("\n".join(ids).encode()).hexdigest(),
        }
    )


def pytest_runtest_logreport(report: Any) -> None:
    if _RECORDS is None:
        return
    kind = "subtest" if hasattr(report, "context") else "phase"
    _COUNTS[f"{kind}:{report.when}:{report.outcome}"] += 1
    _event(
        {
            "kind": kind,
            "nodeid": report.nodeid,
            "worker": getattr(report, "worker_id", None),
            "phase": report.when,
            "outcome": report.outcome,
            "started_at": report.start,
            "finished_at": report.stop,
            "seconds": report.duration,
        }
    )


def _loaded_origins() -> dict[str, object]:
    root = Path(os.environ["OPENADAPT_SHARD_MEASUREMENT_SUBJECT"])
    result: dict[str, object] = {}
    for name, relative in ORIGINS.items():
        module = sys.modules.get(name)
        if module is None:
            result[name] = {"status": "not_loaded"}
            continue
        source = getattr(module, "__file__", None)
        expected = (root / relative).resolve()
        if source is None or Path(source).resolve() != expected:
            result[name] = {"status": "unexpected_origin"}
            continue
        result[name] = {"status": "owned", "path": relative, "sha256": _sha(expected)}
    return result


def pytest_testnodedown(node: Any, error: object) -> None:
    _event(
        {
            "kind": "worker_finished",
            "worker": node.gateway.id,
            "clean_exit": error is None,
            "origins": node.workeroutput.get("measurement_origins"),
        }
    )


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    if hasattr(session.config, "workerinput"):
        session.config.workeroutput["measurement_origins"] = _loaded_origins()
        return
    _event(
        {
            "kind": "finish",
            "finished_at": time.time(),
            "exit_code": int(exitstatus),
            "collected": session.testscollected,
            "phase_counts": dict(_COUNTS),
        }
    )


def pytest_unconfigure(config: Any) -> None:
    global _RECORDS
    if _RECORDS is not None:
        _RECORDS.close()
        _RECORDS = None


def _stat(pid: int) -> dict[str, int | str] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
        return {
            "parent": int(fields[1]),
            "state": fields[0],
            "cpu_ticks": int(fields[11]) + int(fields[12]),
            "threads": int(fields[17]),
            "start": int(fields[19]),
            "rss_pages": int(fields[21]),
        }
    except (FileNotFoundError, ProcessLookupError):
        return None


def _children(pid: int) -> set[int]:
    children: set[int] = set()
    try:
        for task in Path(f"/proc/{pid}/task").iterdir():
            try:
                children.update(int(p) for p in (task / "children").read_text().split())
            except (FileNotFoundError, ProcessLookupError):
                continue
    except (FileNotFoundError, ProcessLookupError):
        pass
    return children


def _chain_current(chain: tuple[tuple[int, int], ...]) -> bool:
    for index, (pid, start) in enumerate(chain):
        current = _stat(pid)
        parent = chain[index - 1][0] if index else os.getpid()
        if current is None or current["start"] != start or current["parent"] != parent:
            return False
    return True


def _owned_snapshot(root: int, root_start: int) -> list[dict[str, int | str]]:
    """Accept a row only while its pinned child and ancestor identities agree."""
    pending: list[tuple[tuple[int, int], ...]] = [((root, root_start),)]
    seen: set[tuple[int, int]] = set()
    rows: list[dict[str, int | str]] = []
    while pending:
        chain = pending.pop()
        pid, start = chain[-1]
        if (pid, start) in seen or not _chain_current(chain):
            continue
        seen.add((pid, start))
        before = _stat(pid)
        if before is None or before["start"] != start:
            continue
        children = _children(pid)
        # Pin child starts before queueing; a recycled child PID cannot inherit
        # the old parent's membership. Every later row rechecks the full chain.
        candidates = []
        for child in children:
            state = _stat(child)
            if state is not None and state["parent"] == pid:
                candidates.append(chain + ((child, int(state["start"])),))
        after = _stat(pid)
        if (
            after is None
            or after["start"] != start
            or after["parent"] != before["parent"]
            or not _chain_current(chain)
        ):
            continue
        rows.append({"pid": pid, **after})
        pending.extend(candidates)
    return rows


def _sample(
    root: int, root_start: int, output: Path, done: threading.Event, started: float
) -> None:
    observed: dict[tuple[int, int], int] = {}
    cpu_ticks = 0
    try:
        with (output / "resources.jsonl").open("x", encoding="utf-8") as stream:
            while not done.is_set():
                rows = _owned_snapshot(root, root_start)
                for row in rows:
                    key = (int(row["pid"]), int(row["start"]))
                    ticks = int(row["cpu_ticks"])
                    cpu_ticks += max(0, ticks - observed.get(key, 0))
                    observed[key] = ticks
                sample = {
                    "elapsed_seconds": time.monotonic() - started,
                    "processes": len(rows),
                    "threads": sum(int(row["threads"]) for row in rows),
                    "runnable_processes": sum(row["state"] == "R" for row in rows),
                    "rss_sum_bytes": sum(int(row["rss_pages"]) for row in rows)
                    * os.sysconf("SC_PAGE_SIZE"),
                    "observed_cpu_seconds": cpu_ticks / os.sysconf("SC_CLK_TCK"),
                }
                stream.write(json.dumps(sample, sort_keys=True) + "\n")
                stream.flush()
                done.wait(SAMPLE_INTERVAL)
    except (OSError, ValueError, IndexError):
        # A metric failure must not turn a failed or unmeasured run green.
        _write_json(
            output / "measurement-error.json", {"kind": "resource_sample_failed"}
        )


def _capacity(name: str) -> str | None:
    path = Path("/sys/fs/cgroup") / name
    return path.read_text().strip() if path.is_file() else None


def _identity(root: Path, files: list[str], profile: str, shard: int) -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True
        ).strip()

    if git("rev-parse", "HEAD") != REFS[profile]:
        raise ValueError("subject checkout differs from fixed comparison ref")
    return {
        "profile": profile,
        "commit": REFS[profile],
        "tree": git("rev-parse", "HEAD^{tree}"),
        "shard": shard,
        "of": 3,
        "lane": "fast",
        "python": sys.version,
        "logical_cpus": os.cpu_count(),
        "affinity_cpus": len(os.sched_getaffinity(0)),
        "cpu_quota": _capacity("cpu.max"),
        "memory_limit": _capacity("memory.max"),
        "dependencies": {name: importlib.metadata.version(name) for name in PACKAGES},
        "source_sha256": {name: _sha(root / name) for name in IDENTITY_PATHS},
        "test_files_sha256": {
            name: _sha(root / "packages/flow" / name) for name in files
        },
        "expected_origins_sha256": {
            name: _sha(root / relative) for name, relative in ORIGINS.items()
        },
        "reporter_sha256": _sha(Path(__file__)),
        "limitations": [
            "One ordered pair cannot establish resource contention or explain older merge heads.",
            "Samples miss short-lived or reparented descendants and CPU use between samples.",
            "RSS sums can double-count shared pages; runnable count is a sampled lower bound.",
            "Observer startup, sampling and phase writes add overhead to both profiles.",
            "No environment, process arguments, captured output or foreign process metrics are retained.",
        ],
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value >= 0


def _records(path: Path) -> list[dict[str, Any]]:
    def refuse_constant(value: str) -> None:
        raise ValueError("nonfinite JSON number")

    with path.open("rb") as stream:
        raw = stream.read(MAX_MEASUREMENT_BYTES + 1)
    _require(0 < len(raw) <= MAX_MEASUREMENT_BYTES, "empty or oversized observations")
    result = [
        json.loads(line, parse_constant=refuse_constant) for line in raw.splitlines()
    ]
    _require(all(type(row) is dict for row in result), "invalid observation object")
    return result


def _validate_samples(samples: list[dict[str, Any]], wall: float) -> None:
    _require(_number(wall) and wall > 0 and bool(samples), "missing resource coverage")
    keys = {
        "elapsed_seconds",
        "processes",
        "threads",
        "runnable_processes",
        "rss_sum_bytes",
        "observed_cpu_seconds",
    }
    last_time = 0.0
    last_cpu = 0.0
    for index, sample in enumerate(samples):
        _require(set(sample) == keys, "resource fields differ")
        timestamp = sample["elapsed_seconds"]
        cpu = sample["observed_cpu_seconds"]
        _require(_number(timestamp) and _number(cpu), "invalid resource timing")
        _require(
            timestamp >= last_time and timestamp - last_time <= MAX_SAMPLE_GAP,
            "resource sampling gap",
        )
        _require(index == 0 or timestamp > last_time, "duplicate resource time")
        _require(
            timestamp <= wall + MAX_SAMPLE_GAP and cpu >= last_cpu, "resource drift"
        )
        for name in keys - {"elapsed_seconds", "observed_cpu_seconds"}:
            _require(
                type(sample[name]) is int and sample[name] >= 0,
                "invalid resource count",
            )
        _require(
            sample["runnable_processes"] <= sample["processes"],
            "invalid runnable count",
        )
        _require(sample["threads"] >= sample["processes"], "invalid thread count")
        last_time, last_cpu = timestamp, cpu
    _require(samples[0]["processes"] > 0, "no initial owned process sample")
    _require(wall - last_time <= MAX_SAMPLE_GAP, "resource tail is unobserved")


def _timing_slack(*values: float) -> float:
    """Rounding allowance, not a bound on real-time clock skew or scheduling.

    Four units cover the clock readings, subtraction and comparison. Epoch
    and elapsed values must be passed separately so a large timestamp cannot
    inflate the allowance for a duration. A material clock discontinuity is
    an incomplete observation, never evidence of contention or a test pass.
    """
    resolution = max(
        time.get_clock_info(name).resolution
        for name in ("time", "monotonic", "perf_counter")
    )
    return 4 * (resolution + max(math.ulp(float(value)) for value in values))


def _validate_timing_envelope(
    session: dict[str, Any],
    finish: dict[str, Any],
    phases: dict[str, list[dict[str, Any]]],
    subtests: list[dict[str, Any]],
    wall: float,
) -> None:
    """Bind all report intervals to the observed session and process lifetime.

    Pytest uses real-time timestamps and a separate performance-counter
    duration. We do not demand equality between those clocks. Cross-clock
    upper bounds are conservative: skew that violates them makes this
    measurement incomplete while the caller preserves pytest's exit code.
    """
    _require(_number(wall) and wall > 0, "invalid process wall time")
    start, stop = session["started_at"], finish["finished_at"]
    epoch_slack = _timing_slack(start, stop)
    _require(epoch_slack < wall, "timestamp precision cannot resolve the run")
    _require(
        stop - start <= wall + epoch_slack + _timing_slack(wall),
        "session exceeds process wall time",
    )

    def contained(
        row: dict[str, Any], lower: float, upper: float, duration: float
    ) -> None:
        slack = _timing_slack(lower, upper, row["started_at"], row["finished_at"])
        _require(
            row["started_at"] >= lower - slack and row["finished_at"] <= upper + slack,
            "report interval outside enclosing interval",
        )
        _require(
            row["seconds"] <= duration + _timing_slack(duration, row["seconds"]),
            "report duration exceeds enclosing duration",
        )

    workers: dict[str, list[dict[str, Any]]] = {}
    for reports in phases.values():
        for row in reports:
            contained(row, start, stop, wall)
            workers.setdefault(row["worker"], []).append(row)
        for previous, current in zip(reports, reports[1:]):
            _require(
                previous["finished_at"]
                <= current["started_at"]
                + _timing_slack(previous["finished_at"], current["started_at"]),
                "node phases overlap or run backwards",
            )
    for reports in workers.values():
        ordered = sorted(reports, key=lambda row: row["started_at"])
        for previous, current in zip(ordered, ordered[1:]):
            _require(
                previous["finished_at"]
                <= current["started_at"]
                + _timing_slack(previous["finished_at"], current["started_at"]),
                "ordinary reports overlap on one worker",
            )
        total = math.fsum(row["seconds"] for row in reports)
        slack = math.fsum(_timing_slack(row["seconds"]) for row in reports)
        _require(
            total <= wall + slack + _timing_slack(total, wall),
            "worker durations exceed process wall time",
        )
    for row in subtests:
        call = next(
            report for report in phases[row["nodeid"]] if report["phase"] == "call"
        )
        contained(row, call["started_at"], call["finished_at"], call["seconds"])
        # Subtests may nest. Their durations are contained, never added again
        # to the ordinary call duration or the worker's total.


def _validate_phases(
    events: list[dict[str, Any]],
    code: int,
    expected_origins: dict[str, str],
    wall: float,
) -> dict[str, int]:
    _require(bool(events), "missing phase observations")
    _require(events[0].get("kind") == "session", "missing initial session")
    _require(events[-1].get("kind") == "finish", "missing terminal session")
    sessions = [event for event in events if event.get("kind") == "session"]
    finishes = [event for event in events if event.get("kind") == "finish"]
    _require(len(sessions) == len(finishes) == 1, "duplicate session records")
    _require(set(sessions[0]) == {"kind", "started_at"}, "invalid session fields")
    _require(_number(sessions[0]["started_at"]), "invalid session time")
    finish = finishes[0]
    _require(
        set(finish)
        == {"kind", "finished_at", "exit_code", "collected", "phase_counts"},
        "invalid finish fields",
    )
    _require(
        _number(finish["finished_at"])
        and finish["finished_at"] >= sessions[0]["started_at"],
        "invalid finish time",
    )
    _require(
        type(finish["exit_code"]) is int and finish["exit_code"] == code,
        "exit mismatch",
    )
    _require(
        type(finish["collected"]) is int and finish["collected"] > 0, "empty collection"
    )
    _require(type(finish["phase_counts"]) is dict, "invalid finish counts")
    collections: dict[str, list[str]] = {}
    workers: dict[str, dict[str, Any]] = {}
    phases: dict[str, list[dict[str, Any]]] = {}
    subtests: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for event in events[1:-1]:
        kind = event.get("kind")
        worker = event.get("worker")
        if type(worker) is not str or not worker:
            raise ValueError("missing worker identity")
        if kind == "collection":
            _require(
                set(event) == {"kind", "worker", "count", "nodeids", "nodeids_sha256"},
                "invalid collection fields",
            )
            ids = event["nodeids"]
            _require(type(ids) is list and bool(ids), "empty worker collection")
            _require(
                all(type(node) is str and node for node in ids), "invalid node identity"
            )
            _require(len(ids) == len(set(ids)), "duplicate collected node")
            _require(
                type(event["count"]) is int and event["count"] == len(ids),
                "count mismatch",
            )
            _require(
                event["nodeids_sha256"]
                == hashlib.sha256("\n".join(ids).encode()).hexdigest(),
                "collection digest mismatch",
            )
            _require(worker not in collections, "duplicate worker collection")
            collections[worker] = ids
        elif kind == "worker_finished":
            _require(
                set(event) == {"kind", "worker", "clean_exit", "origins"},
                "invalid worker fields",
            )
            _require(
                worker in collections and worker not in workers,
                "unknown or repeated worker",
            )
            _require(event["clean_exit"] is True, "unclean worker termination")
            origins = event["origins"]
            _require(
                type(origins) is dict and set(origins) == set(ORIGINS),
                "missing origins",
            )
            for name, entry in origins.items():
                _require(
                    entry == {"status": "not_loaded"}
                    or entry
                    == {
                        "status": "owned",
                        "path": ORIGINS[name],
                        "sha256": expected_origins[name],
                    },
                    "origin differs from subject",
                )
            workers[worker] = event
        elif kind in ("phase", "subtest"):
            _require(
                set(event)
                == {
                    "kind",
                    "nodeid",
                    "worker",
                    "phase",
                    "outcome",
                    "started_at",
                    "finished_at",
                    "seconds",
                },
                "invalid phase fields",
            )
            _require(
                worker in collections and worker not in workers,
                "phase outside worker window",
            )
            _require(event["nodeid"] in collections[worker], "uncollected node report")
            _require(event["phase"] in ("setup", "call", "teardown"), "unknown phase")
            _require(
                event["outcome"] in ("passed", "failed", "skipped"), "unknown outcome"
            )
            _require(
                all(
                    _number(event[name])
                    for name in ("started_at", "finished_at", "seconds")
                )
                and event["finished_at"] >= event["started_at"],
                "invalid phase timing",
            )
            counts[f"{kind}:{event['phase']}:{event['outcome']}"] += 1
            if kind == "subtest":
                _require(event["phase"] == "call", "invalid subtest phase")
                subtests.append(event)
            else:
                phases.setdefault(event["nodeid"], []).append(event)
        else:
            raise ValueError("unknown observation kind")
    _require(
        bool(collections) and set(workers) == set(collections), "unfinished worker"
    )
    ids = next(iter(collections.values()))
    _require(
        all(other == ids for other in collections.values()), "worker collections differ"
    )
    _require(
        finish["collected"] == len(ids) and set(phases) == set(ids),
        "missing node phases",
    )
    for node, reports in phases.items():
        setup = reports[0]
        expected = (
            ["setup", "call", "teardown"]
            if setup["outcome"] == "passed"
            else ["setup", "teardown"]
        )
        _require(
            [row["phase"] for row in reports] == expected, "missing or duplicate phase"
        )
        _require(len({row["worker"] for row in reports}) == 1, "node changed worker")
    for row in subtests:
        _require(
            phases[row["nodeid"]][0]["outcome"] == "passed"
            and phases[row["nodeid"]][0]["worker"] == row["worker"],
            "subtest outside callable node",
        )
    _require(
        all(
            type(value) is int and value >= 0
            for value in finish["phase_counts"].values()
        )
        and finish["phase_counts"] == dict(counts),
        "finish phase counts differ",
    )
    _require(code in (0, 1), "pytest did not finish normally")
    _require(
        code != 0 or not any(key.endswith(":failed") for key in counts),
        "failed phase with success exit",
    )
    _validate_timing_envelope(sessions[0], finish, phases, subtests, wall)
    return {
        "workers_observed": len(workers),
        "observations": len(events),
        "collected": len(ids),
    }


def _finalize(
    output: Path,
    code: int,
    wall: float,
    observer_cpu: float,
    before: dict[str, Any],
    after_identity: Any,
    had_initial_sample: bool,
) -> int:
    _write_json(output / "pytest-exit.json", {"pytest_exit_code": code})
    result: dict[str, Any] = {
        "pytest_exit_code": code,
        "pytest_wall_seconds": wall,
        "observer_cpu_seconds": observer_cpu,
        "measurement_complete": False,
    }
    try:
        after = after_identity()
        _write_json(output / "identity-after.json", after)
        _require(before == after, "subject identity drift")
        _require(had_initial_sample, "missing initial process identity")
        _require(not (output / "measurement-error.json").exists(), "sampler error")
        _validate_samples(_records(output / "resources.jsonl"), wall)
        result.update(
            _validate_phases(
                _records(output / "phases.jsonl"),
                code,
                before["expected_origins_sha256"],
                wall,
            )
        )
        result["measurement_complete"] = True
    except Exception as exc:
        # Fixed classification only, never exception text or captured output.
        result["observation_error_type"] = type(exc).__name__
    _write_json(output / "result.json", result)
    return code if code else (0 if result["measurement_complete"] else 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--subject", type=Path, required=True)
    parser.add_argument("--files", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=tuple(REFS), required=True)
    parser.add_argument("--shard", type=int, choices=(1, 2, 3), required=True)
    args = parser.parse_args()
    root = args.subject.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    files = args.files.read_text().splitlines()
    if not files or len(files) != len(set(files)):
        raise ValueError("empty or duplicate shard selection")
    for name in files:
        path = Path(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not name.startswith("tests/")
            or not path.name.startswith("test_")
            or path.suffix != ".py"
        ):
            raise ValueError("invalid test file selection")
    if os.environ.get("OPENADAPT_TEST_LANE") != "fast":
        raise ValueError("measurement requires the unchanged fast lane")
    before = _identity(root, files, args.profile, args.shard)
    _write_json(output / "identity-before.json", before)
    environment = os.environ.copy()
    # Only the observer directory is added. The subject's installed packages,
    # conftest and pytest fixtures keep their ordinary import paths.
    observer = str(Path(__file__).resolve().parent)
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (observer, environment.get("PYTHONPATH", "")) if part
    )
    environment["OPENADAPT_SHARD_MEASUREMENT_OUTPUT"] = str(output)
    environment["OPENADAPT_SHARD_MEASUREMENT_SUBJECT"] = str(root)
    command = [
        "pytest",
        "-p",
        "flow_shard_measurement",
        "-n",
        "logical",
        "--dist",
        "loadfile",
        "-v",
        "--durations=20",
        "--basetemp=runs/ci",
        *files,
    ]
    (root / "packages/flow/runs").mkdir(exist_ok=True)
    started = time.monotonic()
    observer_before = resource.getrusage(resource.RUSAGE_SELF)
    child = subprocess.Popen(command, cwd=root / "packages/flow", env=environment)
    initial = _stat(child.pid)
    done = threading.Event()
    sampler = None
    if initial is not None:
        sampler = threading.Thread(
            target=_sample,
            args=(child.pid, int(initial["start"]), output, done, started),
            daemon=True,
        )
        sampler.start()
    code = child.wait()
    pytest_wall_seconds = time.monotonic() - started
    # Retain this before sampling shutdown, identity reads or JSON parsing.
    _write_json(output / "pytest-exit.json", {"pytest_exit_code": code})
    done.set()
    if sampler is not None:
        sampler.join()
    observer_after = resource.getrusage(resource.RUSAGE_SELF)
    return _finalize(
        output,
        code,
        pytest_wall_seconds,
        observer_after.ru_utime
        + observer_after.ru_stime
        - observer_before.ru_utime
        - observer_before.ru_stime,
        before,
        lambda: _identity(root, files, args.profile, args.shard),
        initial is not None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
