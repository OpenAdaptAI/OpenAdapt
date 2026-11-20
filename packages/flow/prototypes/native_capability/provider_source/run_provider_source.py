"""Inert until separately authorized: one owned SDK provider case, never retry."""

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

ABI_RUNNER_SHA256 = "6ed593c74f06518d4e9c7fc73626a362b8da1b1ee30ec53b635ebcd93062ed39"
SUBJECT = {
    "sdk_capability.cpp": "e6e38e71c14b5d73aac7c1f68dc9e366450aba71b292884395cb4d2ef58a3639",
    "sdk_capability.h": "35800bddab432003a5677ee07c25d0881ff1a653692cad4a37a75baa66c2eadb",
    "run_abi_fixture.py": ABI_RUNNER_SHA256,
}
SOURCE_FILES = (
    *SUBJECT,
    "io_capture_control.py",
    "provider_source/sdk_checkpoint.h",
    "provider_source/sdk_checkpoint.cpp",
    "provider_source/provider_fixture.cpp",
    "provider_source/provider_audit.py",
    "provider_source/run_provider_source.py",
    "provider_source/compile_provider.cmd",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_source(path, name, expected=None):
    raw = path.read_bytes()
    if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
        raise RuntimeError("frozen_source_mismatch")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("source_spec_missing")
    module = importlib.util.module_from_spec(spec)
    exec(compile(raw, str(path), "exec", dont_inherit=True), module.__dict__)
    if (
        Path(module.__file__).resolve() != path.resolve()
        or Path(module.__spec__.origin).resolve() != path.resolve()
        or path.read_bytes() != raw
    ):
        raise RuntimeError("source_origin_changed")
    return module


def execute(root, out, support, abi, audit, options, compiler, receipt):
    deadline = time.monotonic() + 130
    receipt["capture_job"] = abi.bounded(
        [sys.executable, "-I", "-S", str(root / "io_capture_control.py")],
        root,
        out / "capture-positive.stdout",
        5,
        deadline,
        support,
        out / "capture-positive.stderr",
    )
    if not abi.completed_ok(receipt["capture_job"]):
        return 2
    receipt["capture"] = abi.capture_control_evidence(
        out / "capture-positive.stdout", out / "capture-positive.stderr"
    )
    if receipt["capture"]["validated"] is not True:
        return 2
    receipt["compile"] = abi.bounded(
        [
            r"C:\Windows\System32\cmd.exe",
            "/d",
            "/c",
            str(root / "provider_source/compile_provider.cmd"),
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
    if (
        not abi.completed_ok(receipt["compile"])
        or (out / "compile.log").stat().st_size == 0
    ):
        return 3
    executable = out / "provider_fixture.exe"
    receipt["executable_sha256"] = digest(executable)
    receipt["case_job"] = abi.bounded(
        [str(executable), str(options.case), str(options.pattern)],
        root,
        out / "provider.jsonl",
        20,
        deadline,
        support,
        out / "provider.stderr",
    )
    # Nonzero/stderr/incomplete capture is retained and cannot earn success.
    if (
        not abi.completed_ok(receipt["case_job"])
        or (out / "provider.stderr").read_bytes()
    ):
        return 4
    rows = [
        json.loads(line) for line in (out / "provider.jsonl").read_text().splitlines()
    ]
    receipt["category_audit"] = audit.audit(rows, options.case, options.pattern)
    return 0 if receipt["category_audit"] else 5


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=int, choices=range(16), required=True)
    parser.add_argument("--pattern", type=int, choices=range(3), required=True)
    parser.add_argument("--sdk", choices=["10.0.22621.0"], required=True)
    parser.add_argument("--process-support", type=Path, required=True)
    parser.add_argument(
        "--vcvars",
        type=Path,
        default=Path(
            r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
        ),
    )
    options = parser.parse_args()
    if sys.platform != "win32":
        raise RuntimeError("windows_only_no_local_native_execution")
    root = Path(__file__).resolve().parent.parent
    if " " in str(root) or (root / "out").exists() or not options.vcvars.is_file():
        raise RuntimeError("fresh_owned_tree_and_existing_compiler_required")
    before = {name: digest(root / name) for name in SOURCE_FILES}
    if any(before[name] != expected for name, expected in SUBJECT.items()):
        raise RuntimeError("frozen_subject_changed")
    abi = load_source(
        root / "run_abi_fixture.py", "provider_owned_abi", ABI_RUNNER_SHA256
    )
    audit = load_source(
        root / "provider_source/provider_audit.py", "provider_owned_audit"
    )
    support = abi.load_process_support(options.process_support.resolve())
    compiler = (
        options.vcvars.parents[2] / "Tools/MSVC/14.40.33807/bin/Hostx64/x64/cl.exe"
    )
    sdk_root = (
        Path(r"C:\Program Files (x86)\Windows Kits\10\Include") / options.sdk / "um"
    )
    # These exact on-disk identities do not prove the loaded UIA core/module closure.
    tools = [
        compiler,
        *(
            sdk_root / name
            for name in (
                "UIAutomationClient.h",
                "UIAutomationCore.h",
                "UIAutomationCoreApi.h",
            )
        ),
    ]
    toolchain = {str(path): digest(path) for path in tools}
    out = root / "out"
    out.mkdir()
    receipt = {
        "schema": "openadapt.owned-provider-source-fixture/v1",
        "case": options.case,
        "pattern": options.pattern,
        "source_before": before,
        "toolchain_before": toolchain,
        "process_support_sha256": abi.PROCESS_SUPPORT_SHA256,
        "abi_subject": "b0ab00afcba6d3f78890767227b1bd9241f27b22",
        "capture_harness": "8114d9a8680c261c2252c083dac0b0a196820462",
        "runtime_adoption": False,
    }
    try:
        return execute(root, out, support, abi, audit, options, compiler, receipt)
    finally:
        receipt["source_after"] = {name: digest(root / name) for name in SOURCE_FILES}
        receipt["toolchain_after"] = {str(path): digest(path) for path in tools}
        receipt["process_support_after"] = digest(options.process_support)
        receipt["unchanged"] = (
            receipt["source_before"] == receipt["source_after"]
            and receipt["toolchain_before"] == receipt["toolchain_after"]
            and receipt["process_support_sha256"] == receipt["process_support_after"]
        )
        (out / "provider-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if not receipt["unchanged"]:
            raise RuntimeError("source_or_toolchain_changed")


if __name__ == "__main__":
    try:
        outcome = main()
    except BaseException:
        sys.stderr.write('{"closed_error":"owned_provider_refused"}\n')
        outcome = 10
    raise SystemExit(outcome)
