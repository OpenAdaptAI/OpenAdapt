"""The closing "next steps" block after a plain VERIFIED tutorial run.

Browser-free, mirroring ``tests/test_cli_tutorial_break_it.py``: the
tutorial's heavy loop is faked and only the CLI wiring is proven here.

* :func:`openadapt_flow.tutorial._next_steps_block` carries the three
  destinations the flagship README points at;
* the plain VERIFIED tutorial prints the block after the receipt paths;
* a ``--break-it`` run and a non-VERIFIED run do NOT print it -- the block
  belongs to the success rail only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from openadapt_flow import tutorial as tutorial_module
from openadapt_flow.__main__ import main
from openadapt_flow.tutorial import (
    BreakItResult,
    TutorialResult,
    _next_steps_block,
)

EXECUTE_URL = "https://openadapt.ai/execute"
QUALIFY_URL = "https://openadapt.ai/qualify"
DISCORD_URL = "https://discord.gg/yF527cQbDG"


def _result(
    root: Path,
    *,
    execution_outcome: str = "VERIFIED",
    break_it: Optional[BreakItResult] = None,
) -> TutorialResult:
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
        receipt_paths=(
            {
                "png": root / "run" / "receipt.png",
                "json": root / "run" / "receipt.json",
            }
            if verified
            else {}
        ),
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


def _wire(monkeypatch: pytest.MonkeyPatch, result_for: Any) -> None:
    def fake_run_tutorial(work_dir: Path, **kwargs: Any) -> TutorialResult:
        return result_for(Path(work_dir), kwargs)

    monkeypatch.setattr(tutorial_module, "run_tutorial", fake_run_tutorial)


def test_next_steps_block_carries_the_three_readme_urls() -> None:
    block = _next_steps_block()
    assert EXECUTE_URL in block
    assert QUALIFY_URL in block
    assert DISCORD_URL in block
    assert "no model call" in block


def test_verified_tutorial_prints_the_block_after_the_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _wire(monkeypatch, lambda root, kwargs: _result(root))
    assert main(["tutorial", "--out", str(tmp_path / "t")]) == 0

    out = capsys.readouterr().out
    assert _next_steps_block() in out
    # After the receipt paths, at the very end of the run's story.
    assert out.index("receipt.json") < out.index(EXECUTE_URL)


def test_break_it_run_does_not_print_the_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _wire(
        monkeypatch,
        lambda root, kwargs: _result(root, break_it=_broken_result(root)),
    )
    assert main(["tutorial", "--break-it", "--out", str(tmp_path / "t")]) == 0

    out = capsys.readouterr().out
    assert EXECUTE_URL not in out
    assert QUALIFY_URL not in out
    assert DISCORD_URL not in out


def test_unverified_tutorial_does_not_print_the_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _wire(
        monkeypatch,
        lambda root, kwargs: _result(root, execution_outcome="HALTED"),
    )
    assert main(["tutorial", "--out", str(tmp_path / "t")]) == 1

    out = capsys.readouterr().out
    assert EXECUTE_URL not in out
    assert QUALIFY_URL not in out
    assert DISCORD_URL not in out
