"""Observer accounting and owned-process boundaries; no Flow test substitutions."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "flow_shard_measurement",
    Path(__file__).resolve().parents[1] / "flow_shard_measurement.py",
)
assert _SPEC is not None and _SPEC.loader is not None
measurement = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(measurement)


def _events(code=0):
    ids = ["test_synthetic.py::test_case"]
    events = [
        {"kind": "session", "started_at": 1.0},
        {
            "kind": "collection",
            "worker": "gw0",
            "count": 1,
            "nodeids": ids,
            "nodeids_sha256": hashlib.sha256("\n".join(ids).encode()).hexdigest(),
        },
    ]
    for index, phase in enumerate(("setup", "call", "teardown")):
        events.append(
            {
                "kind": "phase",
                "nodeid": ids[0],
                "worker": "gw0",
                "phase": phase,
                "outcome": "failed" if code and phase == "call" else "passed",
                "started_at": 1.02 + index * 0.02,
                "finished_at": 1.03 + index * 0.02,
                "seconds": 0.01,
            }
        )
    events.append(
        {
            "kind": "worker_finished",
            "worker": "gw0",
            "clean_exit": True,
            "origins": {name: {"status": "not_loaded"} for name in measurement.ORIGINS},
        }
    )
    events.append(
        {
            "kind": "finish",
            "finished_at": 1.1,
            "exit_code": code,
            "collected": 1,
            "phase_counts": dict(
                Counter(
                    f"{e['kind']}:{e['phase']}:{e['outcome']}"
                    for e in events
                    if e["kind"] == "phase"
                )
            ),
        }
    )
    return events


def _sample(elapsed=0.1):
    return {
        "elapsed_seconds": elapsed,
        "processes": 1,
        "threads": 1,
        "runnable_processes": 0,
        "rss_sum_bytes": 4096,
        "observed_cpu_seconds": 0.0,
    }


def _write_records(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_complete_failed_run_is_measured_without_becoming_success(tmp_path):
    _write_records(tmp_path / "resources.jsonl", [_sample()])
    _write_records(tmp_path / "phases.jsonl", _events(1))
    identity = {"expected_origins_sha256": {}}
    assert (
        measurement._finalize(tmp_path, 1, 0.2, 0.0, identity, lambda: identity, True)
        == 1
    )
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["pytest_exit_code"] == 1
    assert result["measurement_complete"] is True


@pytest.mark.parametrize("problem", ["empty", "absent", "nonfinite", "malformed"])
@pytest.mark.parametrize("code", [0, 1, 2])
def test_bad_samples_never_hide_original_pytest_exit(tmp_path, problem, code):
    path = tmp_path / "resources.jsonl"
    if problem == "empty":
        path.write_text("")
    elif problem == "nonfinite":
        path.write_text('{"elapsed_seconds":NaN}\n')
    elif problem == "malformed":
        path.write_text("{\n")
    _write_records(tmp_path / "phases.jsonl", _events(code))
    identity = {"expected_origins_sha256": {}}
    assert measurement._finalize(
        tmp_path, code, 0.2, 0.0, identity, lambda: identity, True
    ) == (code or 2)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["pytest_exit_code"] == code
    assert (
        json.loads((tmp_path / "pytest-exit.json").read_text())["pytest_exit_code"]
        == code
    )
    assert result["measurement_complete"] is False


@pytest.mark.parametrize("code", [0, 1, 2, 5])
@pytest.mark.parametrize(
    "problem", ["identity_exception", "identity_drift", "phase_parse"]
)
def test_postprocessing_errors_preserve_exit(tmp_path, code, problem):
    _write_records(tmp_path / "resources.jsonl", [_sample()])
    (tmp_path / "phases.jsonl").write_text("not json\n")
    identity = {"expected_origins_sha256": {}}

    def after():
        if problem == "identity_exception":
            raise OSError("synthetic identity read refusal")
        return {} if problem == "identity_drift" else identity

    assert measurement._finalize(tmp_path, code, 0.2, 0.0, identity, after, True) == (
        code or 2
    )
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["pytest_exit_code"] == code
    assert (
        json.loads((tmp_path / "pytest-exit.json").read_text())["pytest_exit_code"]
        == code
    )
    assert result["measurement_complete"] is False


@pytest.mark.parametrize(
    "problem",
    [
        "empty",
        "initial_gap",
        "middle_gap",
        "tail_gap",
        "duplicate_time",
        "nan",
        "boolean_count",
        "negative_rss",
        "too_many_runnable",
        "zero_initial",
    ],
)
def test_sample_coverage_refuses_missing_or_malformed_intervals(problem):
    rows = [_sample(0.1), _sample(1.0)]
    wall = 1.2
    if problem == "empty":
        rows = []
    elif problem == "initial_gap":
        rows = [_sample(4.0)]
        wall = 4.2
    elif problem == "middle_gap":
        rows[1]["elapsed_seconds"] = 4.0
        wall = 4.2
    elif problem == "tail_gap":
        wall = 5.0
    elif problem == "duplicate_time":
        rows[1]["elapsed_seconds"] = 0.1
    elif problem == "nan":
        rows[0]["elapsed_seconds"] = float("nan")
    elif problem == "boolean_count":
        rows[0]["processes"] = True
    elif problem == "negative_rss":
        rows[0]["rss_sum_bytes"] = -1
    elif problem == "too_many_runnable":
        rows[0]["runnable_processes"] = 2
    elif problem == "zero_initial":
        rows[0]["processes"] = 0
    with pytest.raises(ValueError):
        measurement._validate_samples(rows, wall)


@pytest.mark.parametrize(
    "problem",
    [
        "missing_setup",
        "missing_call",
        "missing_teardown",
        "duplicate_phase",
        "missing_collection",
        "missing_finish",
        "missing_worker",
        "duplicate_worker",
        "collection_count",
        "collection_digest",
        "finish_count",
        "phase_counts",
        "unknown_node",
        "unknown_worker",
        "phase_nan",
        "phase_bool",
        "wrong_exit",
        "unknown_kind",
        "origin_drift",
        "false_success",
    ],
)
def test_phase_accounting_refuses_incomplete_or_malformed_reports(problem):
    rows = _events()
    expected_origins = {}
    if problem.startswith("missing_") and problem.split("_", 1)[1] in (
        "setup",
        "call",
        "teardown",
    ):
        rows = [r for r in rows if r.get("phase") != problem.split("_", 1)[1]]
    elif problem == "duplicate_phase":
        rows.insert(3, copy.deepcopy(rows[2]))
    elif problem == "missing_collection":
        del rows[1]
    elif problem == "missing_finish":
        rows.pop()
    elif problem == "missing_worker":
        del rows[-2]
    elif problem == "duplicate_worker":
        rows.insert(-1, copy.deepcopy(rows[-2]))
    elif problem == "collection_count":
        rows[1]["count"] = 2
    elif problem == "collection_digest":
        rows[1]["nodeids_sha256"] = "0" * 64
    elif problem == "finish_count":
        rows[-1]["collected"] = 2
    elif problem == "phase_counts":
        rows[-1]["phase_counts"] = {}
    elif problem == "unknown_node":
        rows[2]["nodeid"] = "test_unknown"
    elif problem == "unknown_worker":
        rows[2]["worker"] = "gw9"
    elif problem == "phase_nan":
        rows[2]["seconds"] = float("nan")
    elif problem == "phase_bool":
        rows[2]["seconds"] = True
    elif problem == "wrong_exit":
        rows[-1]["exit_code"] = 1
    elif problem == "unknown_kind":
        rows[2]["kind"] = "invented"
    elif problem == "origin_drift":
        expected_origins = {name: "a" * 64 for name in measurement.ORIGINS}
        assert (
            measurement._validate_phases(rows, 0, expected_origins, 0.2)["collected"]
            == 1
        )
        rows[-2]["origins"]["openadapt_flow"] = {"status": "unexpected_origin"}
    elif problem == "false_success":
        rows = _events(1)
        rows[-1]["exit_code"] = 0
    with pytest.raises(
        ValueError,
        match="origin differs from subject" if problem == "origin_drift" else None,
    ):
        measurement._validate_phases(rows, 0, expected_origins, 0.2)


def _state(parent, start):
    return {
        "parent": parent,
        "start": start,
        "state": "S",
        "cpu_ticks": 0,
        "threads": 1,
        "rss_pages": 1,
    }


@pytest.mark.parametrize(
    "change", ["parent_reuse", "child_reuse", "reparent", "ancestor_reuse"]
)
def test_descendant_queue_revalidates_pinned_ancestry(monkeypatch, change):
    # All links are initially valid. Mutation occurs only after a parent row
    # has passed validation and its child has been queued for later traversal.
    states = {10: _state(os.getpid(), 100), 20: _state(10, 200), 30: _state(20, 300)}
    child_reads = 0

    def stat(pid):
        nonlocal child_reads
        if pid == 30:
            child_reads += 1
            if child_reads == 2:
                if change == "parent_reuse":
                    states[20]["start"] = 201
                elif change == "child_reuse":
                    states[30]["start"] = 301
                elif change == "reparent":
                    states[30]["parent"] = 999
                elif change == "ancestor_reuse":
                    states[10]["start"] = 101
        return dict(states[pid]) if pid in states else None

    monkeypatch.setattr(measurement, "_stat", stat)
    monkeypatch.setattr(
        measurement, "_children", lambda pid: {10: {20}, 20: {30}, 30: set()}[pid]
    )
    rows = measurement._owned_snapshot(10, 100)
    assert 30 not in {row["pid"] for row in rows}
    assert 10 in {row["pid"] for row in rows}


def test_owned_tree_excludes_real_foreign_sibling(tmp_path):
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)']); "
        "Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(20)"
    )
    marker = tmp_path / "child.pid"
    owned = subprocess.Popen([sys.executable, "-c", script, str(marker)])
    foreign = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(20)"])
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        child_pid = int(marker.read_text())
        initial = measurement._stat(owned.pid)
        assert initial is not None
        rows = measurement._owned_snapshot(owned.pid, int(initial["start"]))
        assert {owned.pid, child_pid} <= {row["pid"] for row in rows}
        assert foreign.pid not in {row["pid"] for row in rows}
        done = threading.Event()
        started = time.monotonic()
        sampler = threading.Thread(
            target=measurement._sample,
            args=(owned.pid, int(initial["start"]), tmp_path, done, started),
        )
        sampler.start()
        try:
            time.sleep(1.1)
        finally:
            done.set()
            sampler.join(timeout=5)
        assert not sampler.is_alive()
        samples = measurement._records(tmp_path / "resources.jsonl")
        measurement._validate_samples(samples, time.monotonic() - started)
        assert all(row["processes"] == 2 for row in samples)
    finally:
        if child_pid is not None:
            os.kill(child_pid, 15)
        for process in (owned, foreign):
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize("bad", ["--subj", "--fil", "--out", "--prof", "--sha"])
def test_cli_does_not_abbreviate_options(bad):
    completed = subprocess.run(
        [sys.executable, measurement.__file__, bad, "unused"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 2
    # Supply the other required fields, so missing-required diagnostics cannot
    # conceal acceptance of the abbreviated option in the first call.
    args = [
        "--subject",
        ".",
        "--files",
        "unused",
        "--output",
        "unused",
        "--profile",
        "parent",
        "--shard",
        "1",
    ]
    completed = subprocess.run(
        [sys.executable, measurement.__file__, *args, bad, "unused"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 2
    assert f"unrecognized arguments: {bad} unused" in completed.stderr


@pytest.mark.parametrize("failure", [False, True])
def test_genuine_xdist_reports_all_outcomes_and_subtests(tmp_path, failure):
    source = """
import pytest

def test_pass(subtests):
    with subtests.test(i=1):
        assert True
    with subtests.test(i=2):
        assert True

@pytest.mark.skip(reason='synthetic skip')
def test_skip():
    pass

@pytest.mark.xfail(reason='synthetic expected failure', strict=True)
def test_xfail():
    assert False
"""
    if failure:
        source += """
@pytest.fixture
def refuses_setup():
    raise RuntimeError('synthetic setup failure')

def test_error(refuses_setup):
    pass

def test_failure():
    assert False
"""
    (tmp_path / "test_synthetic.py").write_text(source)
    output = tmp_path / "observations"
    output.mkdir()
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(Path(measurement.__file__).parent),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        OPENADAPT_SHARD_MEASUREMENT_OUTPUT=str(output),
        OPENADAPT_SHARD_MEASUREMENT_SUBJECT=str(tmp_path),
    )
    # Two workers bound this observer test. The real workflow remains -n logical.
    started = time.monotonic()
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "xdist.plugin",
            "-p",
            "flow_shard_measurement",
            "-n",
            "2",
            "--dist",
            "loadfile",
            "-q",
            "test_synthetic.py",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert run.returncode == (1 if failure else 0), run.stdout + run.stderr
    events = measurement._records(output / "phases.jsonl")
    result = measurement._validate_phases(
        events, run.returncode, {}, time.monotonic() - started
    )
    assert result["collected"] == (5 if failure else 3)
    assert result["workers_observed"] == 2
    assert len([row for row in events if row["kind"] == "subtest"]) == 2
    assert any(
        row.get("phase") == "setup" and row.get("outcome") == "skipped"
        for row in events
    )


def test_sampler_stopped_before_first_tick_cannot_qualify(tmp_path):
    done = threading.Event()
    done.set()
    measurement._sample(1, 1, tmp_path, done, time.monotonic())
    assert (tmp_path / "resources.jsonl").read_bytes() == b""
    identity = {"expected_origins_sha256": {}}
    _write_records(tmp_path / "phases.jsonl", _events())
    assert (
        measurement._finalize(tmp_path, 0, 0.2, 0.0, identity, lambda: identity, True)
        == 2
    )
    assert (
        json.loads((tmp_path / "result.json").read_text())["measurement_complete"]
        is False
    )


def test_collection_identity_cannot_be_replaced_by_matching_counts():
    rows = _events()
    rows[1]["nodeids"] = ["test_synthetic.py::different_case"]
    rows[1]["nodeids_sha256"] = hashlib.sha256(
        rows[1]["nodeids"][0].encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="uncollected node report"):
        measurement._validate_phases(rows, 0, {}, 0.2)


def test_finish_counts_cannot_bless_a_removed_call():
    rows = _events()
    rows = [row for row in rows if row.get("phase") != "call"]
    del rows[-1]["phase_counts"]["phase:call:passed"]
    with pytest.raises(ValueError, match="missing or duplicate phase"):
        measurement._validate_phases(rows, 0, {}, 0.2)


def test_workflow_keeps_the_fixed_pair_and_original_test_policy():
    import yaml

    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load(
        (root / ".github/workflows/flow-shard-measurement.yml").read_text()
    )
    trigger = workflow.get("on", workflow.get(True))
    assert trigger == {
        "workflow_dispatch": {
            "inputs": {
                "run_comparison": {
                    "description": "Run the fixed-reference six-job comparison",
                    "type": "boolean",
                    "required": True,
                    "default": False,
                }
            }
        }
    }
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert set(jobs) == {"parent", "current"}
    assert "needs" not in jobs["parent"]
    assert jobs["current"]["needs"] == "parent"
    for profile, job in jobs.items():
        assert job["timeout-minutes"] == 40
        assert job["strategy"] == {
            "fail-fast": False,
            "max-parallel": 3,
            "matrix": {"shard": [1, 2, 3]},
        }
        activation = (
            "github.event_name == 'workflow_dispatch' && "
            "inputs.run_comparison == true && "
            "github.repository == 'OpenAdaptAI/OpenAdapt' && "
            "github.ref == 'refs/heads/agent/2078'"
        )
        assert job["if"].strip() == (
            ("always() && !cancelled() && " if profile == "current" else "")
            + activation
        )
        observer = next(
            step
            for step in job["steps"]
            if step.get("name") == "Check out the reviewed observer"
        )
        assert observer["with"]["ref"] == "${{ github.sha }}"
        assert observer["with"]["persist-credentials"] is False
        subject = next(
            step
            for step in job["steps"]
            if step.get("name") == "Check out the fixed subject"
        )
        assert subject["with"]["ref"] == measurement.REFS[profile]
        invocation = next(
            step
            for step in job["steps"]
            if step.get("name") == "Measure the unchanged pytest command"
        )
        assert invocation["env"]["OPENADAPT_TEST_LANE"] == "fast"
        assert f"--profile {profile}" in invocation["run"]
        assert not any(step.get("continue-on-error") for step in job["steps"])
    assert "always() && !cancelled()" in jobs["current"]["if"]


def _with_subtests(rows, count=1):
    call = next(row for row in rows if row.get("phase") == "call")
    for index in range(count):
        report = {
            **call,
            "kind": "subtest",
            "started_at": 1.042,
            "finished_at": 1.048,
            "seconds": 0.006,
        }
        rows.insert(3 + index, report)
    rows[-1]["phase_counts"][f"subtest:call:{call['outcome']}"] = count
    return rows


@pytest.mark.parametrize("kind", ["phase", "subtest"])
@pytest.mark.parametrize(
    "problem",
    [
        "before_session",
        "after_session",
        "overlong_duration",
        "outside_call",
        "huge_duration",
    ],
)
@pytest.mark.parametrize("code", [0, 1])
def test_timing_corruption_cannot_qualify_a_complete_measurement(
    tmp_path, kind, problem, code
):
    rows = _with_subtests(_events(code)) if kind == "subtest" else _events(code)
    identity = {"expected_origins_sha256": {}}
    _write_records(tmp_path / "resources.jsonl", [_sample()])
    _write_records(tmp_path / "phases.jsonl", rows)
    assert (
        measurement._finalize(
            tmp_path, code, 0.2, 0.0, identity, lambda: identity, True
        )
        == code
    )
    assert (
        json.loads((tmp_path / "result.json").read_text())["measurement_complete"]
        is True
    )
    report = next(
        row for row in rows if row.get("kind") == kind and row.get("phase") == "call"
    )
    if problem == "before_session":
        report.update(started_at=0.8, finished_at=0.81)
    elif problem == "after_session":
        report.update(started_at=1.3, finished_at=1.31)
    elif problem == "overlong_duration":
        report["seconds"] = 0.015 if kind == "subtest" else 0.25
    elif problem == "outside_call":
        # This stays inside the session. For ordinary calls it runs before
        # setup finishes; a subtest lies outside its ordinary call interval.
        report.update(started_at=1.015, finished_at=1.018)
    elif problem == "huge_duration":
        report["seconds"] = 1e200
    _write_records(tmp_path / "phases.jsonl", rows)
    assert measurement._finalize(
        tmp_path, code, 0.2, 0.0, identity, lambda: identity, True
    ) == (code or 2)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["measurement_complete"] is False
    assert result["pytest_exit_code"] == code
    assert (
        json.loads((tmp_path / "pytest-exit.json").read_text())["pytest_exit_code"]
        == code
    )
    assert result["observation_error_type"] == "ValueError"


@pytest.mark.parametrize("offset", [0.0, 1e6, 1.8e9])
@pytest.mark.parametrize("scale", [0.001, 1.0, 1000.0])
def test_timing_containment_is_translation_and_scale_consistent(offset, scale):
    rows = _with_subtests(_events())
    for row in rows:
        for name in ("started_at", "finished_at"):
            if name in row:
                row[name] = offset + (row[name] - 1.0) * scale
        if "seconds" in row:
            row["seconds"] *= scale
    wall = 0.2 * scale
    assert measurement._validate_phases(rows, 0, {}, wall)["collected"] == 1
    call = next(
        row for row in rows if row.get("kind") == "phase" and row.get("phase") == "call"
    )
    call["started_at"] = rows[-1]["finished_at"] + wall
    call["finished_at"] = call["started_at"] + 0.01 * scale
    with pytest.raises(ValueError, match="outside enclosing interval"):
        measurement._validate_phases(rows, 0, {}, wall)


def test_worker_duration_sum_cannot_exceed_observed_process_wall():
    rows = _events()
    assert measurement._validate_phases(rows, 0, {}, 0.2)["collected"] == 1
    for row in rows:
        if row.get("kind") == "phase":
            row["seconds"] = 0.08  # Each fits the run; their sum does not.
    with pytest.raises(ValueError, match="worker durations exceed"):
        measurement._validate_phases(rows, 0, {}, 0.2)


def test_parallel_workers_are_not_added_into_a_serial_duration():
    rows = [_events()[0]]
    ids = [f"test_synthetic.py::test_case_{index}" for index in range(8)]
    for index, nodeid in enumerate(ids):
        worker = f"gw{index}"
        selected = _events()
        selected[1].update(
            worker=worker,
            count=len(ids),
            nodeids=ids,
            nodeids_sha256=hashlib.sha256("\n".join(ids).encode()).hexdigest(),
        )
        for row in selected[2:-1]:
            row["worker"] = worker
            if "nodeid" in row:
                row["nodeid"] = nodeid
        rows.extend(selected[1:-1])
    finish = _events()[-1]
    finish.update(
        collected=len(ids),
        phase_counts={
            f"phase:{phase}:passed": len(ids) for phase in ("setup", "call", "teardown")
        },
    )
    rows.append(finish)
    assert sum(row.get("seconds", 0) for row in rows) > 0.2
    assert measurement._validate_phases(rows, 0, {}, 0.2)["workers_observed"] == 8


def test_nested_subtests_are_contained_without_double_counting():
    rows = _with_subtests(_events(), 4)
    assert sum(row["seconds"] for row in rows if row.get("kind") == "subtest") > 0.01
    assert measurement._validate_phases(rows, 0, {}, 0.2)["collected"] == 1


@pytest.mark.parametrize("code", [0, 1])
def test_session_clock_discontinuity_refuses_measurement_without_changing_pytest_exit(
    tmp_path, code
):
    rows = _events(code)
    assert measurement._validate_phases(rows, code, {}, 0.2)["collected"] == 1
    rows[-1]["finished_at"] = 2.0
    _write_records(tmp_path / "phases.jsonl", rows)
    _write_records(tmp_path / "resources.jsonl", [_sample()])
    identity = {"expected_origins_sha256": {}}
    assert measurement._finalize(
        tmp_path, code, 0.2, 0.0, identity, lambda: identity, True
    ) == (code or 2)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["measurement_complete"] is False
    assert result["pytest_exit_code"] == code


@pytest.mark.parametrize("wall", [0, -1, True, None, float("nan"), float("inf")])
def test_temporal_model_requires_a_finite_observed_process_wall(wall):
    with pytest.raises(ValueError, match="invalid process wall time"):
        measurement._validate_phases(_events(), 0, {}, wall)


def test_epoch_rounding_cannot_create_an_unbounded_duration_allowance():
    rows = _events()
    for row in rows:
        for name in ("started_at", "finished_at"):
            if name in row:
                row[name] += 1e100
    with pytest.raises(ValueError, match="timestamp precision"):
        measurement._validate_phases(rows, 0, {}, 0.2)


def test_distinct_nodes_cannot_overlap_on_the_same_worker():
    rows = _events()
    ids = [rows[1]["nodeids"][0], "test_synthetic.py::second_case"]
    rows[1].update(
        count=2,
        nodeids=ids,
        nodeids_sha256=hashlib.sha256("\n".join(ids).encode()).hexdigest(),
    )
    later = copy.deepcopy(rows[2:5])
    for row in later:
        row["nodeid"] = ids[1]
        row["started_at"] += 0.1
        row["finished_at"] += 0.1
    rows[5:5] = later
    rows[-1].update(
        collected=2,
        finished_at=1.19,
        phase_counts={
            f"phase:{phase}:passed": 2 for phase in ("setup", "call", "teardown")
        },
    )
    assert measurement._validate_phases(rows, 0, {}, 0.2)["collected"] == 2
    for row in later:
        row["started_at"] -= 0.095
        row["finished_at"] -= 0.095
    with pytest.raises(ValueError, match="ordinary reports overlap"):
        measurement._validate_phases(rows, 0, {}, 0.2)


def _untimed_events(code=0):
    rows = _with_subtests(_events(code))
    subtest = next(row for row in rows if row["kind"] == "subtest")
    subtest.update(started_at=0, finished_at=0, seconds=0)
    return rows


def _recount(rows):
    rows[-1]["phase_counts"] = dict(
        Counter(
            f"{row['kind']}:{row['phase']}:{row['outcome']}"
            for row in rows
            if row["kind"] in ("phase", "subtest")
        )
    )


@pytest.mark.parametrize("numeric_type", [int, float])
@pytest.mark.parametrize("outcome", ["passed", "failed", "skipped"])
def test_untimed_subtests_preserve_raw_records_and_original_exit(
    tmp_path, numeric_type, outcome
):
    code = int(outcome == "failed")
    rows = _untimed_events(code)
    subtest = next(row for row in rows if row["kind"] == "subtest")
    subtest.update(
        started_at=numeric_type(0),
        finished_at=numeric_type(0),
        seconds=numeric_type(0),
        outcome=outcome,
    )
    _recount(rows)
    original = copy.deepcopy(rows)
    _write_records(tmp_path / "phases.jsonl", rows)
    raw = (tmp_path / "phases.jsonl").read_bytes()
    _write_records(tmp_path / "resources.jsonl", [_sample()])
    identity = {"expected_origins_sha256": {}}
    assert (
        measurement._finalize(
            tmp_path, code, 0.2, 0.0, identity, lambda: identity, True
        )
        == code
    )
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["measurement_complete"] is True
    assert result["pytest_exit_code"] == code
    assert result["subtests_timing_unavailable"] == 1
    assert (tmp_path / "phases.jsonl").read_bytes() == raw
    assert rows == original


@pytest.mark.parametrize("field", ["started_at", "finished_at", "seconds"])
@pytest.mark.parametrize("value", [False, "0", None, -1, float("nan"), float("inf")])
def test_untimed_subtest_sentinel_requires_finite_numeric_zeros(field, value):
    rows = _untimed_events()
    assert (
        measurement._validate_phases(rows, 0, {}, 0.2)["subtests_timing_unavailable"]
        == 1
    )
    next(row for row in rows if row["kind"] == "subtest")[field] = value
    with pytest.raises(ValueError, match="invalid phase timing"):
        measurement._validate_phases(rows, 0, {}, 0.2)


@pytest.mark.parametrize(
    "values", [(0, 0, 0.001), (0, 1.045, 0), (1.045, 0, 0), (0, 0, 1000)]
)
def test_partial_zero_subtest_is_not_an_untimed_sentinel(values):
    rows = _untimed_events()
    assert (
        measurement._validate_phases(rows, 0, {}, 0.2)["subtests_timing_unavailable"]
        == 1
    )
    row = next(row for row in rows if row["kind"] == "subtest")
    row.update(zip(("started_at", "finished_at", "seconds"), values))
    assert measurement._untimed_subtest(row) is False
    with pytest.raises(ValueError, match="invalid phase timing|outside enclosing"):
        measurement._validate_phases(rows, 0, {}, 0.2)


@pytest.mark.parametrize(
    "placement",
    [
        "before_setup",
        "after_call",
        "after_teardown",
        "other_active_node",
        "different_next_node",
    ],
)
def test_untimed_subtest_requires_its_active_parent_in_the_worker_stream(placement):
    rows = _untimed_events()
    assert (
        measurement._validate_phases(rows, 0, {}, 0.2)["subtests_timing_unavailable"]
        == 1
    )
    report = rows.pop(3)
    if placement in ("other_active_node", "different_next_node"):
        other = "test_synthetic.py::test_other"
        rows[1]["nodeids"].append(other)
        rows[1]["count"] = 2
        rows[1]["nodeids_sha256"] = hashlib.sha256(
            "\n".join(rows[1]["nodeids"]).encode()
        ).hexdigest()
        if placement == "other_active_node":
            rows.insert(3, {**rows[2], "nodeid": other})
            rows.insert(4, report)
        else:
            rows.insert(3, report)
            rows.insert(4, {**rows[2], "nodeid": other})
    else:
        position = {"before_setup": 2, "after_call": 4, "after_teardown": 5}[placement]
        rows.insert(position, report)
    expected = (
        "untimed subtest missing its next parent call"
        if placement == "different_next_node"
        else "untimed subtest outside active parent call"
    )
    with pytest.raises(ValueError, match=expected):
        measurement._validate_phases(rows, 0, {}, 0.2)


@pytest.mark.parametrize("problem", ["wrong_worker", "failed_setup", "missing_call"])
def test_untimed_subtest_requires_a_valid_same_scope_parent(problem):
    rows = _untimed_events()
    assert (
        measurement._validate_phases(rows, 0, {}, 0.2)["subtests_timing_unavailable"]
        == 1
    )
    if problem == "wrong_worker":
        rows.insert(2, {**copy.deepcopy(rows[1]), "worker": "gw1"})
        next(row for row in rows if row["kind"] == "subtest")["worker"] = "gw1"
        expected = "untimed subtest outside active parent call"
    elif problem == "failed_setup":
        rows[2]["outcome"] = "failed"
        expected = "untimed subtest outside active parent call"
    else:
        rows = [
            row
            for row in rows
            if not (row["kind"] == "phase" and row.get("phase") == "call")
        ]
        _recount(rows)
        expected = "untimed subtest missing its next parent call"
    with pytest.raises(ValueError, match=expected):
        measurement._validate_phases(rows, 0, {}, 0.2)


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_zero_timing_ordinary_phase_is_still_impossible(phase):
    rows = _events()
    assert measurement._validate_phases(rows, 0, {}, 0.2)["collected"] == 1
    report = next(row for row in rows if row.get("phase") == phase)
    report.update(started_at=0, finished_at=0, seconds=0)
    assert measurement._untimed_subtest(report) is False
    with pytest.raises(ValueError, match="outside enclosing"):
        measurement._validate_phases(rows, 0, {}, 0.2)


def test_real_epoch_zero_duration_subtest_remains_timed():
    rows = _with_subtests(_events())
    report = next(row for row in rows if row["kind"] == "subtest")
    report.update(started_at=1.045, finished_at=1.045, seconds=0)
    assert (
        measurement._validate_phases(rows, 0, {}, 0.2)["subtests_timing_unavailable"]
        == 0
    )


@pytest.mark.parametrize("failure", [False, True])
def test_genuine_unittest_subtests_under_xdist_have_unavailable_timing(
    tmp_path, failure
):
    source = """
import unittest

class TestSynthetic(unittest.TestCase):
    def test_subtests(self):
        for index in range(2):
            with self.subTest(index=index):
                self.assertTrue(index != 1 or not FAIL)
""".replace("FAIL", repr(failure))
    (tmp_path / "test_synthetic.py").write_text(source)
    output = tmp_path / "observations"
    output.mkdir()
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(Path(measurement.__file__).parent),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        OPENADAPT_SHARD_MEASUREMENT_OUTPUT=str(output),
        OPENADAPT_SHARD_MEASUREMENT_SUBJECT=str(tmp_path),
    )
    started = time.monotonic()
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "xdist.plugin",
            "-p",
            "flow_shard_measurement",
            "-n",
            "2",
            "--dist",
            "loadfile",
            "-q",
            "test_synthetic.py",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert run.returncode == int(failure), run.stdout + run.stderr
    events = measurement._records(output / "phases.jsonl")
    result = measurement._validate_phases(
        events, run.returncode, {}, time.monotonic() - started
    )
    reports = [row for row in events if row["kind"] == "subtest"]
    assert len(reports) == 2
    assert all(
        (row["started_at"], row["finished_at"], row["seconds"]) == (0, 0, 0)
        for row in reports
    )
    assert sum(row["outcome"] == "failed" for row in reports) == int(failure)
    assert result["collected"] == 1
    assert result["workers_observed"] == 2
    assert result["subtests_timing_unavailable"] == 2


@pytest.mark.parametrize("code", [0, 1])
@pytest.mark.parametrize(
    "problem", ["before_setup_end", "after_session", "duration", "missing_call"]
)
def test_untimed_subtest_does_not_bless_an_invalid_parent(tmp_path, code, problem):
    rows = _untimed_events(code)
    assert (
        measurement._validate_phases(rows, code, {}, 0.2)["subtests_timing_unavailable"]
        == 1
    )
    call = next(
        row for row in rows if row["kind"] == "phase" and row["phase"] == "call"
    )
    if problem == "before_setup_end":
        call["started_at"] = 1.025
    elif problem == "after_session":
        call["finished_at"] = 1.2
    elif problem == "duration":
        call["seconds"] = 1000
    else:
        rows.remove(call)
        _recount(rows)
    _write_records(tmp_path / "phases.jsonl", rows)
    _write_records(tmp_path / "resources.jsonl", [_sample()])
    identity = {"expected_origins_sha256": {}}
    assert measurement._finalize(
        tmp_path, code, 0.2, 0.0, identity, lambda: identity, True
    ) == (code or 2)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["measurement_complete"] is False
    assert result["pytest_exit_code"] == code
    assert result["observation_error_type"] == "ValueError"
