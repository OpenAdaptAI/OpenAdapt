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
                "started_at": 2.0 + index,
                "finished_at": 2.5 + index,
                "seconds": 0.5,
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
            "finished_at": 6.0,
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
        assert measurement._validate_phases(rows, 0, expected_origins)["collected"] == 1
        rows[-2]["origins"]["openadapt_flow"] = {"status": "unexpected_origin"}
    elif problem == "false_success":
        rows = _events(1)
        rows[-1]["exit_code"] = 0
    with pytest.raises(
        ValueError,
        match="origin differs from subject" if problem == "origin_drift" else None,
    ):
        measurement._validate_phases(rows, 0, expected_origins)


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
    result = measurement._validate_phases(events, run.returncode, {})
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
        measurement._validate_phases(rows, 0, {})


def test_finish_counts_cannot_bless_a_removed_call():
    rows = _events()
    rows = [row for row in rows if row.get("phase") != "call"]
    del rows[-1]["phase_counts"]["phase:call:passed"]
    with pytest.raises(ValueError, match="missing or duplicate phase"):
        measurement._validate_phases(rows, 0, {})


def test_workflow_keeps_the_fixed_pair_and_original_test_policy():
    import yaml

    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load(
        (root / ".github/workflows/flow-shard-measurement.yml").read_text()
    )
    trigger = workflow.get("on", workflow.get(True))
    assert set(trigger) == {"pull_request"}
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
        assert "github.event.pull_request.draft == false" in job["if"]
        assert "github.head_ref == 'agent/2078'" in job["if"]
        assert (
            "github.event.pull_request.head.repo.full_name == github.repository"
            in job["if"]
        )
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
