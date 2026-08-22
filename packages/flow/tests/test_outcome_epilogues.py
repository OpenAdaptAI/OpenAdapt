"""Outcome-aware epilogues (presentation only) on the non-VERIFIED endings.

* tutorial non-VERIFIED runs print the 3-line epilogue (what / why-safe /
  next command); VERIFIED runs keep printing the success-rail block only;
* a failing `lint` exits nonzero AND prints the epilogue;
* `_finish_replay` appends the epilogue for HALTED / COMPLETED_UNVERIFIED
  runs and leaves exit codes untouched (fail-closed semantics identical).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow import tutorial as tutorial_module
from openadapt_flow.__main__ import _finish_replay, main
from openadapt_flow.ir import RunReport, StepResult
from openadapt_flow.tutorial import TutorialResult, outcome_epilogue_lines


def _tutorial_result(root: Path, execution_outcome: str) -> TutorialResult:
    verified = execution_outcome == "VERIFIED"
    return TutorialResult(
        recording_dir=root / "recording",
        bundle_dir=root / "bundle",
        run_dir=root / "run",
        execution_outcome=execution_outcome,
        transaction_outcome=execution_outcome,
        execution_profile="standard",
        transaction_billable=verified,
        model_calls=0,
        effects_required=2,
        effects_confirmed=2 if verified else 0,
        effect_tier=1 if verified else None,
        bundle_digest="d" * 64,
        system_of_record_records=1 if verified else 0,
    )


def _wire(monkeypatch: pytest.MonkeyPatch, result_for) -> None:
    def fake_run_tutorial(work_dir: Path, **kwargs):
        return result_for(Path(work_dir), kwargs)

    monkeypatch.setattr(tutorial_module, "run_tutorial", fake_run_tutorial)


class TestTutorialEpilogue:
    def test_halted_tutorial_prints_explain_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _wire(
            monkeypatch,
            lambda root, kwargs: _tutorial_result(root, "HALTED"),
        )
        assert main(["tutorial", "--out", str(tmp_path / "t")]) == 1
        out = capsys.readouterr().out
        assert "What happened:" in out
        assert "Why this is safe:" in out
        assert f"openadapt-flow explain {tmp_path}/t/run" in out.replace("//", "/")

    def test_unverified_tutorial_points_at_scaffold_verifier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _wire(
            monkeypatch,
            lambda root, kwargs: _tutorial_result(root, "COMPLETED_UNVERIFIED"),
        )
        assert main(["tutorial", "--out", str(tmp_path / "t")]) == 1
        out = capsys.readouterr().out
        assert "scaffold-verifier" in out

    def test_verified_tutorial_prints_no_epilogue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _wire(
            monkeypatch,
            lambda root, kwargs: _tutorial_result(root, "VERIFIED"),
        )
        assert main(["tutorial", "--out", str(tmp_path / "t")]) == 0
        out = capsys.readouterr().out
        assert "What happened:" not in out


class TestLintEpilogue:
    @pytest.fixture()
    def gap_bundle(self, tmp_path: Path) -> Path:
        """A bundle whose unarmed irreversible click fails lint at 'error'."""
        from openadapt_flow.ir import ActionKind, Step, Workflow

        bundle = tmp_path / "gap-bundle"
        Workflow(
            name="gappy",
            steps=[
                Step(
                    id="step_000",
                    intent="click 'Delete record'",
                    action=ActionKind.CLICK,
                    risk="irreversible",
                )
            ],
        ).save(bundle)
        return bundle

    def test_failing_lint_prints_three_line_epilogue(
        self, gap_bundle: Path, capsys
    ) -> None:
        assert main(["lint", str(gap_bundle)]) == 1
        out = capsys.readouterr().out
        assert "What happened: lint found coverage gaps" in out
        assert "Why this is safe:" in out
        assert f"Next command: openadapt-flow certify {gap_bundle}" in out

    def test_clean_lint_prints_no_epilogue(self, tmp_path: Path, capsys) -> None:
        from openadapt_flow.ir import ActionKind, Step, Workflow

        bundle = tmp_path / "clean"
        Workflow(
            name="clean",
            steps=[Step(id="s", intent="click 'Open'", action=ActionKind.CLICK)],
        ).save(bundle)
        assert main(["lint", str(bundle)]) == 0
        assert "What happened:" not in capsys.readouterr().out


class TestReplayFinisherEpilogue:
    """_finish_replay gains lines only; exit codes are unchanged."""

    @staticmethod
    def _run_and_finish(tmp_path: Path, outcome: str, success: bool) -> tuple[int, str]:
        run_dir = tmp_path / f"run-{outcome}"
        report = RunReport(
            workflow_name="wf",
            started_at="2026-08-20T12:00:00+00:00",
            execution_outcome=outcome,
            results=[
                StepResult(
                    step_id="step_004",
                    intent="click 'Save encounter'",
                    ok=success,
                    safety_halt=outcome == "HALTED",
                    effect_verified=False if outcome == "HALTED" else None,
                    postconditions_ok=False if outcome == "HALTED" else None,
                    elapsed_ms=100.0,
                )
            ],
            success=success,
        )
        # render_run_report needs report.json on disk first.
        report.save(run_dir)
        code = _finish_replay(run_dir, report)
        return code, run_dir

    def test_halted_replay_appends_epilogue_exit_code_kept(
        self, tmp_path: Path, capsys
    ) -> None:
        code, _ = self._run_and_finish(tmp_path, "HALTED", False)
        out = capsys.readouterr().out
        assert "What happened: the run stopped at step `step_004`" in out
        assert "Why this is safe:" in out
        assert "Next command: openadapt-flow explain" in out
        assert code == 1

    def test_completed_unverified_names_scaffold_verifier(
        self, tmp_path: Path, capsys
    ) -> None:
        code, _ = self._run_and_finish(tmp_path, "COMPLETED_UNVERIFIED", True)
        out = capsys.readouterr().out
        assert "scaffold-verifier" in out
        assert "can never claim success under Flow" in out
        assert code == 0


def test_outcome_epilogue_lines_shape() -> None:
    lines = outcome_epilogue_lines(what="x", why_safe="y", next_command="z")
    assert len(lines) == 3
    assert lines[0].startswith("What happened:")
    assert lines[1].startswith("Why this is safe:")
    assert lines[2].startswith("Next command:")
