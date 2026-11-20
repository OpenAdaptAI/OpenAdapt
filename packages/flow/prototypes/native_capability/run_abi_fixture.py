"""One owned synthetic ABI build/run. No UIA client/provider or Flow import."""

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

# Immutable b0 subject bytes, not an asserted native qualification flag.
FROZEN_SUBJECT = {
    "sdk_capability.cpp": "e6e38e71c14b5d73aac7c1f68dc9e366450aba71b292884395cb4d2ef58a3639",
    "sdk_capability.h": "35800bddab432003a5677ee07c25d0881ff1a653692cad4a37a75baa66c2eadb",
}
PROCESS_SUPPORT_SHA256 = (
    "c731e2d758c98d7be512be487e81161cc81847f9ac15082a80504ea729758b90"
)
OWNED_WRAPPER_FLAG = "--owned-abi-child"
SOURCE_FILES = (
    *FROZEN_SUBJECT,
    "abi_fixture.cpp",
    "fixture_element_stubs.inc",
    "fixture_patterns_stubs.inc",
    "compile_fixture.cmd",
    "run_abi_fixture.py",
)


def hashes(root):
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in SOURCE_FILES
    }


def preflight(root, vcvars, sdk):
    if sys.platform != "win32":
        raise RuntimeError("windows_only")
    if not root.is_dir() or " " in str(root) or (root / "out").exists():
        raise RuntimeError("fresh_owned_directory_required")
    if not vcvars.is_file() or vcvars.name.lower() != "vcvars64.bat":
        raise RuntimeError("existing_compiler_environment_required")
    if sdk != "10.0.22621.0":
        raise RuntimeError("inventory_sdk_mismatch")
    before = hashes(root)
    if any(before[name] != expected for name, expected in FROZEN_SUBJECT.items()):
        raise RuntimeError("frozen_subject_mismatch")
    return before


def load_process_support(path):
    if path.name != "install_playwright_browser.py":
        raise RuntimeError("process_support_name_mismatch")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PROCESS_SUPPORT_SHA256:
        raise RuntimeError("process_support_source_mismatch")
    spec = importlib.util.spec_from_file_location("owned_fixture_process_support", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("process_support_spec_missing")
    module = importlib.util.module_from_spec(spec)
    exec(compile(raw, str(path), "exec", dont_inherit=True), module.__dict__)
    if (
        Path(module.__file__).resolve() != path.resolve()
        or Path(module.__spec__.origin).resolve() != path.resolve()
    ):
        raise RuntimeError("process_support_origin_mismatch")
    if path.read_bytes() != raw:
        raise RuntimeError("process_support_changed")
    return module


def owned_gated_child(command):
    if not command or sys.stdin.buffer.readline() != b"START\n":
        return 125
    try:
        return subprocess.run(
            command, check=False, creationflags=subprocess.CREATE_NO_WINDOW
        ).returncode
    except BaseException:
        return 126


def bounded(command, root, log, timeout, deadline, support):
    """Reuse the existing retained Job and START-gated wrapper, no retry."""
    result = {
        "started": False,
        "timeout": False,
        "exit_code": None,
        "owned_process_stopped": True,
        "job_assigned": False,
        "job_empty": False,
        "job_closed": False,
    }
    ledger = root / "out" / (log.stem + "-owned-process.json")
    process = None
    job = None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result["timeout"] = True
            return result
        job = support._WindowsJob()
        with log.open("wb") as output:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    str(Path(__file__).resolve()),
                    OWNED_WRAPPER_FLAG,
                    *command,
                ],
                cwd=root,
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            result.update(started=True, pid=process.pid, owned_process_stopped=False)
            ledger.write_text(json.dumps(result, indent=2) + "\n")
            result["job_assigned"] = job.assign(process)
            if not result["job_assigned"] or process.stdin is None:
                result["closed_error"] = "owned_job_assignment_refused"
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result["timeout"] = True
                return result
            process.stdin.write(b"START\n")
            process.stdin.close()
            try:
                result["exit_code"] = process.wait(timeout=min(timeout, remaining))
            except subprocess.TimeoutExpired:
                result["timeout"] = True
            except BaseException:
                result["closed_error"] = "owned_wait_failed"
    except BaseException:
        result["closed_error"] = "owned_launch_failed"
    finally:
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except BaseException:
                result.setdefault("closed_error", "owned_pipe_close_failed")
        if (
            process is not None
            and not result["job_assigned"]
            and process.poll() is None
        ):
            # No START was sent, so this still-gated wrapper has no children.
            try:
                process.kill()  # Python's retained process handle, not a PID search.
            except BaseException:
                result["cleanup_error"] = "owned_wrapper_stop_failed"
        if job is not None:
            try:
                result["job_empty"] = job.terminate_and_verify_empty()
            except BaseException:
                result["cleanup_error"] = "owned_job_empty_unconfirmed"
            finally:
                try:
                    result["job_closed"] = job.close()  # KILL_ON_JOB_CLOSE fail-safe.
                except BaseException:
                    result.setdefault("cleanup_error", "owned_job_close_unconfirmed")
        if process is not None:
            try:
                result["exit_code"] = process.wait(timeout=5)
            except BaseException:
                result.setdefault("cleanup_error", "owned_wrapper_exit_unconfirmed")
            result["owned_process_stopped"] = process.poll() is not None
        ledger.write_text(json.dumps(result, indent=2) + "\n")
    return result


def completed_ok(result):
    return (
        result.get("started") is True
        and result.get("timeout") is False
        and type(result.get("exit_code")) is int
        and result["exit_code"] == 0
        and result.get("owned_process_stopped") is True
        and result.get("job_assigned") is True
        and result.get("job_empty") is True
        and result.get("job_closed") is True
        and "closed_error" not in result
        and "cleanup_error" not in result
    )


def validate_rows(rows):
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
    expected = {(pattern, name, -1) for pattern in range(3) for name in names}
    expected |= {
        (pattern, "guard_refused", stage) for pattern in range(3) for stage in range(4)
    }
    expected |= {(pattern, "unsupported_guard_refused", 4) for pattern in range(3)}
    expected.add((99, "invalid_pattern", -1))
    if (
        type(rows) is not list
        or len(rows) != 66
        or any(type(row) is not dict for row in rows)
    ):
        return False
    if any(
        sum(key in row for key in ("case", "summary", "oracle_controls")) != 1
        for row in rows
    ):
        return False
    if any(row.get("passed") is not True for row in rows):
        return False
    cases = [row for row in rows if "case" in row]
    summaries = [
        row for row in rows if type(row.get("summary")) is int and row["summary"] == 1
    ]
    oracles = [
        row
        for row in rows
        if type(row.get("oracle_controls")) is int and row["oracle_controls"] == 1
    ]
    if len(cases) != 64 or len(summaries) != 1 or len(oracles) != 1:
        return False
    seen = set()
    for row in cases:
        if (
            type(row.get("pattern")) is not int
            or type(row.get("refused_stage")) is not int
            or type(row.get("case")) is not str
        ):
            return False
        key = (row["pattern"], row["case"], row["refused_stage"])
        if key not in expected or key in seen:
            return False
        seen.add(key)
    summary = summaries[0]
    return (
        seen == expected
        and type(summary.get("cases")) is int
        and summary["cases"] == 64
        and summary.get("live_uia_provider_used") is False
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--vcvars",
        type=Path,
        default=Path(
            r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
        ),
    )
    parser.add_argument("--sdk", required=True)
    parser.add_argument("--process-support", type=Path, required=True)
    options = parser.parse_args()
    root = Path(__file__).resolve().parent
    before = preflight(root, options.vcvars, options.sdk)
    compiler = (
        options.vcvars.parents[2] / "Tools/MSVC/14.40.33807/bin/Hostx64/x64/cl.exe"
    )
    sdk_header = (
        Path(r"C:\Program Files (x86)\Windows Kits\10\Include")
        / options.sdk
        / "um/UIAutomationClient.h"
    )
    toolchain_before = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (compiler, sdk_header)
    }
    if (
        toolchain_before[str(sdk_header)].upper()
        != "3B9C85781322FD94899DC7C69027924BCE197DC6F5601B8044F39962F7E1D5C8"
    ):
        raise RuntimeError("inventory_header_changed")
    support = load_process_support(options.process_support.resolve())
    before["process_support"] = PROCESS_SUPPORT_SHA256
    out = root / "out"
    out.mkdir()
    receipt = {
        "schema": "openadapt.owned-com-abi-fixture/v1",
        "python": sys.version,
        "executable": sys.executable,
        "before": before,
        "sdk": options.sdk,
        "toolchain_before": toolchain_before,
        "live_uia_provider_used": False,
        "subject": "b0ab00afcba6d3f78890767227b1bd9241f27b22",
    }
    deadline = time.monotonic() + 130
    try:
        receipt["compile"] = bounded(
            [
                r"C:\Windows\System32\cmd.exe",
                "/d",
                "/c",
                str(root / "compile_fixture.cmd"),
                str(options.vcvars),
                options.sdk,
                str(compiler),
            ],
            root,
            out / "compile.log",
            90,
            deadline,
            support,
        )
        if not completed_ok(receipt["compile"]):
            return 2
        receipt["executable_sha256"] = hashlib.sha256(
            (out / "fixture.exe").read_bytes()
        ).hexdigest()
        receipt["fixture"] = bounded(
            [str(out / "fixture.exe")], root, out / "cases.jsonl", 20, deadline, support
        )
        if not completed_ok(receipt["fixture"]):
            return 3
        rows = [
            json.loads(line) for line in (out / "cases.jsonl").read_text().splitlines()
        ]
        if not validate_rows(rows):
            return 4
        receipt["validated_cases"] = 64
        receipt["oracle_controls"] = 1
        return 0
    finally:
        receipt["toolchain_after"] = {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (compiler, sdk_header)
        }
        receipt["toolchain_unchanged"] = (
            receipt["toolchain_before"] == receipt["toolchain_after"]
        )
        receipt["after"] = hashes(root)
        receipt["after"]["process_support"] = hashlib.sha256(
            options.process_support.read_bytes()
        ).hexdigest()
        receipt["source_unchanged"] = receipt["before"] == receipt["after"]
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if not receipt["source_unchanged"] or not receipt["toolchain_unchanged"]:
            raise RuntimeError("source_changed_during_fixture")


if __name__ == "__main__":
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == OWNED_WRAPPER_FLAG:
            outcome = owned_gated_child(sys.argv[2:])
        else:
            outcome = main()
    except BaseException:
        sys.stderr.write('{"closed_error":"fixture_runner_refused"}\n')
        outcome = 10
    raise SystemExit(outcome)
