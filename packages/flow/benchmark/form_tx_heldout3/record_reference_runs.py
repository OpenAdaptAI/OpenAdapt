"""Run both reference fillers over every case and write ``REFERENCE_RUNS.json``.

    python -m benchmark.form_tx_heldout3.record_reference_runs
"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path
from typing import Any

from . import naive_filler, reference_human
from .portals import PORTALS
from .runner import run_cases

HERE = Path(__file__).resolve().parent


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dev = {spec.dev_case for spec in PORTALS.values()}
    scored = [r for r in rows if r["case_id"] not in dev]
    clean = [r for r in scored if "clean" in r["traps"]]
    per_portal = {}
    for pid in PORTALS:
        mine = [r for r in scored if r["portal"] == pid]
        per_portal[pid] = {"scored_cases": len(mine), "correct": sum(r["correct"] for r in mine)}
    return {
        "scored_cases": len(scored),
        "correct": sum(r["correct"] for r in scored),
        "wrong": sorted(r["case_id"] for r in scored if r["submitted"] and not r["correct"]),
        "not_submitted": sorted(r["case_id"] for r in scored if not r["submitted"]),
        "clean_cases": len(clean),
        "clean_cases_correct": sum(r["correct"] for r in clean),
        "dev_correct": sum(r["correct"] for r in rows if r["case_id"] in dev),
        "per_portal": per_portal,
    }


def main() -> None:
    from importlib.metadata import version

    pw_version = version("playwright")

    runs: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for name, mod in (("reference_human", reference_human), ("naive_filler", naive_filler)):
        rows = run_cases(mod.make_source, include_dev=True)
        for r in rows:
            r["filler_name"] = name
        runs.extend(rows)
        summary[name] = _summary(rows)
    env = f"{platform.system()} {platform.release()}; Python {platform.python_version()}; Playwright {pw_version}; headless Chromium"
    out = {"environment": env, "summary": summary, "runs": runs}
    (HERE / "REFERENCE_RUNS.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(summary, indent=1), file=sys.stderr)


if __name__ == "__main__":
    main()
