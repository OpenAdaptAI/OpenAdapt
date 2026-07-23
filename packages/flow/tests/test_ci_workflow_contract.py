"""Regression contracts for cross-platform GitHub Actions selection."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github/workflows/ci.yml"
QUICKSTART = ROOT / ".github/workflows/quickstart-lifecycle.yml"
VALIDATE_CLAIMS = ROOT / ".github/workflows/validate-claims.yml"


def test_playwright_version_probes_are_valid_python() -> None:
    workflow = CI.read_text(encoding="utf-8")
    probe = (
        'python -c "import importlib.metadata as m; '
        "print('version=' + m.version('playwright'))\" >> \"$GITHUB_OUTPUT\""
    )

    assert workflow.count("- name: Resolve Playwright version") == 3
    assert workflow.count(probe) == 3
    assert r"m.version(\"playwright\")" not in workflow


def test_full_matrix_can_be_dispatched_on_an_exact_branch() -> None:
    workflow = CI.read_text(encoding="utf-8")
    on_start = workflow.index("on:\n")
    jobs_start = workflow.index("\njobs:\n", on_start)
    triggers = workflow[on_start:jobs_start]

    assert "  workflow_dispatch:\n" in triggers


def test_full_matrix_runs_only_nightly_or_when_explicitly_dispatched() -> None:
    workflow = CI.read_text(encoding="utf-8")
    matrix_start = workflow.index("\n  test-matrix:")
    strategy_start = workflow.index("\n    strategy:", matrix_start)
    matrix_header = workflow[matrix_start:strategy_start]

    assert "github.event_name == 'schedule'" in matrix_header
    assert "github.event_name == 'workflow_dispatch'" in matrix_header
    assert "inputs." not in matrix_header
    assert "github.event_name != 'pull_request'" not in matrix_header
    assert "github.event_name == 'push'" not in matrix_header


def test_required_context_comments_match_actual_checkrun_names() -> None:
    workflow = CI.read_text(encoding="utf-8")
    start = workflow.index("# REQUIRED_CONTEXTS")
    end = workflow.index("# `gate` comes from", start)
    documented = set(
        re.findall(r"^#   - ([a-z0-9-]+)$", workflow[start:end], re.MULTILINE)
    )

    assert documented == {
        "lint",
        "python-compatibility",
        "mypy-strict-safety",
        "phi-guard",
        "windows-mock",
        "docs-consistency",
        "effectbench-standalone",
        "interop-types",
        "test",
        "e2e-browser",
        "linux-atspi-x11",
        "wheel",
        "gate",
    }

    claims = VALIDATE_CLAIMS.read_text(encoding="utf-8")
    claims_header = claims[: claims.index("\non:\n")]
    assert '"gate" (the actual CheckRun job name' in claims_header
    assert '"validate-claims" to block a PR' not in claims_header


def test_required_test_job_parallelizes_only_profiled_identity_outliers() -> None:
    workflow = CI.read_text(encoding="utf-8")
    test_start = workflow.index("\n  test:")
    e2e_start = workflow.index("\n  e2e-browser:", test_start)
    test_job = workflow[test_start:e2e_start]

    # Preserve the single required CheckRun name: this job must never become a
    # matrix, and exactly two pytest subprocesses share its result.
    assert "\n    strategy:" not in test_job
    test_step_start = test_job.index("- name: Test (fast unit suite)")
    combine_start = test_job.index("- name: Combine parallel coverage data")
    test_step = test_job[test_step_start:combine_start]
    assert "set -euo pipefail" in test_step
    assert test_step.count("pytest -q") == 2
    assert test_step.count(" --cov=openadapt_flow --cov-report= &") == 2

    heavy_module = "tests/test_identity_ocr_conservative_9th.py"
    heavy_node = (
        "tests/test_identity_ladder.py::test_harness_zero_false_accept_all_configs"
    )

    # The normal lane excludes only the two measured outliers, while the heavy
    # lane selects both exactly once. No test is removed from the required gate.
    ignored = re.findall(r"--ignore=([^\s]+)", test_step)
    deselected = re.findall(r"--deselect=([^\s]+)", test_step)
    assert len(ignored) == 2
    assert set(ignored) == {"tests/e2e", heavy_module}
    assert deselected == [heavy_node]
    assert test_step.count(heavy_module) == 2
    assert test_step.count(heavy_node) == 2

    # Parallel pytest-cov writers must never share a data file or basetemp.
    assert test_step.count("COVERAGE_FILE=.coverage.normal") == 1
    assert test_step.count("COVERAGE_FILE=.coverage.heavy-identity") == 1
    assert test_step.count("--basetemp=runs/ci-normal") == 1
    assert test_step.count("--basetemp=runs/ci-heavy-identity") == 1
    assert test_step.count(" &\n") == 2

    # Both subprocesses are awaited even if the first fails, and either failure
    # fails the unchanged required `test` context.
    assert 'if ! wait "$normal_pid"; then' in test_step
    assert 'if ! wait "$heavy_pid"; then' in test_step
    assert 'exit "$test_status"' in test_step

    combine_step = test_job[combine_start:]
    visibility_start = combine_step.index("- name: Coverage (whole-package visibility)")
    assert "run: coverage combine" in combine_step[:visibility_start]
    assert combine_step.index("run: coverage combine") < visibility_start
    assert "run: coverage report || true" in combine_step
    assert "coverage report --fail-under=85" in combine_step


def test_macos_deselects_only_redundant_heavy_identity_harness() -> None:
    workflow = CI.read_text(encoding="utf-8")
    node = "tests/test_identity_ladder.py::test_harness_zero_false_accept_all_configs"
    linux_start = workflow.index(
        "- name: Test (full suite incl. e2e, canonical Ubuntu)"
    )
    macos_start = workflow.index(
        "- name: Test (full suite incl. e2e, macOS platform coverage)"
    )
    upload_start = workflow.index("- name: Upload run artifacts", macos_start)
    linux_step = workflow[linux_start:macos_start]
    macos_step = workflow[macos_start:upload_start]

    assert "if: runner.os == 'Linux'" in linux_step
    assert "--deselect" not in linux_step
    assert "pytest -q --basetemp=runs/ci" in linux_step
    assert "if: runner.os == 'macOS'" in macos_step
    assert macos_step.count(f"--deselect={node}") == 1
    # The only other deselection is the required test job's normal lane; that
    # same node is selected explicitly by its concurrent heavy lane.
    assert workflow.count(f"--deselect={node}") == 2


def test_clean_machine_lifecycle_declares_utf8_on_every_os() -> None:
    workflow = QUICKSTART.read_text(encoding="utf-8")
    lifecycle_start = workflow.index("  lifecycle:")
    strategy_start = workflow.index("    strategy:", lifecycle_start)
    lifecycle_header = workflow[lifecycle_start:strategy_start]

    assert 'PYTHONUTF8: "1"' in lifecycle_header
    assert 'PYTHONIOENCODING: "utf-8"' in lifecycle_header
