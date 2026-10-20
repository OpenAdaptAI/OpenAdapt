"""The verification-coverage benchmark's summary (benchmark/verification_coverage)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmark"


@pytest.fixture
def summarize(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(BENCH))
    sys.modules.pop("verification_coverage.summarize", None)
    from verification_coverage import summarize as mod

    monkeypatch.setattr(mod, "HERE", tmp_path)
    return mod


def _write(tmp_path: Path, rows: list[dict[str, str]], complete: bool = True) -> None:
    run = tmp_path / "results" / "openemr" / "r1"
    run.mkdir(parents=True)
    (run / "meta.json").write_text(
        json.dumps(
            {
                "complete": complete,
                "engines": {"e1365": "a" * 40, "main": "b" * 40, "after": "c" * 40},
            }
        )
    )
    with (run / "items.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _row(
    fault: str, truth: str, *outcomes: str, presave: str = "agree"
) -> dict[str, str]:
    return {
        "item": "1",
        "fault": fault,
        "ground_truth": truth,
        "e1365_outcome": outcomes[0],
        "main_outcome": outcomes[1],
        "after_outcome": outcomes[2],
        "presave_check": presave,
    }


def test_catches_and_false_alarms_are_counted_apart(summarize, tmp_path) -> None:
    v, r = "verified", "reconciliation_required"
    _write(
        tmp_path,
        [
            _row("ok", "correct", v, v, v),
            _row("collateral_edit", "collateral_edit", v, r, r),
            _row("concurrent_edit_other", "correct", v, r, v),
            _row("misread_source", "wrong_vs_source", v, v, r, presave="hold"),
        ],
    )
    text = summarize.render()
    assert (
        "| `collateral_edit` | 1 | 0/1 caught | 1/1 caught | 1/1 caught | 0/1 |" in text
    )
    assert (
        "| `concurrent_edit_other` | 1 | 0/1 false alarms | 1/1 false alarms | 0/1 false alarms | 0/1 |"
        in text
    )
    assert (
        "| `misread_source` | 1 | 0/1 caught | 0/1 caught | 1/1 caught | 1/1 |" in text
    )
    assert "**0/2** | **1/2** | **2/2**" in text


def test_an_incomplete_run_is_not_reported(summarize, tmp_path) -> None:
    _write(
        tmp_path,
        [_row("ok", "correct", "verified", "verified", "verified")],
        complete=False,
    )
    assert "No complete run yet." in summarize.render()


def test_the_benchmark_modules_import(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(BENCH))
    import importlib

    for name in ("exec_mariadb", "summarize"):
        importlib.import_module(f"verification_coverage.{name}")
