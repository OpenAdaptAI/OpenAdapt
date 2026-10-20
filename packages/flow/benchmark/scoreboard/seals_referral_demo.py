"""Run the site demo's four referrals with the record check on, and seal each run.

    PYTHONPATH=<#1269 checkout>/packages/flow python -m scoreboard.seals_referral_demo \\
        --bundle <api-tier bundle> --out raw/openemr_referral/seals/<date>

The landing page shows the compiled OpenEMR referral program on four referrals:
three it saves, and one possible duplicate it stops on. This reruns exactly
that, with no model calls:

* referrals r1 (Nadia Brennan), r2 (Marcus Feld) and r3 (Ines Carvalho), and
  r6 (Rosa Delgado, 1958-03-22), the possible duplicate of the seeded chart
  Rosa Delgado 1958-03-27, which is the site's "referral 4";
* the #1269 harness's own run (``feature_e2e.run_one``) on its private
  OpenEMR copy, with API steps off (the screen program), and the engine's
  record check: OpenEMR's REST API read back through the read-only oracle
  client;
* each run's ``report.json`` signed with an offline Seal
  (``openadapt.local-seal/v1``, this machine's key), checked with
  ``openadapt-flow local-seal verify RUN_DIR --keys seal-key.json``.

The runs use no named execution profile, so the outcome is what the engine
records for that: a completed run with every record check confirmed, not
``VERIFIED`` (that needs the standard profile, whose gate refuses this bundle
today). The Seal copies the outcome; it never upgrades it. Screenshots stay
out of the commit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REFS = (1, 2, 3, 6)  # the site's referrals 1 to 4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    import openadapt_flow.vision.ocr  # noqa: F401
    from benchmark.openemr_api_tier.common import REFERRALS, load_demo, reset_and_seed
    from benchmark.openemr_api_tier.feature_e2e import run_one
    from benchmark.openemr_api_tier.spike_fixture import SpikeFixture
    from openadapt_flow.offline_seal import public_key_entry, seal_run

    ocr = sys.modules["openadapt_flow.vision.ocr"]
    out = args.out
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    demo, f = load_demo(), SpikeFixture()
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ocr._get_engine()
    env = {"demo": demo, "f": f, "browser": browser, "ocr": ocr}
    summary = []
    try:
        seed = reset_and_seed(f)
        for n, idx in enumerate(REFS, start=1):
            ref = REFERRALS[idx - 1]
            item = out / f"referral-{n}-{ref.key}"
            res = run_one(env, "browser", idx, args.bundle, item)
            run_dir = item / "engine"
            # Keep the report and the step evidence that is text; no screenshots.
            for png in run_dir.rglob("*.png"):
                png.unlink()
            seal = seal_run(run_dir, enqueue=False)
            summary.append(
                {
                    "referral": n,
                    "ref": ref.key,
                    "run_dir": str(run_dir.relative_to(out)),
                    "outcome": res.get("outcome"),
                    "transaction_outcome": res.get("transaction_outcome"),
                    "engine_success": res.get("engine_success"),
                    "readback": res.get("readback"),
                    "new_charts": res.get("new_charts"),
                    "mismatch_case": ref.mismatch,
                    "error": res.get("error"),
                    "seal_subject_sha256": seal.subject_sha256,
                    "seal_key_id": seal.key_id,
                }
            )
            print(json.dumps(summary[-1]), flush=True)
    finally:
        browser.close()
        pw.stop()
    key = public_key_entry()
    (out / "seal-key.json").write_text(json.dumps([key], indent=2) + "\n")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    (out / "summary.json").write_text(
        json.dumps(
            {
                "note": (
                    "offline Seal, signed by this machine's key; no named execution "
                    "profile; record check: OpenEMR REST read-back"
                ),
                "engine_git_head": head,
                "bundle": str(args.bundle),
                "seed": seed,
                "verify": "openadapt-flow local-seal verify <run_dir> --keys seal-key.json",
                "runs": summary,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
