"""Run a filler over cases and score the oracle's stored records."""

from __future__ import annotations

import json
import sys
import time
import traceback
from typing import Any, Callable

from .drivers.base import DriverError, Source, oracle
from .portals import PORTALS
from .scoring import load_answer_key, score_case

MARK = "FORM_TX_HELDOUT_RESULTS"


def driver_for(portal: str) -> Any:
    import importlib

    return importlib.import_module(f".drivers.{portal}", __package__)


def run_cases(
    make_source: Callable[[str, str], Source],
    portals: list[str] | None = None,
    include_dev: bool = False,
    only: list[str] | None = None,
) -> list[dict[str, Any]]:
    from playwright.sync_api import sync_playwright

    results: list[dict[str, Any]] = []
    ak = load_answer_key()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for pid in portals or list(PORTALS):
                spec = PORTALS[pid]
                server, base = spec.start()
                drv = driver_for(pid)
                try:
                    cases = ([spec.dev_case] if include_dev else []) + spec.cases
                    for cid in cases:
                        if only and cid not in only:
                            continue
                        oracle(base, "/__oracle/load_case", {"case_id": cid})
                        src = make_source(pid, cid)
                        ctx = browser.new_context()
                        page = ctx.new_page()
                        page.set_default_timeout(15000)
                        t0 = time.time()
                        err = None
                        try:
                            outcome = drv.run(page, base, src)
                        except Exception as exc:  # record and continue
                            outcome = "error"
                            err = f"{type(exc).__name__}: {exc}"
                            if not isinstance(exc, DriverError):
                                err += "\n" + traceback.format_exc(limit=3)
                        ctx.close()
                        recs = oracle(base, "/__oracle/submissions")
                        score = score_case(cid, recs)
                        row = {
                            "portal": pid,
                            "case_id": cid,
                            "filler": src.kind,
                            "outcome": outcome,
                            "seconds": round(time.time() - t0, 1),
                            "events": src.events,
                            **score,
                            "traps": ak["cases"][cid]["traps"],
                        }
                        if err:
                            row["error"] = err
                        if hasattr(src, "check_operator_labels"):
                            row["operator_labels_not_seen"] = src.check_operator_labels()
                        if getattr(src, "problems", None):
                            row["solvability_problems"] = src.problems
                        results.append(row)
                        print(json.dumps({k: row[k] for k in ("portal", "case_id", "outcome", "correct")}), file=sys.stderr)
                finally:
                    server.shutdown()
        finally:
            browser.close()
    return results


def emit(results: list[dict[str, Any]]) -> None:
    print(f"<<<{MARK}")
    print(json.dumps(results, indent=1))
    print(f"{MARK}>>>")
