"""Regression contracts for cross-platform GitHub Actions selection."""

from __future__ import annotations

import re
from pathlib import Path

from scripts import install_playwright_browser
from scripts.classify_ci_scope import code_changed

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github/workflows/ci.yml"
QUICKSTART = ROOT / ".github/workflows/quickstart-lifecycle.yml"
VALIDATE_CLAIMS = ROOT / ".github/workflows/validate-claims.yml"
PIXEL_E2E = ROOT / "tests/e2e/test_citrix_pixel_e2e.py"
DESKTOP_E2E = ROOT / "tests/e2e/test_parallels_desktop_e2e.py"


def test_lint_job_rejects_a_stale_dependency_lock() -> None:
    workflow = CI.read_text(encoding="utf-8")
    lint_start = workflow.index("\n  lint:")
    compatibility_start = workflow.index("\n  python-compatibility:")
    lint_job = workflow[lint_start:compatibility_start]

    assert (
        "uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9" in lint_job
    )
    assert 'version: "0.12.5"' in lint_job
    assert "run: uv lock --locked" in lint_job
    assert lint_job.index("run: uv lock --locked") < lint_job.index("- name: Install\n")


def test_playwright_version_probes_are_valid_python() -> None:
    workflow = CI.read_text(encoding="utf-8")
    probe = (
        'python -c "import importlib.metadata as m; '
        "print('version=' + m.version('playwright'))\" >> \"$GITHUB_OUTPUT\""
    )

    assert workflow.count("- name: Resolve Playwright version") == 3
    assert workflow.count(probe) == 3
    assert r"m.version(\"playwright\")" not in workflow


def test_playwright_installs_and_enclosing_jobs_are_bounded() -> None:
    """PR browser delivery avoids apt; release qualification owns OS deps."""
    workflow = CI.read_text(encoding="utf-8")
    test_start = workflow.index("\n  test:")
    e2e_start = workflow.index("\n  e2e-browser:")
    linux_start = workflow.index("\n  linux-atspi-x11:")
    matrix_start = workflow.index("\n  test-matrix:")
    windows_start = workflow.index("\n  windows-mock:")

    test_job = workflow[test_start:e2e_start]
    e2e_job = workflow[e2e_start:linux_start]
    matrix_job = workflow[matrix_start:windows_start]
    standard_invocation = (
        "python scripts/install_playwright_browser.py\n"
        "          --attempts 2 --attempt-timeout-seconds 270"
    )
    qualification_invocation = (
        "python scripts/install_playwright_browser.py\n"
        "          --attempts 2 --attempt-timeout-seconds 600\n"
        "          --with-system-deps"
    )

    for job, timeout in (
        (test_job, "timeout-minutes: 55"),
        (e2e_job, "timeout-minutes: 50"),
    ):
        assert timeout in job
        install_start = job.index("- name: Install and launch Playwright browser")
        install_end = job.index("\n\n", install_start)
        install_step = job[install_start:install_end]
        assert "timeout-minutes: 12" in install_step
        assert standard_invocation in install_step
        assert "--with-system-deps" not in install_step

    assert "timeout-minutes: 75" in matrix_job
    matrix_install_start = matrix_job.index("- name: Install Playwright browser")
    matrix_install_end = matrix_job.index("\n\n", matrix_install_start)
    matrix_install_step = matrix_job[matrix_install_start:matrix_install_end]
    assert "timeout-minutes: 24" in matrix_install_step
    assert qualification_invocation in matrix_install_step

    assert workflow.count(standard_invocation) == 2
    assert workflow.count(qualification_invocation) == 1
    assert "run: playwright install --with-deps chromium" not in workflow

    # The privileged cleanup proof runs before browser delivery and does not
    # depend on an apt mirror. The full suite excludes only that duplicate.
    assert "Linux retained installer process group (non-injecting)" in test_job
    assert (
        "pytest -vv --durations=20 tests/test_install_playwright_browser.py" in test_job
    )
    assert "--ignore=tests/test_install_playwright_browser.py" in test_job
    assert "coverage report --fail-under=85" in test_job
    assert "pytest -vv --durations=20 tests/e2e/test_free_path_e2e.py" in e2e_job
    assert "test_paths=(tests/e2e/test_record_compile_replay.py)" in e2e_job
    assert 'pytest -vv --durations=20 "${test_paths[@]}"' in e2e_job
    assert "--ignore=tests/e2e/test_free_path_e2e.py" in e2e_job
    assert "pytest -vv --durations=20 --basetemp=runs/ci" in matrix_job


def test_standard_browser_step_covers_cleanup_and_launch_worst_cases() -> None:
    attempts = 2
    attempt_timeout_seconds = 270
    retry_delay_seconds = 5
    step_timeout_seconds = 12 * 60
    launch_timeout_seconds = (
        install_playwright_browser.BROWSER_LAUNCH_TIMEOUT_MILLISECONDS / 1000
    )

    # POSIX can spend one bounded sudo call and one bounded group-exit wait at
    # each of TERM, KILL, and the final exact-PGID privileged KILL fallback.
    posix_cleanup_per_attempt = 3 * (
        install_playwright_browser.SUDO_SIGNAL_TIMEOUT_SECONDS
        + install_playwright_browser.PROCESS_EXIT_TIMEOUT_SECONDS
    )
    # Windows can wait for job accounting to reach zero and then reap the
    # gated wrapper before it closes the retained Job Object handle.
    windows_cleanup_per_attempt = 2 * (
        install_playwright_browser.PROCESS_EXIT_TIMEOUT_SECONDS
    )

    posix_worst_case = (
        attempts * (attempt_timeout_seconds + posix_cleanup_per_attempt)
        + retry_delay_seconds
        + install_playwright_browser.BROWSER_LAUNCH_PROBE_TIMEOUT_SECONDS
        + posix_cleanup_per_attempt
    )
    windows_worst_case = (
        attempts * (attempt_timeout_seconds + windows_cleanup_per_attempt)
        + retry_delay_seconds
        + install_playwright_browser.BROWSER_LAUNCH_PROBE_TIMEOUT_SECONDS
        + windows_cleanup_per_attempt
    )

    assert launch_timeout_seconds == 30
    assert install_playwright_browser.BROWSER_LAUNCH_PROBE_TIMEOUT_SECONDS == 35
    assert (
        launch_timeout_seconds
        < install_playwright_browser.BROWSER_LAUNCH_PROBE_TIMEOUT_SECONDS
    )
    assert posix_worst_case == 670
    assert windows_worst_case == 610
    assert max(posix_worst_case, windows_worst_case) < step_timeout_seconds


def test_release_browser_step_covers_system_dependency_cleanup() -> None:
    attempts = 2
    attempt_timeout_seconds = 600
    retry_delay_seconds = 5
    step_timeout_seconds = 24 * 60
    posix_cleanup_per_attempt = 3 * (
        install_playwright_browser.SUDO_SIGNAL_TIMEOUT_SECONDS
        + install_playwright_browser.PROCESS_EXIT_TIMEOUT_SECONDS
    )
    windows_cleanup_per_attempt = 2 * (
        install_playwright_browser.PROCESS_EXIT_TIMEOUT_SECONDS
    )

    posix_worst_case = (
        attempts * (attempt_timeout_seconds + posix_cleanup_per_attempt)
        + retry_delay_seconds
        + install_playwright_browser.BROWSER_LAUNCH_PROBE_TIMEOUT_SECONDS
        + posix_cleanup_per_attempt
    )
    windows_worst_case = (
        attempts * (attempt_timeout_seconds + windows_cleanup_per_attempt)
        + retry_delay_seconds
        + install_playwright_browser.BROWSER_LAUNCH_PROBE_TIMEOUT_SECONDS
        + windows_cleanup_per_attempt
    )

    assert posix_worst_case == 1330
    assert windows_worst_case == 1270
    assert max(posix_worst_case, windows_worst_case) < step_timeout_seconds


def test_windows_required_job_proves_retained_installer_job_object() -> None:
    workflow = CI.read_text(encoding="utf-8")
    windows_start = workflow.index("\n  windows-mock:")
    wheel_start = workflow.index("\n  wheel:")
    windows_job = workflow[windows_start:wheel_start]

    assert "Windows retained installer Job Object (non-injecting)" in windows_job
    assert (
        "pytest -vv --durations=20 tests/test_install_playwright_browser.py"
        in windows_job
    )


def test_required_linux_atspi_qualification_is_bounded() -> None:
    workflow = CI.read_text(encoding="utf-8")
    linux_start = workflow.index("\n  linux-atspi-x11:")
    matrix_start = workflow.index("\n  test-matrix:")
    linux_job = workflow[linux_start:matrix_start]

    assert "runs-on: ubuntu-24.04\n    timeout-minutes: 30" in linux_job
    step_limits = (
        ("Install isolated X11, D-Bus, GTK3, and AT-SPI", 15),
        ("Qualify GTK workflow on real AT-SPI", 10),
        ("Upload Linux AT-SPI qualification evidence", 2),
    )
    for step_name, timeout_minutes in step_limits:
        step_start = linux_job.index(f"- name: {step_name}")
        step_end = linux_job.index("\n\n", step_start)
        step = linux_job[step_start:step_end]
        assert f"timeout-minutes: {timeout_minutes}" in step

    assert sum(timeout for _name, timeout in step_limits) <= 30
    assert linux_job.index("sudo apt-get update") < linux_job.index(
        "scripts/qualify_linux_atspi.py"
    )
    assert "--output runs/linux-atspi/results.json" in linux_job
    assert "if-no-files-found: error" in linux_job


def test_docs_only_classifier_skips_markdown_and_fails_closed() -> None:
    assert code_changed(["README.md", "docs/LIMITS.md"]) is False
    assert code_changed(["paper/workshop/main.tex"]) is False
    assert code_changed(["LICENSE", "NOTICE.txt"]) is False
    assert code_changed([".github/ISSUE_TEMPLATE/bug.md"]) is False
    assert code_changed(["openadapt_flow/policy.py"]) is True
    assert code_changed([".github/workflows/ci.yml"]) is True
    assert code_changed(["README.md", "openadapt_flow/ir.py"]) is True
    assert code_changed([]) is True


def test_expensive_required_jobs_no_op_on_docs_only() -> None:
    """Documentation-only PRs must still report the required CheckRun names.

    GitHub leaves a missing required check pending, so the jobs stay in the
    workflow and succeed without installing the suite. docs-consistency is
    the one required job that still does work on a docs-only change.
    """
    workflow = CI.read_text(encoding="utf-8")
    gated = (
        "lint",
        "python-compatibility",
        "mypy-strict-safety",
        "effectbench-standalone",
        "interop-types",
        "phi-guard",
        "test",
        "e2e-browser",
        "linux-atspi-x11",
        "windows-mock",
        "wheel",
    )
    job_starts = {
        match.group(1): match.start()
        for match in re.finditer(r"^  ([a-z0-9-]+):", workflow, re.MULTILINE)
    }
    ordered = sorted(job_starts.items(), key=lambda item: item[1])
    for name in gated:
        start = job_starts[name]
        later = [pos for other, pos in ordered if pos > start]
        end = later[0] if later else len(workflow)
        job = workflow[start:end]
        assert "needs: [scope]" in job, name
        assert "Skip expensive work on documentation-only changes" in job, name
        assert "needs.scope.outputs.code_changed == 'true'" in job, name

    docs_start = workflow.index("\n  docs-consistency:")
    docs_end = workflow.index("\n  effectbench-standalone:")
    docs_job = workflow[docs_start:docs_end]
    assert "needs: [scope]" not in docs_job
    assert "code_changed" not in docs_job
    assert "python scripts/check_consistency.py" in docs_job

    scope = workflow[workflow.index("\n  scope:") : workflow.index("\n  lint:")]
    assert "scripts/classify_ci_scope.py" in scope
    assert "fetch-depth: 0" in scope


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


def test_macos_deselects_only_redundant_platform_neutral_heavy_nodes() -> None:
    """macOS drops exactly the platform-neutral nodes Ubuntu already counts.

    Both deselected nodes are heavy AND carry no OS-specific signal: the
    identity harness re-runs one browser/OCR corpus, and the qualification
    campaign is deterministic with no Docker, network, browser, or model
    calls. Every canonical Ubuntu leg still runs both in full, so neither
    contract is weakened -- only its redundant second execution is dropped.
    Linux must never deselect anything.
    """

    workflow = CI.read_text(encoding="utf-8")
    nodes = (
        "tests/test_identity_ladder.py::test_harness_zero_false_accept_all_configs",
        "tests/test_qualification_gate_campaign.py",
    )
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
    assert "pytest -vv --durations=20 --basetemp=runs/ci" in linux_step
    assert "if: runner.os == 'macOS'" in macos_step
    for node in nodes:
        assert macos_step.count(f"--deselect={node}") == 1
        assert workflow.count(f"--deselect={node}") == 1


def test_exhaustive_identity_ladder_corpus_runs_in_the_slow_lane_only() -> None:
    """The exhaustive identity-ladder sweep stays OUT of the fast PR lane.

    The fast `test` job runs the bounded class-covering harness; the
    nightly/dispatch canonical-Ubuntu matrix leg opts into the exhaustive
    corpus via OPENADAPT_IDENTITY_LADDER_EXHAUSTIVE. This pins both sides so
    the exhaustive sweep can neither silently stop running anywhere nor creep
    back into the fast lane whose 900s budget it intermittently exceeded.
    """
    workflow = CI.read_text(encoding="utf-8")
    flag = "OPENADAPT_IDENTITY_LADDER_EXHAUSTIVE"

    linux_start = workflow.index(
        "- name: Test (full suite incl. e2e, canonical Ubuntu)"
    )
    macos_start = workflow.index(
        "- name: Test (full suite incl. e2e, macOS platform coverage)"
    )
    linux_step = workflow[linux_start:macos_start]
    assert f'{flag}: "1"' in linux_step

    fast_start = workflow.index("- name: Test (PR gate or complete post-merge suite)")
    fast_end = workflow.index("- name: Coverage (whole-package visibility)")
    assert flag not in workflow[fast_start:fast_end]
    # exactly one opt-in: the canonical Ubuntu matrix leg
    assert workflow.count(f'{flag}: "1"') == 1


def test_supported_claims_consume_their_required_jobs_real_junit() -> None:
    """The two required test jobs must fail when cited evidence did not run."""

    workflow = CI.read_text(encoding="utf-8")
    unit_start = workflow.index("- name: Test (PR gate or complete post-merge suite)")
    unit_end = workflow.index("- name: Coverage (whole-package visibility)")
    unit = workflow[unit_start:unit_end]
    assert "--junitxml=runs/unit-claims-junit.xml" in unit
    assert "--ci-job test --junit runs/unit-claims-junit.xml" in unit

    browser_start = workflow.index("- name: E2E (PR gate or complete post-merge suite)")
    browser_end = workflow.index("- name: Upload run artifacts", browser_start)
    browser = workflow[browser_start:browser_end]
    assert "--junitxml=runs/e2e-claims-junit.xml" in browser
    assert "--ci-job e2e-browser --junit runs/e2e-claims-junit.xml" in browser

    claims = VALIDATE_CLAIMS.read_text(encoding="utf-8")
    assert "validate_claims.py --check --structure-only" in claims


def test_pr_and_complete_test_tiers_are_event_bound_and_fail_closed() -> None:
    """PR and main-push stay focused. Only schedule and dispatch get the rest."""

    workflow = CI.read_text(encoding="utf-8")
    unit_start = workflow.index("\n  test:")
    browser_start = workflow.index("\n  e2e-browser:")
    linux_start = workflow.index("\n  linux-atspi-x11:")
    unit = workflow[unit_start:browser_start]
    browser = workflow[browser_start:linux_start]

    complete_condition = (
        'if [ "$GITHUB_EVENT_NAME" = schedule ] || '
        '[ "$GITHUB_EVENT_NAME" = workflow_dispatch ]; then'
    )
    campaign = "--ignore=tests/test_qualification_gate_campaign.py"

    assert unit.count("- name: Test (PR gate or complete post-merge suite)") == 1
    assert unit.count(complete_condition) == 1
    unit_step = unit[
        unit.index("- name: Test (PR gate or complete post-merge suite)") : unit.index(
            "- name: Validate passing unit claim evidence"
        )
    ]
    assert f"extra_args=({campaign})" in unit_step
    assert "extra_args=()" in unit_step
    assert unit_step.index(f"extra_args=({campaign})") < unit_step.index(
        complete_condition
    )
    assert unit_step.index(complete_condition) < unit_step.index("extra_args=()")
    assert '"${extra_args[@]}"' in unit_step
    assert "--ignore=tests/e2e" in unit_step
    assert "--ignore=tests/test_install_playwright_browser.py" in unit_step
    assert "--junitxml=runs/unit-claims-junit.xml" in unit_step
    assert "--cov=openadapt_flow --cov-report=" in unit_step

    assert browser.count("- name: E2E (PR gate or complete post-merge suite)") == 1
    assert browser.count(complete_condition) == 1
    browser_step = browser[
        browser.index(
            "- name: E2E (PR gate or complete post-merge suite)"
        ) : browser.index("- name: Validate passing browser claim evidence")
    ]
    focused = "test_paths=(tests/e2e/test_record_compile_replay.py)"
    complete = "test_paths=(tests/e2e --ignore=tests/e2e/test_free_path_e2e.py)"
    assert focused in browser_step
    assert complete in browser_step
    assert browser_step.index(focused) < browser_step.index(complete_condition)
    assert browser_step.index(complete_condition) < browser_step.index(complete)
    assert 'pytest -vv --durations=20 "${test_paths[@]}"' in browser_step
    assert "--junitxml=runs/e2e-claims-junit.xml" in browser_step

    # The arrays start focused. Only named slow events expand them. A new
    # event cannot silently inherit the complete selection.
    assert 'GITHUB_EVENT_NAME" = pull_request' not in unit + browser
    assert 'GITHUB_EVENT_NAME" = push' not in unit + browser


def test_validating_refresh_uses_exact_macos_parallels_substrate_and_scope() -> None:
    """A green validating report cannot come from Ubuntu or skipped evidence."""

    claims = VALIDATE_CLAIMS.read_text(encoding="utf-8")
    start = claims.index("  refresh-validating-evidence:")
    refresh = claims[start:]

    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in claims
    assert "runs-on: [self-hosted, macos, arm64, openadapt-parallels]" in refresh
    assert "OPENADAPT_PARALLELS_VALIDATION_ENABLED" in refresh
    assert "group: openadapt-parallels-validation" in refresh
    assert "cancel-in-progress: false" in refresh
    assert "runs-on: ubuntu-latest" not in refresh
    assert "oa-vm" not in refresh
    assert "command -v prlctl" in refresh
    assert "OAFLOW_PARALLELS_BASE_SNAPSHOT_ID" in refresh
    assert "OAFLOW_PARALLELS_RECOVERY_JOURNAL" in refresh
    assert "${{ github.workspace }}/../.openadapt-flow/" in refresh
    assert "vm.require_current_snapshot" in refresh
    assert refresh.count("scripts/reconcile_parallels_recovery.py") == 2
    assert "if: ${{ always() }}" in refresh
    assert "timeout-minutes: 110" in refresh
    assert "timeout-minutes: 90" in refresh
    assert "--ci-job validating --junit runs/validating-junit.xml" in refresh
    assert refresh.index("tests/e2e/test_citrix_pixel_e2e.py") < refresh.index(
        "tests/e2e/test_parallels_desktop_e2e.py"
    )
    for path in (
        "tests/e2e/test_parallels_desktop_e2e.py",
        "tests/e2e/test_citrix_pixel_e2e.py",
    ):
        assert f"--evidence-path {path}" in refresh

    for test_path in (PIXEL_E2E, DESKTOP_E2E):
        source = test_path.read_text(encoding="utf-8")
        assert "journal.begin(" in source
        assert source.index("journal.begin(") < source.index("vm.revert(")
        assert "vm.snapshot(" not in source


def test_clean_machine_lifecycle_declares_utf8_on_every_os() -> None:
    workflow = QUICKSTART.read_text(encoding="utf-8")
    lifecycle_start = workflow.index("  lifecycle:")
    strategy_start = workflow.index("    strategy:", lifecycle_start)
    lifecycle_header = workflow[lifecycle_start:strategy_start]

    assert 'PYTHONUTF8: "1"' in lifecycle_header
    assert 'PYTHONIOENCODING: "utf-8"' in lifecycle_header
