"""Run both reference fillers over every case and write REFERENCE_RUNS.json.

Run from ``packages/flow``: ``python -m benchmark.form_tx_heldout4.make_reference_runs [label]``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import naive_filler, reference_human
from .portals import PORTALS
from .runner import run_cases

HERE = Path(__file__).resolve().parent


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in rows if r["case_id"] != PORTALS[r["portal"]].dev_case]
    dev = [r for r in rows if r["case_id"] == PORTALS[r["portal"]].dev_case]
    clean = [r for r in scored if r["expected_behavior"] == "submit_clean"]

    def counts(rs: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "scored_cases": len(rs),
            "correct": sum(r["correct"] for r in rs),
            "wrong": sum(r["submitted"] and not r["correct"] for r in rs),
            "not_submitted": sum(not r["submitted"] for r in rs),
        }

    out = counts(scored)
    out.update(
        clean_cases=len(clean),
        clean_cases_correct=sum(r["correct"] for r in clean),
        dev_correct=all(r["correct"] for r in dev),
        per_portal={p: counts([r for r in scored if r["portal"] == p]) for p in PORTALS},
    )
    return out


def main(argv: list[str]) -> None:
    env = argv[0] if argv else "headless Chromium via Playwright"
    ref = run_cases(reference_human.make_source, include_dev=True)
    naive = run_cases(naive_filler.make_source, include_dev=True)
    doc = {
        "environment": env,
        "summary": {"reference_human": _summary(ref), "naive_filler": _summary(naive)},
        "runs": naive + ref,
    }
    (HERE / "REFERENCE_RUNS.json").write_text(json.dumps(doc, indent=1) + "\n")
    print(json.dumps(doc["summary"], indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
