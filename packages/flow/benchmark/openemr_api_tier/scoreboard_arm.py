"""Scoreboard arm 5 (``program_api``) and its like-for-like ``program`` rows.

Runs the OpenEMR referral through the scoreboard protocol
(``benchmark/scoreboard/PROTOCOL.md``, task ``openemr_referral``): the same
bundle, replayed with API steps off (arm ``program``) and on (arm
``program_api``), under one run id, so the two compare like for like. Rows are
``scoreboard.schema.Row`` in ``scoreboard/raw/openemr_referral/<arm>/<run_id>/``.

- Referrals: the October study's eight (seven new patients, and r6, the
  possible duplicate of the seeded Rosa Delgado chart). Five blocks of four
  per arm; each block resets the fixture and seeds Rosa; arms interleave and
  alternate order by block.
- Read-back: the study's direct SQL rule. A new referral is ``correct`` with
  one new chart matching 12 of 12 fields; r6 is right when no chart is
  created (``readback=missing``, outcome ``safe_stop``).
- Load: each item waits until the 1-minute load is under 12 and records the
  load at the start and the end (``timed``).
- The bundle is compiled from a recording made with network capture
  (``feature_e2e.py record`` and ``compile``); its API step is admitted by the
  harness under the name ``benchmark-fixture``.

    PYTHONPATH=packages/flow python -m benchmark.openemr_api_tier.scoreboard_arm \\
        --bundle <bundle> --run-id api-tier-2026-10-19
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from benchmark.openemr_api_tier.common import REFERRALS, load_demo, reset_and_seed
from benchmark.openemr_api_tier.feature_e2e import run_one, wait_quiet
from benchmark.openemr_api_tier.spike_fixture import SpikeFixture

TASK = "openemr_referral"
ARMS = {"browser": "program", "api": "program_api"}
SCOREBOARD = Path(__file__).resolve().parents[1] / "scoreboard"


def readback_of(res: dict[str, Any]) -> str:
    if res.get("mismatch"):
        return "missing" if res.get("new_charts") == 0 else "duplicate"
    n = res.get("new_charts")
    if n == 0:
        return "missing"
    if n and n > 1:
        return "duplicate"
    return "correct" if res.get("correct") else "wrong"


def row_for(schema: Any, run_id: str, item: int, res: dict[str, Any], arm: str,
            load_start: float, load_end: float) -> Any:
    fault = "duplicate_check" if res.get("mismatch") else "none"
    reported = bool(res.get("engine_success"))
    readback = readback_of(res)
    outcome = schema.classify(reported, readback, fault)
    if res.get("mismatch") and readback == "missing":
        outcome = "safe_stop"
    detail = json.dumps({k: res.get(k) for k in ("actuation", "api_fallback", "transaction_outcome", "error")})
    return schema.Row(
        task=TASK, arm=ARMS[arm], run_id=run_id, item=item, attempt=1, fault=fault,
        reported_success=reported, readback=readback, outcome=outcome, model_calls=0,
        input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0, cost_usd=0.0,
        item_s=float(res.get("engine_s") or 0.0), wall_s=float(res.get("wall_s") or 0.0),
        approvals_asked=0, host_load_1m=load_start, timed=schema.timed(load_start, load_end),
        detail=detail[:300],
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--blocks", type=int, default=5)
    ap.add_argument("--per-block", type=int, default=4)
    ap.add_argument("--work", required=True, help="Scratch directory for per-run evidence")
    a = ap.parse_args()

    import sys

    from benchmark.scoreboard import schema
    from playwright.sync_api import sync_playwright

    import openadapt_flow.vision.ocr  # noqa: F401
    from openadapt_flow.ir import Workflow

    ocr = sys.modules["openadapt_flow.vision.ocr"]
    bundle, work = Path(a.bundle), Path(a.work)
    save = Workflow.load(bundle).steps[-1]
    admitted = bool(save.api_binding is not None and save.api_binding.admitted())
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    for arm in ARMS.values():
        schema.write_meta(
            SCOREBOARD / "raw" / TASK / arm / a.run_id, task=TASK, arm=arm,
            note=("OpenEMR referral, one bundle compiled from a recording with network capture; "
                  f"API steps {'on' if arm == 'program_api' else 'off'}; fixture: private copy of the "
                  "pinned referral fixture (spike_fixture.py)"),
            api_step_admitted=admitted, api_step_admitted_by=(save.api_binding.admission.admitted_by
                                                             if admitted else None),
            harness_git_head=sha, bundle_save_step=save.id,
        )
    demo, f = load_demo(), SpikeFixture()
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ocr._get_engine()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    page.goto(f.ui_base_url)
    ocr.ocr(page.screenshot(type="png"))
    ctx.close()
    env = {"demo": demo, "f": f, "browser": browser, "ocr": ocr}
    try:
        for b in range(a.blocks):
            for arm in (["browser", "api"] if b % 2 == 0 else ["api", "browser"]):
                reset_and_seed(f)
                for pos in range(a.per_block):
                    idx = ((b * a.per_block + pos) % len(REFERRALS)) + 1
                    gate = wait_quiet()
                    load_start = schema.load_1m()
                    out = work / f"b{b + 1:02d}-{arm}-{pos + 1}-{REFERRALS[idx - 1].key}"
                    res = run_one(env, arm, idx, bundle, out)
                    load_end = schema.load_1m()
                    res["gate"] = gate
                    (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
                    row = row_for(schema, a.run_id, (b + 1) * 100 + pos + 1, res, arm, load_start, load_end)
                    schema.append(SCOREBOARD / "raw" / TASK / ARMS[arm] / a.run_id / "items.csv", row)
                    print(json.dumps({"arm": row.arm, "item": row.item, "ref": res["ref"], "outcome": row.outcome,
                                      "readback": res.get("readback"), "wall_s": row.wall_s, "timed": row.timed,
                                      "actuation": res.get("actuation"), "fallback": res.get("api_fallback")}),
                          flush=True)
                    time.sleep(0.2)
    finally:
        browser.close()
        pw.stop()


if __name__ == "__main__":
    main()
