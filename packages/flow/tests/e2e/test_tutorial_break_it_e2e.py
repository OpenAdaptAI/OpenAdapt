"""``tutorial --break-it``, end to end: the engine catches the fault it names.

The clean free path is pinned by :mod:`test_free_path_e2e` (golden task).  This
module covers the OTHER half of the tutorial's demonstration: the SAME
certified bundle, rerun against a backend that rejects the write AFTER the
application has painted its success banner (``?fault=optimistic``).  Every
on-screen check passes; only the independent read of the system of record can
tell the truth -- and the run must end HALTED because of it.

What is asserted, on one real browser run:

* the clean half still terminates ``VERIFIED`` with its receipt;
* the broken half terminates ``HALTED`` / ``RECONCILIATION_REQUIRED``, not
  billable, with the declared ``record_written`` effect REFUTED at
  INDEPENDENT_SYSTEM strength while zero rows exist in the store;
* the lie was real: the consequential step's on-screen postconditions all
  PASSED on the broken run -- the halt did not come from the screen;
* the caught fault leaves a clearly-labeled LOCAL report
  (``run-broken/REPORT.md`` leads with ``HALTED``) and NO shareable receipt,
  and ``report-run`` still refuses the halted run -- the success rail was not
  weakened to make the fault showable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.__main__ import main as cli_main
from openadapt_flow.ir import RunReport
from openadapt_flow.tutorial import TutorialResult, run_tutorial


@pytest.fixture(scope="module")
def break_it_path(tmp_path_factory: pytest.TempPathFactory) -> TutorialResult:
    """Run the tutorial once with ``break_it=True`` for the whole module."""

    return run_tutorial(tmp_path_factory.mktemp("break-it"), break_it=True)


def _report(run_dir: Path) -> RunReport:
    return RunReport.model_validate_json(
        (run_dir / "report.json").read_text(encoding="utf-8")
    )


def test_the_clean_half_still_verifies(break_it_path: TutorialResult) -> None:
    """--break-it prepends, never replaces, the clean VERIFIED run."""

    assert break_it_path.execution_outcome == "VERIFIED"
    assert break_it_path.receipt_paths, "the clean half stopped emitting its receipt"
    assert break_it_path.receipt_paths["json"].is_file()


def test_the_engine_halts_on_the_injected_fault(
    break_it_path: TutorialResult,
) -> None:
    """The aha itself: same bundle, lying backend, HALT -- not success."""

    broken = break_it_path.break_it
    assert broken is not None
    assert broken.fault == "optimistic"
    assert broken.execution_outcome == "HALTED"
    assert broken.transaction_outcome == "RECONCILIATION_REQUIRED"
    assert broken.transaction_billable is not True
    # Nothing landed: the write the screen claimed does not exist.
    assert broken.system_of_record_records == 0
    assert broken.effects_refuted >= 1
    # The engine's own explanation names the refuted contract, not a guess.
    assert "record_written" in broken.halt_reason
    assert "refuted" in broken.halt_reason


def test_the_screen_really_did_lie(break_it_path: TutorialResult) -> None:
    """The halt came from the independent verifier, not from the screen.

    If the on-screen postconditions had failed, the run would have halted for
    an ordinary reason and the demonstration would prove nothing about effect
    verification.  The whole point is that the screen PASSED.
    """

    broken = break_it_path.break_it
    assert broken is not None
    assert broken.screen_claimed_success is True

    report = _report(broken.run_dir)
    [save] = [step for step in report.results if step.risk == "irreversible"]
    assert save.postconditions_ok is True
    assert save.safety_halt is True
    refuted = [
        evidence
        for evidence in save.effect_evidence
        if evidence.final_verdict == "refuted"
    ]
    assert refuted, "the save step retained no refuted effect evidence"
    assert all(evidence.verification_tier == 1 for evidence in refuted)


def test_the_caught_fault_is_showable_but_not_a_success(
    break_it_path: TutorialResult, tmp_path: Path
) -> None:
    """A clearly-labeled LOCAL report exists; the success rail does not bend."""

    broken = break_it_path.break_it
    assert broken is not None

    # The local evidence: REPORT.md leads with the honest outcome.
    assert broken.report_path.is_file()
    first_line = broken.report_path.read_text(encoding="utf-8").split("\n", 1)[0]
    assert "HALTED" in first_line
    assert "VERIFIED" not in first_line

    # No shareable receipt was emitted for the halted run...
    assert not (broken.run_dir / "receipt.json").exists()
    assert not (broken.run_dir / "receipt.png").exists()

    # ...and report-run still refuses it (exit 0, nothing emitted).
    refused = tmp_path / "refused"
    assert cli_main(["report-run", str(broken.run_dir), "--receipt", str(refused)]) == 0
    assert not refused.exists(), "a HALTED run emitted a receipt"


def test_the_broken_run_is_separate_from_the_clean_evidence(
    break_it_path: TutorialResult,
) -> None:
    """The halt lands in run-broken/ and never contaminates the clean run."""

    broken = break_it_path.break_it
    assert broken is not None
    assert broken.run_dir != break_it_path.run_dir
    assert broken.run_dir.name == "run-broken"
    clean = _report(break_it_path.run_dir)
    assert clean.execution_outcome == "VERIFIED"
