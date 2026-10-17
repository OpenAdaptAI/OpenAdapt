"""CLI wiring and fail-loud contract for ``tutorial --break-it``.

Browser-free: the tutorial's heavy loop is faked, and the real end-to-end
behavior (record, run, halt) is owned by ``tests/e2e/test_tutorial_break_it_e2e.py``.
What is proven here, cheaply and on every unit run:

* the flag reaches :func:`openadapt_flow.tutorial.run_tutorial` and its result
  drives the printed narrative -- screen claim, refuted verifier read, HALTED
  outcome, evidence path, and the no-receipt rule -- from run evidence, not
  from a script;
* the plain tutorial ends with a pointer at ``--break-it`` as the next step;
* :func:`openadapt_flow.tutorial._run_break_it` raises loudly when the engine
  does NOT halt on the injected fault, and extracts the narrative facts from a
  halted report when it does.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import openadapt_flow.mockmed.fault_server as fault_server_module
import openadapt_flow.report as report_module
from openadapt_flow import tutorial as tutorial_module
from openadapt_flow.__main__ import main
from openadapt_flow.tutorial import (
    BreakItResult,
    TutorialError,
    TutorialResult,
    _run_break_it,
)

# ---------------------------------------------------------------------------
# CLI narrative
# ---------------------------------------------------------------------------


def _verified_result(
    root: Path, *, break_it: Optional[BreakItResult]
) -> TutorialResult:
    return TutorialResult(
        recording_dir=root / "recording",
        bundle_dir=root / "bundle",
        run_dir=root / "run",
        execution_outcome="VERIFIED",
        transaction_outcome="VERIFIED",
        execution_profile="standard",
        transaction_billable=True,
        model_calls=0,
        effects_required=2,
        effects_confirmed=2,
        effect_tier=1,
        bundle_digest="d" * 64,
        system_of_record_records=1,
        receipt_paths={
            "png": root / "run" / "receipt.png",
            "json": root / "run" / "receipt.json",
        },
        break_it=break_it,
    )


def _broken_result(root: Path) -> BreakItResult:
    return BreakItResult(
        run_dir=root / "run-broken",
        report_path=root / "run-broken" / "REPORT.md",
        fault="optimistic",
        execution_outcome="HALTED",
        transaction_outcome="RECONCILIATION_REQUIRED",
        transaction_billable=False,
        screen_claimed_success=True,
        screen_claim_text="Encounter saved",
        effects_required=2,
        effects_refuted=1,
        halt_reason="record_written refuted against the rest system of record",
        system_of_record_records=0,
    )


def test_break_it_flag_reaches_run_tutorial_and_drives_the_narrative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def fake_run_tutorial(work_dir: Path, **kwargs: Any) -> TutorialResult:
        seen.update(kwargs)
        return _verified_result(Path(work_dir), break_it=_broken_result(Path(work_dir)))

    monkeypatch.setattr(tutorial_module, "run_tutorial", fake_run_tutorial)
    assert main(["tutorial", "--break-it", "--out", str(tmp_path / "t")]) == 0
    assert seen["break_it"] is True

    out = capsys.readouterr().out
    # The two runs are labeled so VERIFIED is never misread as the broken one.
    assert "clean run" in out
    assert "break-it: the same certified bundle" in out
    # The narrative's three beats, from evidence fields.
    assert "every on-screen check passed" in out
    assert '"Encounter saved"' in out
    assert "1/2 declared effect(s) REFUTED" in out
    assert "0 record(s)" in out
    assert "HALTED at the consequential step" in out
    assert "RECONCILIATION_REQUIRED" in out
    # The engine's own halt reason is quoted, not paraphrased.
    assert "record_written refuted" in out
    # Where the evidence lives, and what may NOT be claimed.
    assert str(tmp_path / "t" / "run-broken" / "REPORT.md") in out
    assert "NOT a success receipt" in out
    assert "No shareable receipt for the halted run" in out
    # And what to do next.
    assert "record your own workflow" in out


def test_plain_tutorial_points_at_break_it_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, Any] = {}

    def fake_run_tutorial(work_dir: Path, **kwargs: Any) -> TutorialResult:
        seen.update(kwargs)
        return _verified_result(Path(work_dir), break_it=None)

    monkeypatch.setattr(tutorial_module, "run_tutorial", fake_run_tutorial)
    assert main(["tutorial", "--out", str(tmp_path / "t")]) == 0
    assert seen["break_it"] is False

    out = capsys.readouterr().out
    assert "REPORT.md" in out
    assert "receipt.json" in out
    assert "openadapt-flow tutorial --break-it" in out
    assert "break-it: the same certified bundle" not in out


# ---------------------------------------------------------------------------
# _run_break_it: fail loud, extract honestly
# ---------------------------------------------------------------------------


def _fake_workflow() -> Any:
    effect = SimpleNamespace(
        kind=SimpleNamespace(value="record_written"),
        needs_operator_confirmation=False,
    )
    step = SimpleNamespace(id="step_005", risk="irreversible", effects=[effect])
    return SimpleNamespace(steps=[step])


def _wire_break_it_fakes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, report: Any
) -> None:
    monkeypatch.setattr(
        fault_server_module,
        "serve",
        lambda: ("http://127.0.0.1:9/", None, lambda: None),
    )
    monkeypatch.setattr(tutorial_module, "_records", lambda base_url: [])
    monkeypatch.setattr(
        tutorial_module, "run_tutorial_workflow", lambda **kwargs: report
    )
    monkeypatch.setattr(
        report_module,
        "render_run_report",
        lambda run_dir: Path(run_dir) / "REPORT.md",
    )


def test_run_break_it_refuses_an_uncaught_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected fault the engine misses is a product failure, not a demo."""

    report = SimpleNamespace(execution_outcome="VERIFIED")
    _wire_break_it_fakes(monkeypatch, tmp_path, report)
    with pytest.raises(TutorialError, match="FAILED to catch the injected fault"):
        _run_break_it(
            workflow=_fake_workflow(),
            bundle_dir=tmp_path / "bundle",
            run_dir=tmp_path / "run-broken",
            headed=False,
            say=lambda message: None,
        )


def test_run_break_it_extracts_the_narrative_from_the_halted_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_result = SimpleNamespace(
        step_id="step_005",
        postconditions_ok=True,
        effect_evidence=[SimpleNamespace(final_verdict="refuted")],
    )
    report = SimpleNamespace(
        execution_outcome="HALTED",
        transaction_outcome="RECONCILIATION_REQUIRED",
        transaction_billable=False,
        results=[save_result],
        halt=SimpleNamespace(
            observed_texts=["MockMed", "Encountersaved-"],
            reason="record_written refuted -- nothing landed",
        ),
        outcome_envelope=SimpleNamespace(required_contracts=SimpleNamespace(effect=2)),
    )
    _wire_break_it_fakes(monkeypatch, tmp_path, report)

    broken = _run_break_it(
        workflow=_fake_workflow(),
        bundle_dir=tmp_path / "bundle",
        run_dir=tmp_path / "run-broken",
        headed=False,
        say=lambda message: None,
    )
    assert broken.execution_outcome == "HALTED"
    assert broken.fault == "optimistic"
    assert broken.screen_claimed_success is True
    assert broken.screen_claim_text == "Encountersaved-"
    assert broken.effects_required == 2
    assert broken.effects_refuted == 1
    assert broken.system_of_record_records == 0
    assert broken.halt_reason == "record_written refuted -- nothing landed"
    assert broken.report_path == tmp_path / "run-broken" / "REPORT.md"
