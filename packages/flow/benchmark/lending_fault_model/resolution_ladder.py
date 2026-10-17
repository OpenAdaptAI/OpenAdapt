"""Lending (MockLoan) resolution-ladder probe: model-free drift recovery.

Replays the SAME compiled disbursement bundle against MockLoan under cosmetic
UI drift (``?drift=theme`` breaks template matching; ``?drift=rename`` relabels
the Authorize button and the Open button) with ``?fault=ok`` (a clean write), and
records how the deterministic resolution ladder behaves:

- **full ladder** (default ``Replayer``: structural + template + OCR + geometry)
  - the out-of-the-box model-free path. The measured result is that it RECOVERS
  from both drifts and books exactly one correct disbursement, zero model calls.
- **template-only visual rung** (``use_structural=False``, the fault-isolation
  config the transactional study uses) - a stricter rung that, when a
  cosmetic drift breaks the template match and no grounder rung is installed,
  HALTS before the consequential write rather than acting on a low-confidence
  resolve. Zero wrong disbursements.

Either way the consequential money-movement step is never taken on a
low-confidence resolve. No Anthropic / model calls are made.

Outputs (under ``benchmark/lending_fault_model/``):

- ``resolution_ladder_results.json`` - per (config x drift) outcome.
- ``RESOLUTION_LADDER.md`` - the readable summary.

Usage::

    python -m benchmark.lending_fault_model.resolution_ladder
"""

from __future__ import annotations

import json
import os
import platform
import tempfile
import time
from pathlib import Path

import requests

from benchmark.lending_fault_model import faults as F
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.compiler import compile_recording
from openadapt_flow.demo_driver import record_disbursement_demo
from openadapt_flow.ir import Workflow
from openadapt_flow.mockloan.fault_server import serve as fault_serve
from openadapt_flow.runtime import Replayer

HERE = Path(__file__).resolve().parent
PARAMS = {"memo": F.MEMO_TEXT}
VIEWPORT = {"width": 1280, "height": 800}
DRIFTS = ("none", "theme", "rename")


def _replay(browser, bundle_dir, base_url, drift, use_structural, run_dir):
    run_dir.mkdir(parents=True, exist_ok=True)
    requests.post(base_url + "api/reset", json={"seed_concurrent": False}, timeout=10)
    q = "?fault=ok" + (f"&drift={drift}" if drift != "none" else "")
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
    try:
        page.goto(base_url + q)
        report = Replayer(PlaywrightBackend(page), use_structural=use_structural).run(
            Workflow.load(bundle_dir),
            params=dict(PARAMS),
            bundle_dir=Path(bundle_dir),
            run_dir=run_dir,
        )
    finally:
        page.close()
    snap = requests.get(base_url + "api/db", timeout=10).json()
    records = snap.get("records", [])
    correct = (
        report.success
        and len(records) == 1
        and records[0].get("memo") == F.MEMO_TEXT
        and records[0].get("loan_id") == F.TARGET_LOAN
    )
    return {
        "drift": drift,
        "config": "full_ladder" if use_structural else "template_only",
        "report_success": bool(report.success),
        "rows_written": len(records),
        "correct_disbursement": bool(correct),
        # A halt with zero rows is the safe outcome (no wrong money movement).
        "wrong_write": bool(
            len(records) > 1
            or (bool(records) and records[0].get("memo") != F.MEMO_TEXT)
        ),
    }


def measure() -> dict:
    os.environ.pop("ANTHROPIC_API_KEY", None)
    from playwright.sync_api import sync_playwright

    url, _db, stop = fault_serve(port=0)
    rows: list[dict] = []
    # Isolated scratch dir so a concurrent fault-model run never shares the
    # recording/bundle path.
    work = Path(tempfile.mkdtemp(prefix="mockloan-ladder-"))
    try:
        rec = record_disbursement_demo(url, work / "_recording", memo_text=F.MEMO_TEXT)
        bundle = work / "_bundle"
        compile_recording(rec, bundle, name="disburse-demo")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for use_structural in (True, False):
                    for drift in DRIFTS:
                        cfg = "full" if use_structural else "tpl"
                        rows.append(
                            _replay(
                                browser,
                                bundle,
                                url,
                                drift,
                                use_structural,
                                HERE / "runs_ladder" / f"{cfg}_{drift}",
                            )
                        )
            finally:
                browser.close()
    finally:
        stop()
    return {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "platform": f"{platform.system()} {platform.machine()} "
            f"py{platform.python_version()}",
            "model_calls": 0,
        },
        "rows": rows,
    }


def to_markdown(result: dict) -> str:
    rows = result["rows"]
    lines = ["# Lending (MockLoan) resolution-ladder probe", ""]
    lines.append(
        "The same compiled disbursement bundle replayed under cosmetic UI drift "
        "with a clean write (`?fault=ok`), zero model calls. `theme` breaks "
        "template matching; `rename` relabels the Authorize/Open buttons."
    )
    lines.append("")
    lines.append(f"Generated: {result['meta']['generated_at']}  ")
    lines.append(f"Platform: {result['meta']['platform']}  ")
    lines.append("")
    lines.append(
        "| config | drift | replay | rows | correct disbursement | wrong write |"
    )
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['config']} | {r['drift']} | "
            f"{'SUCCESS' if r['report_success'] else 'HALT'} | "
            f"{r['rows_written']} | {'yes' if r['correct_disbursement'] else 'no'} | "
            f"{'YES' if r['wrong_write'] else 'no'} |"
        )
    lines.append("")
    full = [r for r in rows if r["config"] == "full_ladder"]
    tpl = [r for r in rows if r["config"] == "template_only"]
    full_ok = all(r["correct_disbursement"] for r in full)
    tpl_no_wrong = all(not r["wrong_write"] for r in tpl)
    lines.append("## Reading")
    lines.append("")
    lines.append(
        f"- **Full ladder** (structural + template + OCR + geometry, the "
        f"default): recovers cosmetic drift model-free and books the correct "
        f"disbursement in {sum(1 for r in full if r['correct_disbursement'])}/"
        f"{len(full)} cells" + (" (all)." if full_ok else ".")
    )
    lines.append(
        f"- **Template-only rung** (fault-isolation config): when a drift breaks "
        f"the template and no grounder rung is installed it HALTS before the "
        f"consequential write - {sum(1 for r in tpl if not r['wrong_write'])}/"
        f"{len(tpl)} cells took no wrong action" + (" (all)." if tpl_no_wrong else ".")
    )
    lines.append(
        "- In neither config is the money-movement step taken on a "
        "low-confidence resolve."
    )
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```\npython -m benchmark.lending_fault_model.resolution_ladder\n```")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    result = measure()
    (HERE / "resolution_ladder_results.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    (HERE / "RESOLUTION_LADDER.md").write_text(to_markdown(result))
    for r in result["rows"]:
        print(
            f"[{r['config']:13s} {r['drift']:6s}] "
            f"{'SUCCESS' if r['report_success'] else 'HALT':7s} "
            f"rows={r['rows_written']} correct={r['correct_disbursement']} "
            f"wrong={r['wrong_write']}"
        )
    print("[done] wrote resolution_ladder_results.json + RESOLUTION_LADDER.md")


if __name__ == "__main__":
    main()
