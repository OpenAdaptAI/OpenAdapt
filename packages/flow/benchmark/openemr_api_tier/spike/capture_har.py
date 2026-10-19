"""Step 1: replay the compiled referral bundle once with network capture on.

Signs in, opens the New Patient form, replays the DOM-identity referral bundle
(the published ``replay-dom`` arm) for one referral, and writes a Playwright HAR
of every request the browser made, with bodies. The HAR is the input to
``compile_api.py``.

    python -m benchmark.openemr_api_tier.spike.capture_har --ref 1 --out <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from benchmark.openemr_api_tier.common import (
    REFERRALS,
    load_demo,
    record_check,
    reset_and_seed,
    spec_for,
    sql_patients,
)
from benchmark.openemr_api_tier.spike_fixture import SpikeFixture

BUNDLE = Path(os.environ.get("REFERRAL_BUNDLE", str(Path.home() / "oa/scratch/replay-speed/dom-identity/out/bundle")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", type=int, default=1)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=False)

    from playwright.sync_api import sync_playwright

    from openadapt_flow.benchmark.openemr_local import OpenEMRPatientOracle
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    demo = load_demo()
    f = SpikeFixture()
    meta = reset_and_seed(f)
    ref = REFERRALS[a.ref - 1]
    spec = spec_for(ref)
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=1,
                              record_har_path=str(out / "referral.har"), record_har_content="embed")
    page = ctx.new_page()
    page.goto(f.ui_base_url)
    backend = demo._BasePlaywrightBackend(page)
    try:
        demo._preauthenticate_browser(backend, f)
        demo._stable_pre_trial_counts(f)
        before_ids = {r["id"] for r in sql_patients(f)}
        oracle = OpenEMRPatientOracle(f.api_base_url, f.token_session("oracle"), spec)
        marks = {"task_start": time.time()}
        rep = Replayer(backend, effect_verifier=oracle.verifier).run(
            Workflow.load(BUNDLE), params=spec.params(), bundle_dir=BUNDLE, run_dir=out / "engine")
        marks["task_end"] = time.time()
        time.sleep(demo.ORACLE_SETTLE_S)
        check = record_check(f, ref, before_ids)
        raw = rep.model_dump(mode="json")
        (out / "capture.json").write_text(json.dumps({
            "ref": ref.key, "params": spec.params(), "fixture": meta, "bundle": str(BUNDLE), "success": raw.get("success"),
            "outcome": str(raw.get("execution_outcome")), "task_s": round(marks["task_end"] - marks["task_start"], 3),
            "steps": len(raw.get("results") or []), "check": check,
            "step_marks": [(r["step_id"], r["intent"][:40]) for r in raw["results"]],
        }, indent=1, default=str))
        print(json.dumps({"ref": ref.key, "success": raw.get("success"), "check": check["readback"],
                          "correct": check["correct"]}))
    finally:
        ctx.close()  # flushes the HAR
        browser.close()
        pw.stop()


if __name__ == "__main__":
    main()
