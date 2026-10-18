"""Steps 3-4: run the referral through the browser program or the API step.

    python -m benchmark.openemr_api_tier.bench one --arm api --ref 1 --out <dir>
    python -m benchmark.openemr_api_tier.bench matrix --blocks 5 --per-block 4 --out <dir>

Arms (both through the same ``Replayer`` and the same ``EffectVerifier``):

* ``browser``: the DOM-identity referral bundle (the published ``replay-dom``
  arm). Clock: new browser context -> sign-in -> open the New Patient form ->
  41 replayed steps -> record check.
* ``api``: the compiled one-step bundle with :class:`SessionFormActuator`.
  No browser is started; the backend refuses every GUI call. Clock: new HTTP
  session -> sign-in -> read the form (CSRF token, defaults, drift check) ->
  duplicate check -> save POST -> record check.

Warm mode for both: Python imports, Chromium launch and the OCR engine are
set up once before the matrix (untimed), as in the October timing study.
The record check is the test setup's own SQL read-back, the same rule as that
study. Fixture reset and seeding are outside the clock.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from benchmark.openemr_api_tier.capture_har import BUNDLE as GUI_BUNDLE
from benchmark.openemr_api_tier.common import (
    REFERRALS,
    load_demo,
    record_check,
    reset_and_seed,
    spec_for,
    sql_patients,
)
from benchmark.openemr_api_tier.formtier import SessionFormActuator
from benchmark.openemr_api_tier.spike_fixture import SpikeFixture

API_BUNDLE = Path(os.environ.get("API_BUNDLE", str(Path(__file__).resolve().parent / "api_bundle")))
SETTLE_S = 1.0  # same oracle settle as the October study (demo.ORACLE_SETTLE_S)


class NoGuiBackend:
    """A backend that fails loudly if the API run looks at or acts on the GUI.

    Optional backend capabilities are simply absent (AttributeError); every
    observation or input method raises.
    """

    def _refuse(self, *_a: Any, **_k: Any) -> Any:
        raise RuntimeError("API-tier run touched the GUI backend")

    screenshot = click = double_click = right_click = drag = type_text = press = hotkey = scroll = _refuse
    move = mouse_move = key = select_option = _refuse


class Env:
    def __init__(self, browser: bool) -> None:
        from openadapt_flow.benchmark.openemr_local import OpenEMRPatientOracle
        from openadapt_flow.ir import Workflow
        from openadapt_flow.runtime import Replayer

        self.demo = load_demo()
        self.f = SpikeFixture()
        self.Oracle, self.Workflow, self.Replayer = OpenEMRPatientOracle, Workflow, Replayer
        self.tier = json.loads((API_BUNDLE / "session_form.json").read_text())
        self.pw = self.browser = None
        if browser:
            import sys

            from playwright.sync_api import sync_playwright

            import openadapt_flow.vision.ocr  # noqa: F401

            ocr = sys.modules["openadapt_flow.vision.ocr"]  # the package re-exports a function named ocr
            self.ocr = ocr
            self.pw = sync_playwright().start()
            self.browser = self.pw.chromium.launch(headless=True)
            ocr._get_engine()  # warm the OCR engine once, untimed
            ctx = self.browser.new_context(viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.goto(self.f.ui_base_url)
            ocr.ocr(page.screenshot(type="png"))
            ctx.close()
            ocr.clear_ocr_memo()

    def close(self) -> None:
        if self.browser:
            self.browser.close()
            self.pw.stop()


def run_one(env: Env, arm: str, ref_idx: int, out: Path) -> dict[str, Any]:
    ref = REFERRALS[ref_idx - 1]
    spec = spec_for(ref)
    out.mkdir(parents=True, exist_ok=False)
    before_ids = {r["id"] for r in sql_patients(env.f)}
    oracle = env.Oracle(env.f.api_base_url, env.f.token_session("oracle"), spec)
    res: dict[str, Any] = {"arm": arm, "ref": ref.key, "mismatch": ref.mismatch, "error": None}
    m: dict[str, float] = {}
    ctx = None
    try:
        if arm == "browser":
            m["t0"] = time.time()
            ctx = env.browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
            page = ctx.new_page()
            page.goto(env.f.ui_base_url)
            backend = env.demo._BasePlaywrightBackend(page)
            env.demo._preauthenticate_browser(backend, env.f)
            m["setup_end"] = time.time()
            env.ocr.clear_ocr_memo()
            rep = env.Replayer(backend, effect_verifier=oracle.verifier).run(
                env.Workflow.load(GUI_BUNDLE), params=spec.params(), bundle_dir=GUI_BUNDLE, run_dir=out / "engine")
        else:
            vals = env.f._runtime_values()
            m["t0"] = time.time()
            act = SessionFormActuator(env.f.ui_base_url, env.tier, username=vals["OPENEMR_ACTOR_USER"],
                                      password=vals["OPENEMR_ACTOR_PASSWORD"])
            rep = env.Replayer(NoGuiBackend(), effect_verifier=oracle.verifier, api_actuator=act).run(
                env.Workflow.load(API_BUNDLE), params=spec.params(), bundle_dir=API_BUNDLE, run_dir=out / "engine")
            m["setup_end"] = act.marks.get("form_read", m["t0"])
            res["api_marks"] = {k: round(v - m["t0"], 3) for k, v in act.marks.items()}
            res["api_log"] = act.log
        m["task_end"] = time.time()
        raw = rep.model_dump(mode="json")
        res.update(engine_success=raw.get("success"), outcome=str(raw.get("execution_outcome")),
                   steps=len(raw.get("results") or []),
                   step_errors=[(r["step_id"], (r.get("error") or "")[:300]) for r in raw["results"] if r.get("error")],
                   actuation=[r.get("actuation") for r in raw["results"]][-1:],
                   effect_verified=[r.get("effect_verified") for r in raw["results"]][-1:])
    except Exception as exc:  # noqa: BLE001
        m.setdefault("task_end", time.time())
        res["error"] = f"{type(exc).__name__}: {exc}"[:500]
    time.sleep(SETTLE_S)
    res.update(record_check(env.f, ref, before_ids))
    m["verified"] = time.time()
    if ctx is not None:
        ctx.close()
    res["wall_s"] = round(m["verified"] - m["t0"], 3)
    res["setup_s"] = round(m.get("setup_end", m["t0"]) - m["t0"], 3)
    res["task_s"] = round(m["task_end"] - m["t0"], 3)  # from the clock start, so it includes setup_s
    res["readback_s"] = round(m["verified"] - m["task_end"], 3)
    (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
    return res


COLS = ["arm", "block", "pos", "ref", "mismatch", "correct", "new_charts", "readback", "engine_success", "outcome",
        "error", "wall_s", "setup_s", "task_s", "readback_s", "load1", "start_local", "engine_sha"]


def load1() -> float:
    return float(subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True).stdout.split()[1])


def cmd_matrix(a: argparse.Namespace) -> None:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()[:8]
    env = Env(browser=True)
    csv_path = out / "runs.csv"
    new = not csv_path.exists()
    try:
        for b in range(a.blocks):
            # Interleave arms in time and alternate which goes first.
            arms = ["browser", "api"] if b % 2 == 0 else ["api", "browser"]
            for arm in arms:
                meta = reset_and_seed(env.f)
                meta.update(arm=arm, block=b + 1, started=time.strftime("%Y-%m-%d %H:%M:%S"))
                (out / f"block-{b + 1:02d}-{arm}.json").write_text(json.dumps(meta, indent=1))
                for pos in range(a.per_block):
                    idx = ((b * a.per_block + pos) % len(REFERRALS)) + 1
                    start = time.strftime("%H:%M:%S")
                    res = run_one(env, arm, idx, out / f"b{b + 1:02d}-{arm}-{pos + 1}-{REFERRALS[idx - 1].key}")
                    row = {k: res.get(k) for k in COLS}
                    row.update(block=b + 1, pos=pos + 1, load1=load1(), start_local=start, engine_sha=sha)
                    with open(csv_path, "a", newline="") as fh:
                        w = csv.DictWriter(fh, fieldnames=COLS)
                        if new:
                            w.writeheader()
                            new = False
                        w.writerow(row)
                    print(json.dumps({k: row[k] for k in ("arm", "block", "ref", "correct", "readback", "wall_s", "task_s", "error")}), flush=True)
    finally:
        env.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("one")
    o.add_argument("--arm", required=True, choices=["browser", "api"])
    o.add_argument("--ref", type=int, default=1)
    o.add_argument("--out", required=True)
    o.add_argument("--reset", action="store_true")
    mtx = sub.add_parser("matrix")
    mtx.add_argument("--blocks", type=int, default=5)
    mtx.add_argument("--per-block", type=int, default=4)
    mtx.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.cmd == "matrix":
        return cmd_matrix(a)
    env = Env(browser=a.arm == "browser")
    try:
        if a.reset:
            reset_and_seed(env.f)
        res = run_one(env, a.arm, a.ref, Path(a.out))
        print(json.dumps({k: v for k, v in res.items() if k != "field_check"}, indent=1, default=str))
    finally:
        env.close()


if __name__ == "__main__":
    main()
