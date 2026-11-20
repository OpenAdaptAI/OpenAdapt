"""Pure runner controls. All process objects are fakes; no native command runs."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import run_abi_fixture as runner


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.sys, "platform", "win32")
    root = tmp_path / "owned"
    root.mkdir()
    for name in runner.SOURCE_FILES:
        shutil.copyfile(Path(runner.__file__).parent / name, root / name)
    vcvars = tmp_path / "vcvars64.bat"
    vcvars.write_text("own stand-in, never executed")
    return root, vcvars


def test_preflight_requires_exact_frozen_subject_and_fresh_output(candidate):
    root, vcvars = candidate
    original = runner.preflight(root, vcvars, "10.0.22621.0")
    assert original["sdk_capability.cpp"] == runner.FROZEN_SUBJECT["sdk_capability.cpp"]
    (root / "sdk_capability.cpp").write_text("changed owned subject")
    with pytest.raises(RuntimeError, match="frozen_subject_mismatch"):
        runner.preflight(root, vcvars, "10.0.22621.0")


def test_old_output_is_not_reused(candidate):
    root, vcvars = candidate
    (root / "out").mkdir()
    with pytest.raises(RuntimeError, match="fresh_owned_directory_required"):
        runner.preflight(root, vcvars, "10.0.22621.0")


def test_uninventoried_sdk_or_missing_compiler_refuse(candidate):
    root, vcvars = candidate
    with pytest.raises(RuntimeError, match="inventory_sdk_mismatch"):
        runner.preflight(root, vcvars, "10.0.19041.0")
    vcvars.unlink()
    with pytest.raises(RuntimeError, match="existing_compiler_environment_required"):
        runner.preflight(root, vcvars, "10.0.22621.0")


class OwnedProcess:
    pid = 90123  # Synthetic integer; never passed to a real process API.

    def __init__(self, first="ok", stopped_after_cleanup=True):
        self.first = first
        self.stopped = False
        self.waits = 0
        self.stopped_after_cleanup = stopped_after_cleanup

    def wait(self, timeout):
        self.waits += 1
        if self.waits == 1:
            if self.first == "timeout":
                raise subprocess.TimeoutExpired("owned-fake", timeout)
            if self.first == "error":
                raise OSError("own fake")
            self.stopped = True
            return 0
        if self.stopped_after_cleanup:
            self.stopped = True
            return 0
        raise subprocess.TimeoutExpired("owned-fake", timeout)

    def poll(self):
        return 0 if self.stopped else None


@pytest.mark.parametrize(
    "mode", ["ok", "timeout", "error", "cleanup_failure", "assign_refused"]
)
def test_owned_job_cleanup_is_closed_without_relaunch(tmp_path, monkeypatch, mode):
    (tmp_path / "out").mkdir()
    process = OwnedProcess(
        "timeout" if mode == "cleanup_failure" else mode,
        stopped_after_cleanup=mode != "cleanup_failure",
    )
    launches, events = [], []

    class Pipe:
        def write(self, value):
            events.append(("start", value))

        def close(self):
            events.append(("pipe_close",))

    process.stdin = Pipe()
    process.kill = lambda: setattr(process, "stopped", True)

    class Job:
        def assign(self, candidate):
            assert candidate is process
            events.append(("assign",))
            return mode != "assign_refused"

        def terminate_and_verify_empty(self):
            events.append(("terminate_verify",))
            if mode == "cleanup_failure":
                raise OSError("own fake job")
            process.stopped = True
            return True

        def close(self):
            events.append(("close",))
            return mode != "cleanup_failure"

    class Support:
        __file__ = "owned_support_standin.py"
        WINDOWS_JOB_WRAPPER_FLAG = "--windows-job-wrapper"
        _WindowsJob = Job

    def launch(*args, **kwargs):
        launches.append(args)
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    monkeypatch.setattr(runner.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    result = runner.bounded(
        ["synthetic"],
        tmp_path,
        tmp_path / "out" / "test.log",
        1,
        time.monotonic() + 10,
        Support,
    )
    assert len(launches) == 1
    assert launches[0][0][1:5] == [
        "-I",
        "-S",
        str(Path(runner.__file__).resolve()),
        runner.OWNED_WRAPPER_FLAG,
    ]
    assert runner.completed_ok(result) is (mode == "ok")
    assert events.count(("terminate_verify",)) == 1 and events.count(("close",)) == 1
    assert (
        events.index(("assign",))
        < events.index(("terminate_verify",))
        < events.index(("close",))
    )
    if mode == "assign_refused":
        assert ("start", b"START\n") not in events
    else:
        assert events.index(("assign",)) < events.index(("start", b"START\n"))
    if mode == "cleanup_failure":
        assert result["owned_process_stopped"] is False
        assert result["cleanup_error"] == "owned_job_empty_unconfirmed"
        assert result["job_empty"] is False and result["job_closed"] is False
    assert (
        json.loads((tmp_path / "out" / "test-owned-process.json").read_text()) == result
    )


def test_expired_total_deadline_starts_nothing(tmp_path, monkeypatch):
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start")
    )
    result = runner.bounded(
        ["synthetic"], tmp_path, tmp_path / "out" / "test.log", 1, time.monotonic() - 1, None
    )
    assert not runner.completed_ok(result)
    assert result["timeout"] is True and result["started"] is False


@pytest.mark.parametrize(
    "changed", ["closed_error", "cleanup_error", "job_empty", "job_closed", "exit_bool"]
)
def test_no_ambiguous_completion_can_advance_to_fixture(changed):
    result = {
        "started": True,
        "timeout": False,
        "exit_code": 0,
        "owned_process_stopped": True,
        "job_assigned": True,
        "job_empty": True,
        "job_closed": True,
    }
    assert runner.completed_ok(result)
    if changed == "exit_bool":
        result["exit_code"] = False
    elif changed in ("job_empty", "job_closed"):
        result[changed] = False
    else:
        result[changed] = True
    assert not runner.completed_ok(result)


def test_process_support_poisoned_source_is_refused_before_exec(tmp_path):
    path = tmp_path / "install_playwright_browser.py"
    path.write_text("raise AssertionError('must not execute')")
    with pytest.raises(RuntimeError, match="process_support_source_mismatch"):
        runner.load_process_support(path)


@pytest.mark.parametrize("line", [b"", b"NO\n", b"START\r\n", b"START\n"])
def test_owned_gated_child_requires_exact_start_and_no_window(monkeypatch, line):
    import io
    import types

    monkeypatch.setattr(
        runner.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(line))
    )
    monkeypatch.setattr(runner.subprocess, "CREATE_NO_WINDOW", 77, raising=False)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.owned_gated_child(["synthetic-child"])
    if line == b"START\n":
        assert result == 0 and calls == [
            (["synthetic-child"], {"check": False, "creationflags": 77})
        ]
    else:
        assert result == 125 and not calls


def complete_rows():
    names = (
        "supported",
        "explicit_unsupported",
        "normal_null",
        "s_false_null",
        "s_false_output",
        "failure_null",
        "failure_output",
        "unsupported_output",
        "qi_no_interface",
        "qi_failure_output",
        "qi_normal_null",
        "qi_s_false_output",
        "qi_not_supported",
        "invalid_abi",
        "null_element",
        "null_callback",
    )
    cases = [
        {"pattern": p, "case": name, "refused_stage": -1, "passed": True}
        for p in range(3)
        for name in names
    ]
    cases += [
        {"pattern": p, "case": "guard_refused", "refused_stage": stage, "passed": True}
        for p in range(3)
        for stage in range(4)
    ]
    cases += [
        {
            "pattern": p,
            "case": "unsupported_guard_refused",
            "refused_stage": 4,
            "passed": True,
        }
        for p in range(3)
    ]
    cases += [
        {"pattern": 99, "case": "invalid_pattern", "refused_stage": -1, "passed": True}
    ]
    return cases + [
        {"oracle_controls": 1, "passed": True},
        {"summary": 1, "cases": 64, "live_uia_provider_used": False, "passed": True},
    ]


@pytest.mark.parametrize(
    "fault",
    ["duplicate", "missing", "extra", "bool_pattern", "bool_summary", "truthy_pass"],
)
def test_duplicate_missing_extra_or_wrong_typed_census_never_passes(fault):
    rows = complete_rows()
    assert runner.validate_rows(rows)
    if fault == "duplicate":
        rows[1] = rows[0].copy()
    elif fault == "missing":
        rows.pop(0)
    elif fault == "extra":
        rows.append(rows[0].copy())
    elif fault == "bool_pattern":
        rows[0]["pattern"] = False
    elif fault == "bool_summary":
        rows[-1]["summary"] = True
    else:
        rows[0]["passed"] = 1
    assert not runner.validate_rows(rows)


def test_mixed_roles_and_unclassified_row_cannot_preserve_count_claim():
    rows = complete_rows()
    rows[0]["oracle_controls"] = 1
    rows[-2] = {"passed": True}
    assert not runner.validate_rows(rows)
