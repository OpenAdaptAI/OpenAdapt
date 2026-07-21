#!/usr/bin/env python3
"""Exercise the public quickstart from a clean wheel-installed environment.

This is intentionally a product lifecycle check, not another source-tree test.
It creates a fresh virtual environment, installs only the supplied wheel, runs
the complete MockMed journey, inspects the generated evidence, uninstalls the
package, and proves the environment no longer imports it.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import venv
from pathlib import Path
from typing import Sequence


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log: Path,
    expected: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Run a lifecycle command, persist its output, and enforce its exit code."""
    printable = subprocess.list2cmdline(list(command))
    print(f"\n$ {printable}", flush=True)
    child_env = env.copy()
    # Windows runners otherwise inherit a legacy console code page (commonly
    # cp1252). The CLI deliberately prints status glyphs, and its JSON evidence
    # is a UTF-8 artifact contract, so make the subprocess boundary explicit.
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
    if result.returncode != expected:
        raise RuntimeError(
            f"{printable} exited {result.returncode}; expected {expected} (see {log})"
        )
    return result


def _resolve_wheel(pattern: str) -> Path:
    matches = [Path(item).resolve() for item in glob.glob(pattern)]
    if len(matches) != 1:
        raise ValueError(
            f"--wheel must resolve to exactly one file; {pattern!r} matched "
            f"{len(matches)}: {matches}"
        )
    if matches[0].suffix != ".whl":
        raise ValueError(f"--wheel is not a wheel: {matches[0]}")
    return matches[0]


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _console_script(root: Path) -> Path:
    return root / (
        "Scripts/openadapt-flow.exe" if os.name == "nt" else "bin/openadapt-flow"
    )


def _load_report(path: Path) -> dict:
    if not path.is_file():
        raise AssertionError(f"missing machine-readable run report: {path}")
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise AssertionError(f"run report is not an object: {path}")
    return report


def _inspect_artifacts(artifacts: Path) -> dict[str, object]:
    baseline_dir = artifacts / "baseline-run"
    drift_dir = artifacts / "theme-drift-run"
    baseline = _load_report(baseline_dir / "report.json")
    drift = _load_report(drift_dir / "report.json")

    for run_dir in (baseline_dir, drift_dir):
        if not (run_dir / "REPORT.md").is_file():
            raise AssertionError(f"missing illustrated report: {run_dir / 'REPORT.md'}")

    if baseline.get("success") is not True:
        raise AssertionError("baseline replay did not succeed")
    if baseline.get("model_calls") != 0:
        raise AssertionError("baseline replay made a model call")
    if baseline.get("heal_count") != 0:
        raise AssertionError("baseline replay unexpectedly healed")

    if drift.get("success") is not True:
        raise AssertionError("theme-drift replay did not succeed")
    if drift.get("model_calls") != 0:
        raise AssertionError("deterministic theme repair made a model call")
    heal_count = int(drift.get("heal_count") or 0)
    if heal_count < 1:
        raise AssertionError("theme drift produced no reviewable repair")

    results = drift.get("results") or []
    applied = [row for row in results if (row.get("heal") or {}).get("applied")]
    patches = list((drift_dir / "heals").glob("*/patch.json"))
    if len(applied) != heal_count or len(patches) != heal_count:
        raise AssertionError(
            "heal evidence is incomplete: "
            f"report={heal_count}, applied={len(applied)}, patches={len(patches)}"
        )

    healed_bundle = artifacts / "healed-bundle"
    for required in ("workflow.json", "manifest.json"):
        if not (healed_bundle / required).is_file():
            raise AssertionError(f"healed bundle is missing {required}")

    return {
        "baseline_success": True,
        "baseline_model_calls": 0,
        "baseline_heals": 0,
        "drift_success": True,
        "drift_model_calls": 0,
        "drift_heals": heal_count,
        "repair_patches": len(patches),
        "reports_inspected": 2,
    }


def run_lifecycle(
    wheel: Path,
    work_dir: Path,
    *,
    install_browser: bool,
    browser_with_deps: bool,
) -> dict[str, object]:
    """Run install through uninstall, returning the evidence summary."""
    if work_dir.exists():
        raise FileExistsError(
            f"work directory already exists: {work_dir}; remove it before rerunning"
        )
    work_dir.mkdir(parents=True)
    venv_dir = work_dir / "venv"
    artifacts = work_dir / "artifacts"
    logs = work_dir / "logs"
    artifacts.mkdir()

    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    python = _venv_python(venv_dir)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # MockMed contains synthetic identities. Disable the optional PHI warning so
    # lifecycle output stays actionable; real regulated runs must use SCRUB=on.
    env["OPENADAPT_FLOW_SCRUB"] = "off"
    installed = False
    summary: dict[str, object] = {
        "wheel": wheel.name,
        "platform": sys.platform,
    }

    try:
        _run(
            [str(python), "-m", "pip", "install", str(wheel)],
            cwd=artifacts,
            env=env,
            log=logs / "01-install.log",
        )
        installed = True
        console = _console_script(venv_dir)
        if not console.is_file():
            raise AssertionError(f"console entry point was not installed: {console}")
        _run(
            [str(console), "--help"],
            cwd=artifacts,
            env=env,
            log=logs / "02-cli-help.log",
        )

        if install_browser:
            browser_command = [str(python), "-m", "playwright", "install"]
            if browser_with_deps:
                browser_command.append("--with-deps")
            browser_command.append("chromium")
            _run(
                browser_command,
                cwd=artifacts,
                env=env,
                log=logs / "03-browser-install.log",
            )

        cli = [str(python), "-m", "openadapt_flow"]
        recording = artifacts / "recording"
        bundle = artifacts / "bundle"
        _run(
            [*cli, "demo-record", "--out", str(recording)],
            cwd=artifacts,
            env=env,
            log=logs / "04-record.log",
        )
        _run(
            [
                *cli,
                "compile",
                str(recording),
                "--out",
                str(bundle),
                "--name",
                "clean-machine-lifecycle",
            ],
            cwd=artifacts,
            env=env,
            log=logs / "05-compile.log",
        )

        # The bundled tutorial is deliberately not production-certified. Lint
        # must refuse its unarmed irreversible click, while the smoke policy can
        # still certify the deterministic execution tutorial.
        _run(
            [*cli, "lint", str(bundle)],
            cwd=artifacts,
            env=env,
            log=logs / "06-lint-expected-refusal.log",
            expected=1,
        )
        _run(
            [*cli, "certify", str(bundle), "--policy", "permissive"],
            cwd=artifacts,
            env=env,
            log=logs / "07-certify-permissive.log",
        )
        _run(
            [*cli, "certify", str(bundle), "--policy", "clinical-write"],
            cwd=artifacts,
            env=env,
            log=logs / "08-certify-clinical-expected-refusal.log",
            expected=2,
        )
        _run(
            [
                *cli,
                "replay",
                str(bundle),
                "--run-dir",
                str(artifacts / "baseline-run"),
            ],
            cwd=artifacts,
            env=env,
            log=logs / "09-replay-baseline.log",
        )
        _run(
            [
                *cli,
                "replay",
                str(bundle),
                "--drift",
                "theme",
                "--save-healed-to",
                str(artifacts / "healed-bundle"),
                "--run-dir",
                str(artifacts / "theme-drift-run"),
            ],
            cwd=artifacts,
            env=env,
            log=logs / "10-replay-drift.log",
        )
        summary.update(_inspect_artifacts(artifacts))
    finally:
        if installed:
            _run(
                [str(python), "-m", "pip", "uninstall", "-y", "openadapt-flow"],
                cwd=artifacts,
                env=env,
                log=logs / "11-uninstall.log",
            )
            probe = _run(
                [
                    str(python),
                    "-c",
                    (
                        "import importlib.util; "
                        "assert importlib.util.find_spec('openadapt_flow') is None"
                    ),
                ],
                cwd=artifacts,
                env=env,
                log=logs / "12-uninstall-probe.log",
            )
            summary["uninstall_verified"] = probe.returncode == 0
        (work_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    print(f"\nLifecycle PASS: {work_dir / 'summary.json'}")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel", required=True, help="Wheel path or a glob resolving to one wheel"
    )
    parser.add_argument(
        "--work-dir", required=True, help="New directory for lifecycle artifacts"
    )
    parser.add_argument(
        "--install-browser",
        action="store_true",
        help="Install Playwright Chromium before running the lifecycle",
    )
    parser.add_argument(
        "--browser-with-deps",
        action="store_true",
        help="Also install Linux browser system dependencies",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.browser_with_deps and not args.install_browser:
        raise SystemExit("--browser-with-deps requires --install-browser")
    wheel = _resolve_wheel(args.wheel)
    run_lifecycle(
        wheel,
        Path(args.work_dir).resolve(),
        install_browser=args.install_browser,
        browser_with_deps=args.browser_with_deps,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
