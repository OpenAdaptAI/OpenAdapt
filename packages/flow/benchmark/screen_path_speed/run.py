"""Time and profile the OpenEMR referral on the screen path (API steps off).

One process runs one arm: the engine importable from ``PYTHONPATH`` (a
baseline worktree or this branch). ``compare.py`` alternates arms by block so
both see the same machine and the same fixture.

Every item uses the same clock as the scoreboard rows
(``benchmark/openemr_api_tier/feature_e2e.py``, ``run_one``): a new browser
context, sign-in and opening the form (``setup_s``), the engine (``engine_s``),
then the test setup's own SQL read-back (``readback_s``, including that
harness's fixed 1.0 s settle). The engine also confirms each save through its
own REST record check, inside ``engine_s``.

``--profile`` adds the exclusive-time profiler
(``benchmark/openemr_throughput/profiler.py``) and writes its buckets per item.

    PYTHONPATH=<engine worktree>/packages/flow python -m \\
        benchmark.screen_path_speed.run --bundle B --out O --arm base --blocks 1
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from benchmark.openemr_api_tier.common import (
    REFERRALS,
    load_demo,
    record_check,
    reset_and_seed,
    spec_for,
    sql_patients,
)
from benchmark.openemr_api_tier.spike_fixture import SpikeFixture

GATE_LOAD1 = float(os.environ.get("GATE_LOAD1", "35"))


def load1() -> float:
    return os.getloadavg()[0]


def wait_quiet(max_s: float = 1800.0) -> float:
    t0 = time.time()
    while load1() >= GATE_LOAD1 and time.time() - t0 < max_s:
        time.sleep(5)
    return round(time.time() - t0, 1)


def engine_head() -> str:
    import openadapt_flow

    root = Path(openadapt_flow.__file__).resolve().parents[2]
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "packages/flow/openadapt_flow"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return out[:12] + ("+dirty" if dirty else "")


def run_item(
    env: dict[str, Any], ref_idx: int, bundle: Path, out: Path, profile: bool
) -> dict[str, Any]:
    from benchmark.harness.openemr_local import OpenEMRPatientOracle
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    demo, f, browser, ocr = env["demo"], env["f"], env["browser"], env["ocr"]
    ref = REFERRALS[ref_idx - 1]
    spec = spec_for(ref)
    out.mkdir(parents=True)
    before_ids = {r["id"] for r in sql_patients(f)}
    oracle = OpenEMRPatientOracle(f.api_base_url, f.token_session("oracle"), spec)
    res: dict[str, Any] = {"ref": ref.key, "mismatch": ref.mismatch, "error": None}
    prof = None
    t0 = time.time()
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 800}, device_scale_factor=1
    )
    try:
        page = ctx.new_page()
        page.goto(f.ui_base_url)
        backend = demo._BasePlaywrightBackend(page)
        demo._preauthenticate_browser(backend, f)
        t_setup = time.time()
        ocr.clear_ocr_memo()
        workflow = Workflow.load(bundle)
        if profile:
            from benchmark.openemr_throughput.profiler import RunProfiler

            prof = RunProfiler()
            prof.install(backend)
        rep = Replayer(backend, effect_verifier=oracle.verifier, api_steps=False).run(
            workflow, params=spec.params(), bundle_dir=bundle, run_dir=out / "engine"
        )
        t_task = time.time()
        if prof is not None:
            prof.uninstall()
        raw = rep.model_dump(mode="json")
        totals: Counter[str] = Counter()
        per_step = []
        checks = []
        for r in raw["results"]:
            ident = r.get("identity") or {}
            # What each step proved, to compare arms: identity status and
            # mode, input read-back, postconditions, submit binding, and the
            # save's record check.
            checks.append(
                "|".join(
                    str(v)
                    for v in (
                        r["step_id"],
                        ident.get("status"),
                        ident.get("mode"),
                        r.get("input_verified"),
                        r.get("postconditions_ok"),
                        r.get("submit_binding_verified"),
                        r.get("effect_verified"),
                        len(r.get("effect_results") or []),
                        r.get("ok"),
                    )
                )
            )
            t = r.get("timings") or {}
            totals.update({k: float(v) for k, v in t.items()})
            per_step.append(
                {
                    "step_id": r["step_id"],
                    "intent": r["intent"],
                    "elapsed_ms": round(r.get("elapsed_ms") or 0.0, 1),
                    "timings": {k: round(float(v), 1) for k, v in t.items()},
                }
            )
        res.update(
            engine_success=raw.get("success"),
            outcome=str(raw.get("execution_outcome")),
            steps=len(raw["results"]),
            step_errors=[
                (r["step_id"], (r.get("error") or "")[:300])
                for r in raw["results"]
                if r.get("error")
            ],
            timings_ms={k: round(v, 1) for k, v in totals.most_common()},
            checks=checks,
            per_step=per_step,
        )
    except Exception as exc:  # noqa: BLE001
        if prof is not None:
            prof.uninstall()
        t_setup = locals().get("t_setup", time.time())
        t_task = time.time()
        res["error"] = f"{type(exc).__name__}: {exc}"[:500]
    time.sleep(demo.ORACLE_SETTLE_S)
    res.update(record_check(f, ref, before_ids))
    t_end = time.time()
    ctx.close()
    res.update(
        wall_s=round(t_end - t0, 3),
        setup_s=round(t_setup - t0, 3),
        engine_s=round(t_task - t_setup, 3),
        readback_s=round(t_end - t_task, 3),
    )
    if prof is not None:
        res["profile_buckets_s"] = prof.buckets()
        res["profile_calls"] = {
            k: v
            for k, v in sorted(prof.summary().items(), key=lambda kv: -kv[1]["excl_s"])
        }
        res["profile_steps_s"] = prof.step_buckets()
        res["profile_parents_s"] = prof.parents()
    res.pop("field_check", None)
    (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
    if not os.environ.get("KEEP_STEPS"):
        shutil.rmtree(out / "engine" / "steps", ignore_errors=True)
    return res


def main() -> None:
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--out", required=True, help="Per-item evidence directory")
    ap.add_argument("--results", required=True, help="JSONL file to append rows to")
    ap.add_argument("--arm", required=True)
    ap.add_argument("--block", type=int, default=1)
    ap.add_argument("--per-block", type=int, default=4)
    ap.add_argument("--start", type=int, default=0, help="First referral offset")
    ap.add_argument("--profile", action="store_true")
    ap.add_argument(
        "--speed",
        default=None,
        help="Apply a named speed profile (openadapt_flow.runtime.speed); "
        "default leaves the engine's own default",
    )
    a = ap.parse_args()
    if a.speed:
        from openadapt_flow.runtime.speed import apply_speed_profile

        apply_speed_profile(a.speed)

    import openadapt_flow
    import openadapt_flow.vision.ocr  # noqa: F401
    from playwright.sync_api import sync_playwright

    ocr = sys.modules["openadapt_flow.vision.ocr"]
    bundle, out = Path(a.bundle), Path(a.out)
    head = engine_head()
    demo, f = load_demo(), SpikeFixture()
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    try:
        # Untimed warm-up: OCR engine and a first page load.
        ocr._get_engine()
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        page.goto(f.ui_base_url)
        ocr.ocr(page.screenshot(type="png"))
        ctx.close()
        env = {"demo": demo, "f": f, "browser": browser, "ocr": ocr}
        reset_and_seed(f)
        for pos in range(a.per_block):
            idx = ((a.start + pos) % len(REFERRALS)) + 1
            waited = wait_quiet()
            l0 = load1()
            item_dir = out / f"b{a.block:02d}-{a.arm}-{pos + 1}-{REFERRALS[idx - 1].key}"
            res = run_item(env, idx, bundle, item_dir, a.profile)
            l1 = load1()
            row = {
                "arm": a.arm,
                "engine_head": head,
                "speed": a.speed or "default",
                "page_quiet_ms": os.environ.get("OPENADAPT_FLOW_PAGE_QUIET_MS"),
                "block": a.block,
                "pos": pos + 1,
                "load_start": round(l0, 2),
                "load_end": round(l1, 2),
                "gate_waited_s": waited,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                **{
                    k: res.get(k)
                    for k in (
                        "ref",
                        "mismatch",
                        "engine_success",
                        "outcome",
                        "correct",
                        "new_charts",
                        "readback",
                        "steps",
                        "error",
                        "step_errors",
                        "wall_s",
                        "setup_s",
                        "engine_s",
                        "readback_s",
                        "timings_ms",
                        "checks",
                    )
                },
            }
            with open(a.results, "a") as fh:
                fh.write(json.dumps(row) + "\n")
            print(
                json.dumps(
                    {
                        k: row[k]
                        for k in (
                            "arm",
                            "ref",
                            "correct",
                            "wall_s",
                            "engine_s",
                            "setup_s",
                            "error",
                        )
                    }
                ),
                flush=True,
            )
    finally:
        browser.close()
        pw.stop()


if __name__ == "__main__":
    main()
