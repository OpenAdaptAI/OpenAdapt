#!/usr/bin/env python3
"""Verify the public launcher quickstart from wheel install through uninstall."""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import venv
from pathlib import Path
from typing import Sequence

_UNHANDLED_RUNTIME_MARKERS = (
    "Task was destroyed but it is pending!",
    "Future exception was never retrieved",
)
_LAZY_BROWSER_INSTALL_NOTICE = "Downloading the Chromium browser OpenAdapt needs"


def _resolve_wheel(pattern: str, distribution: str) -> Path:
    matches = [Path(item).resolve() for item in glob.glob(pattern)]
    if len(matches) != 1:
        raise ValueError(
            f"{distribution} wheel pattern must match exactly one file; "
            f"{pattern!r} matched {len(matches)}: {matches}"
        )
    wheel = matches[0]
    if wheel.suffix != ".whl":
        raise ValueError(f"{distribution} artifact is not a wheel: {wheel}")
    return wheel


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _console(root: Path) -> Path:
    return root / ("Scripts/openadapt.exe" if os.name == "nt" else "bin/openadapt")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log: Path,
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    printable = subprocess.list2cmdline(list(command))
    print(f"\n$ {printable}", flush=True)
    child_env = env.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        list(command),
        cwd=cwd,
        env=child_env,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print(result.stdout, end="", flush=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"$ {printable}\n\n{result.stdout}", encoding="utf-8")
    marker = next(
        (item for item in _UNHANDLED_RUNTIME_MARKERS if item in result.stdout), None
    )
    if marker is not None:
        raise RuntimeError(
            f"{printable} emitted an unhandled runtime error ({marker}); see {log}"
        )
    if result.returncode != expected:
        raise RuntimeError(
            f"{printable} exited {result.returncode}; expected {expected} (see {log})"
        )
    return result


def _load_object(path: Path) -> dict:
    if not path.is_file():
        raise AssertionError(f"missing JSON artifact: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"JSON artifact is not an object: {path}")
    return value


def _browser_present(
    python: Path,
    *,
    cwd: Path,
    env: dict[str, str],
    log: Path,
) -> bool:
    """Inspect Flow's exact Playwright browser without launching it."""
    result = _run(
        [
            str(python),
            "-c",
            (
                "from openadapt_flow._browser_setup import _chromium_present; "
                "print('present' if _chromium_present() else 'absent')"
            ),
        ],
        cwd=cwd,
        env=env,
        log=log,
    )
    state = result.stdout.strip()
    if state not in {"present", "absent"}:
        raise AssertionError(f"unexpected Chromium probe output: {state!r}")
    return state == "present"


def _install_browser_system_dependencies(
    python: Path,
    *,
    cwd: Path,
    env: dict[str, str],
    log: Path,
) -> None:
    """Install Linux packages only, leaving Chromium for the public command."""
    _run(
        [
            str(python),
            "-m",
            "playwright",
            "install-deps",
            "chromium",
        ],
        cwd=cwd,
        env=env,
        log=log,
    )


def _inspect_quickstart(root: Path) -> dict[str, object]:
    run = root / "run"
    report = _load_object(run / "report.json")
    if report.get("execution_outcome") != "VERIFIED":
        raise AssertionError(
            f"quickstart outcome is {report.get('execution_outcome')!r}, not VERIFIED"
        )
    if report.get("execution_profile") != "standard":
        raise AssertionError(
            f"quickstart profile is {report.get('execution_profile')!r}, not standard"
        )
    if report.get("transaction_outcome") != "VERIFIED":
        raise AssertionError("quickstart did not verify the transaction outcome")
    if report.get("model_calls") != 0:
        raise AssertionError("quickstart made a model call")

    envelope = report.get("outcome_envelope") or {}
    required = int((envelope.get("required_contracts") or {}).get("effect") or 0)
    passed = int((envelope.get("passed_contracts") or {}).get("effect") or 0)
    if required < 1 or passed != required:
        raise AssertionError(f"quickstart effect coverage is {passed}/{required}")

    receipt = _load_object(run / "receipt.json")
    if receipt.get("outcome") != "VERIFIED":
        raise AssertionError("quickstart receipt is not VERIFIED")
    if receipt.get("provenance") != "synthetic-tutorial":
        raise AssertionError("quickstart receipt has the wrong provenance")
    for path in (run / "REPORT.md", run / "receipt.png", run / "receipt.md"):
        if not path.is_file():
            raise AssertionError(f"quickstart is missing {path.name}")

    return {
        "outcome": "VERIFIED",
        "profile": "standard",
        "transaction_outcome": "VERIFIED",
        "effect_contracts": passed,
        "model_calls": 0,
        "receipt_emitted": True,
    }


def run_lifecycle(
    launcher_wheel: Path,
    work_dir: Path,
    *,
    flow_wheel: Path | None,
    browser_system_deps: bool,
    source_revision: str | None,
) -> dict[str, object]:
    if work_dir.exists():
        raise FileExistsError(f"work directory already exists: {work_dir}")
    work_dir.mkdir(parents=True)
    venv_dir = work_dir / "venv"
    artifacts = work_dir / "artifacts"
    logs = work_dir / "logs"
    artifacts.mkdir()
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)

    python = _venv_python(venv_dir)
    console = _console(venv_dir)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["OPENADAPT_FLOW_SCRUB"] = "off"
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(work_dir / "playwright-browsers")

    summary: dict[str, object] = {
        "launcher_wheel": launcher_wheel.name,
        "launcher_wheel_sha256": _sha256(launcher_wheel),
        "flow_wheel": flow_wheel.name if flow_wheel else "resolved-release",
        "flow_wheel_sha256": _sha256(flow_wheel) if flow_wheel else None,
        "platform": sys.platform,
        "source_revision": source_revision or "local-unbound",
        "browser_preflight": {
            "system_dependencies": "not-requested",
            "doctor": "not-run",
            "chromium_present_before_quickstart": None,
            "chromium_present_after_quickstart": None,
            "lazy_install_performed": False,
            "lazy_install_notice_seen": False,
        },
    }
    browser_preflight = summary["browser_preflight"]
    assert isinstance(browser_preflight, dict)
    installed = False
    try:
        if flow_wheel is not None:
            _run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    f"{flow_wheel}[browser,hosted]",
                ],
                cwd=artifacts,
                env=env,
                log=logs / "01-install-flow.log",
            )
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                str(launcher_wheel),
            ],
            cwd=artifacts,
            env=env,
            log=logs / "02-install-launcher.log",
        )
        installed = True
        if not console.is_file():
            raise AssertionError(f"launcher entry point is missing: {console}")

        _run(
            [str(console), "--help"],
            cwd=artifacts,
            env=env,
            log=logs / "03-launcher-help.log",
        )
        _run(
            [str(console), "flow", "--help"],
            cwd=artifacts,
            env=env,
            log=logs / "04-flow-help.log",
        )
        if browser_system_deps:
            _install_browser_system_dependencies(
                python,
                cwd=artifacts,
                env=env,
                log=logs / "05-browser-host-deps.log",
            )
            browser_preflight["system_dependencies"] = "installed-via-playwright"

        chromium_before = _browser_present(
            python,
            cwd=artifacts,
            env=env,
            log=logs / "06-browser-before.log",
        )
        browser_preflight["chromium_present_before_quickstart"] = chromium_before
        if chromium_before:
            raise AssertionError(
                "the isolated browser cache already contains Chromium; "
                "the lifecycle cannot prove a lazy install"
            )

        _run(
            [str(console), "doctor", "--backend", "web"],
            cwd=artifacts,
            env=env,
            log=logs / "07-doctor.log",
        )
        browser_preflight["doctor"] = "passed-before-browser-download"

        quickstart = artifacts / "openadapt-quickstart"
        quickstart_result = _run(
            [str(console), "quickstart", "--out", str(quickstart)],
            cwd=artifacts,
            env=env,
            log=logs / "08-quickstart.log",
        )
        chromium_after = _browser_present(
            python,
            cwd=artifacts,
            env=env,
            log=logs / "09-browser-after.log",
        )
        browser_preflight["chromium_present_after_quickstart"] = chromium_after
        browser_preflight["lazy_install_performed"] = (
            not chromium_before and chromium_after
        )
        browser_preflight["lazy_install_notice_seen"] = (
            _LAZY_BROWSER_INSTALL_NOTICE in quickstart_result.stdout
        )
        if not chromium_after:
            raise AssertionError("quickstart did not install Chromium")
        if not browser_preflight["lazy_install_notice_seen"]:
            raise AssertionError("quickstart did not report its lazy Chromium install")

        summary.update(_inspect_quickstart(quickstart))
        _run(
            [str(console), "flow", "lint", str(quickstart / "bundle")],
            cwd=artifacts,
            env=env,
            log=logs / "10-lint.log",
        )
    finally:
        if installed:
            _run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "uninstall",
                    "-y",
                    "openadapt",
                    "openadapt-flow",
                ],
                cwd=artifacts,
                env=env,
                log=logs / "11-uninstall.log",
            )
            _run(
                [
                    str(python),
                    "-c",
                    (
                        "import importlib.util; "
                        "assert importlib.util.find_spec('openadapt') is None; "
                        "assert importlib.util.find_spec('openadapt_flow') is None"
                    ),
                ],
                cwd=artifacts,
                env=env,
                log=logs / "12-uninstall-probe.log",
            )
            summary["uninstall_verified"] = True
        (work_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    print(f"\nLauncher lifecycle PASS: {work_dir / 'summary.json'}")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher-wheel", required=True)
    parser.add_argument(
        "--flow-wheel",
        default=None,
        help="Optional local Flow wheel; omit to resolve the supported release",
    )
    parser.add_argument("--work-dir", required=True)
    parser.add_argument(
        "--browser-system-deps",
        action="store_true",
        help="Install Linux browser system packages without downloading Chromium",
    )
    parser.add_argument("--source-revision", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    launcher_wheel = _resolve_wheel(args.launcher_wheel, "launcher")
    flow_wheel = _resolve_wheel(args.flow_wheel, "Flow") if args.flow_wheel else None
    run_lifecycle(
        launcher_wheel,
        Path(args.work_dir).resolve(),
        flow_wheel=flow_wheel,
        browser_system_deps=args.browser_system_deps,
        source_revision=args.source_revision,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
