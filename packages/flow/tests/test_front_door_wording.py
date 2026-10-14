"""Truthful wording at the front door, with every exit code unchanged.

* ``certify`` says a bundle *passes* a policy, never that it is "certified
  safe", and flags ``permissive`` as a minimal smoke check;
* the run gate's certification row is titled "Certification" (a refusal no
  longer reads "Certification passed: bundle is NOT certified"), each offending
  step is listed once, and the approval row no longer claims that no write
  needs verification when a write simply declared no effect;
* ``replay`` prints the plain result first and says that exit 0 on a
  finished-but-unchecked run is not a confirmed save;
* ``lint --strict`` explains a warnings-only failure;
* next-step hints follow the spelling the person typed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow import policy as policy_module
from openadapt_flow.__main__ import _finish_replay, main
from openadapt_flow.ir import ActionKind, RunReport, Step, StepResult, Workflow
from openadapt_flow.policy import CertifyReport, Finding, LintReport
from openadapt_flow.run_gate import (
    GATE_APPROVAL,
    GATE_CERTIFICATION,
    _gate_approval,
    _result,
)

# ---------------------------------------------------------------------------
# certify
# ---------------------------------------------------------------------------


def _benign_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "benign"
    Workflow(
        name="benign",
        steps=[Step(id="s", intent="click 'Open'", action=ActionKind.CLICK)],
    ).save(bundle)
    return bundle


def test_certify_permissive_pass_says_passes_not_certified_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _benign_bundle(tmp_path)
    assert main(["certify", str(bundle), "--policy", "permissive"]) == 0
    out = capsys.readouterr().out
    assert "no violations: passes the 'permissive' policy." in out
    assert "minimal smoke check" in out
    assert "certified safe" not in out
    assert "—" not in out


def test_certify_pass_under_another_policy_scopes_the_claim() -> None:
    text = CertifyReport(
        policy_name="clinical-write", workflow_name="w", passed=True, n_steps=3
    ).render()
    assert "passes the 'clinical-write' policy" in text
    assert "covers this policy's rules only" in text
    assert "certified safe" not in text


def test_certify_failure_render_is_unchanged() -> None:
    from openadapt_flow.policy import Violation

    text = CertifyReport(
        policy_name="clinical-write",
        workflow_name="w",
        passed=False,
        n_steps=1,
        violations=[Violation(rule="r", step_id="s", reason="why")],
    ).render()
    assert text.startswith("FAIL: workflow 'w' vs policy 'clinical-write'")
    assert "  - (r) [s] why" in text


# ---------------------------------------------------------------------------
# run gate rows
# ---------------------------------------------------------------------------


def test_certification_row_title_does_not_contradict_a_refusal() -> None:
    row = _result(GATE_CERTIFICATION, False, "bundle is NOT certified", ["a"])
    assert row.title == "Certification"
    assert row.render().startswith("  [REFUSE] Certification: bundle is NOT")


def test_offenders_are_listed_once_in_first_seen_order() -> None:
    row = _result(
        GATE_CERTIFICATION,
        False,
        "x",
        ["step_000", "step_000", "step_002", "step_002", "step_001"],
    )
    assert row.offenders == ["step_000", "step_002", "step_001"]
    assert "steps: step_000, step_002, step_001" in row.render()


def test_approval_row_names_writes_without_a_declared_effect() -> None:
    write = Step(
        id="save",
        intent="click 'Save Encounter'",
        action=ActionKind.CLICK,
        risk="irreversible",
    )
    workflow = Workflow(name="w", steps=[write])
    row = _gate_approval(
        workflow,
        [write],
        None,
        None,
        False,
        require_current_risk_certification=False,
        certifying_policy=None,
    )
    assert row.gate == GATE_APPROVAL
    assert row.passed  # unchanged: the effect-coverage gate owns this refusal
    assert "no consequential writes require effect verification" not in row.detail
    assert "nothing to approve" in row.detail
    assert "Effect coverage" in row.detail


def test_approval_row_without_any_write_keeps_its_wording() -> None:
    step = Step(id="s", intent="click 'Open'", action=ActionKind.CLICK)
    row = _gate_approval(
        Workflow(name="w", steps=[step]),
        [step],
        None,
        None,
        False,
        require_current_risk_certification=False,
        certifying_policy=None,
    )
    assert row.passed
    assert row.detail == "no consequential writes require effect verification"


# ---------------------------------------------------------------------------
# replay finisher
# ---------------------------------------------------------------------------


def _finish(tmp_path: Path, outcome: str, *, success: bool, profile: str) -> int:
    run_dir = tmp_path / f"run-{outcome}"
    report = RunReport(
        workflow_name="wf",
        started_at="2026-10-09T12:00:00+00:00",
        execution_outcome=outcome,
        execution_profile=profile,
        results=[
            StepResult(
                step_id="step_004",
                intent="click 'Save encounter'",
                ok=success,
                elapsed_ms=10.0,
            )
        ],
        success=success,
    )
    report.save(run_dir)
    return _finish_replay(run_dir, report)


def test_unverified_replay_keeps_exit_0_and_says_not_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _finish(tmp_path, "COMPLETED_UNVERIFIED", success=True, profile="demo")
    out = capsys.readouterr().out
    assert code == 0
    assert out.splitlines()[0] == (
        "Result: Finished, not checked. It did the steps but did not confirm "
        "the saved result."
    )
    assert "Exit code 0 here means the steps finished, not that the save" in out


def test_halted_replay_result_line_is_conservative_without_transaction(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _finish(tmp_path, "HALTED", success=False, profile="demo")
    out = capsys.readouterr().out
    assert code == 1
    assert out.splitlines()[0].startswith("Result: Check the record.")
    assert "Exit code 0 here" not in out


def test_standard_profile_exit_codes_are_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _finish(tmp_path, "COMPLETED_UNVERIFIED", success=True, profile="standard") == 1
    )
    assert "Exit code 0 here" not in capsys.readouterr().out


def test_replay_hints_use_the_launcher_spelling(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.argv", ["/venv/bin/openadapt", "flow", "replay"])
    _finish(tmp_path, "HALTED", success=False, profile="demo")
    out = capsys.readouterr().out
    assert "Next command: openadapt flow explain" in out
    # The run folder is printed verbatim, and its path can itself contain
    # "openadapt-flow" (CI checks the repo out under .../openadapt-flow/).
    # Only the printed command spelling is under test, so drop the path.
    for path in {str(tmp_path.resolve()), str(tmp_path)}:
        out = out.replace(path, "<run-root>")
    assert "openadapt-flow" not in out


# ---------------------------------------------------------------------------
# lint --strict
# ---------------------------------------------------------------------------


def test_strict_lint_explains_a_warnings_only_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = _benign_bundle(tmp_path)
    report = LintReport(
        workflow_name="benign",
        n_steps=1,
        findings=[Finding(severity="warn", code="c", step_id="s", message="m")],
    )
    monkeypatch.setattr(policy_module, "lint_workflow", lambda workflow: report)

    assert main(["lint", str(bundle)]) == 0
    assert "Only warnings were found" not in capsys.readouterr().out

    assert main(["lint", str(bundle), "--strict"]) == 1
    out = capsys.readouterr().out
    assert "Only warnings were found. --strict fails on warnings" in out
    assert f"Next command: openadapt-flow certify {bundle} --policy clinical-write" in (
        out
    )
