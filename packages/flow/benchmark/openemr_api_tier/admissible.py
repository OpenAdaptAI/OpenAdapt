"""The OpenEMR referral's API step, admissible: page script run, background write declared.

Until now the compiler refused an API step for this form for two reasons: the
form validates with script (``submitme(...)`` on submit, a custom validate.js
``pastDate`` rule on the date of birth, ``onchange`` on every field) and the
tab shell posts ``execute_background_services.php`` while the form is open.
With the API step running the page's own script before each save
(runtime/actuators/form_eval.py) and declaring the background post as the
page's own request (``web_form.writes``, role ``ambient``), the same
recording now gets an API step. This harness measures it on the local
OpenEMR test copy, against the same program on the GUI.

Label every number exactly as: "plain replayer, no approval gate, no durable
run, no run gate; API step admitted by the harness (benchmark-fixture)".

- Arms: ``browser`` (``api_steps=False``) and ``api`` (``api_steps=True``),
  interleaved by block; each block resets the fixture and seeds the chart r6
  must not duplicate.
- Items: the eight referrals of ``common.REFERRALS`` (r6 must create no
  chart), plus ``x1-future-dob`` (the form's ``pastDate`` rule must refuse
  it: no chart) and ``x2-lowercase`` (a first name typed in lower case;
  both arms must save the same thing).
- Read-back: the test setup's own SQL check (``common.record_check``), never
  the arm under test, plus the whole new ``patient_data`` row, so the two
  arms' saves can be compared column by column.
- Time: ``wall_s`` from a fresh browser context through sign-in, the entry and
  the read-back; items per hour is 3600 over the mean ``wall_s`` of one
  sequential worker. Each item has a hard timeout (``--item-timeout``).

    python -m benchmark.openemr_api_tier.admissible compile --recording <rec> --out <bundle>
    python -m benchmark.openemr_api_tier.admissible run --bundle <bundle> --out <dir> [--blocks 4]
    python -m benchmark.openemr_api_tier.admissible summarize --out <dir>
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from benchmark.openemr_api_tier.common import (
    _MA,
    REFERRALS,
    Referral,
    load_demo,
    record_check,
    reset_and_seed,
    spec_for,
    sql_patients,
)
from benchmark.openemr_api_tier.feature_e2e import NO_MATCH_TEXT, _load1, wait_quiet
from benchmark.openemr_api_tier.spike_fixture import SpikeFixture

LABEL = (
    "plain replayer, no approval gate, no durable run, no run gate; "
    "API step admitted by the harness (benchmark-fixture)"
)
#: A date of birth in the future: the form's own pastDate rule refuses it.
FUTURE_DOB = Referral(
    "x1-future-dob",
    dict(title="Ms.", fname="Wren", lname="Halvorsen", dob="2031-02-03", sex="Female",
         street="5 ORCHARD LANE", phone_home="555-0171", email="wren.halvorsen@example.invalid", **_MA),
    mismatch=True,
)
#: A first name typed in lower case: whatever the page does with it, both
#: arms must save the same chart.
LOWERCASE = Referral(
    "x2-lowercase",
    dict(title="Mr.", fname="otto", lname="Brandvold", dob="1968-08-21", sex="Male",
         street="21 KILN STREET", phone_home="555-0176", email="otto.brandvold@example.invalid", **_MA),
)
ITEMS = [*REFERRALS, FUTURE_DOB, LOWERCASE]
#: Columns that differ between any two saves of the same referral.
VOLATILE = {"id", "pid", "pubpid", "uuid", "date", "regdate", "created_by", "updated_by",
            "last_updated", "created_at", "updated_at"}

COLS = ["arm", "block", "pos", "ref", "expect_chart", "correct", "new_charts", "readback",
        "engine_success", "outcome", "transaction_outcome", "save_actuation", "api_fallback", "error", "wall_s", "setup_s", "engine_s", "readback_s",
        "load1", "gate_waited_s", "start_local", "engine_sha"]


def cmd_compile(a: argparse.Namespace) -> None:
    from openadapt_flow.compiler.api_proposal import (
        admit_api_step,
        describe_api_step,
        propose_api_step,
    )
    from openadapt_flow.ir import Postcondition, PostconditionKind, Workflow

    demo = load_demo()
    rec, bundle = Path(a.recording), Path(a.out)
    demo.compile_recording(rec, bundle)
    first = json.loads((bundle / "api_proposal.json").read_text())
    wf = Workflow.load(bundle)
    # The referral's business rule (as in feature_e2e): confirm a new chart
    # only when OpenEMR's duplicate check finds no existing patient.
    dialog = [s for s in wf.steps if s.anchor is not None and s.anchor.structural is not None
              and s.anchor.structural.selector == "#create"]
    assert len(dialog) == 1
    dialog[0].expect = list(dialog[0].expect) + [
        Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=NO_MATCH_TEXT, timeout_s=10.0)]
    wf, step, reasons = propose_api_step(wf, rec, replace=True)
    if step is None:
        raise SystemExit(f"no API step: {reasons}")
    wf, _ = admit_api_step(wf, admitted_by="benchmark-fixture")
    wf.save(bundle)
    save = next(s for s in wf.steps if s.id == step.id)
    out = {"first_compile": first, "proposed": step.id, "label": LABEL,
           "client_scripts": save.api_binding.web_form.client_scripts,
           "writes": [w.model_dump(mode="json") for w in save.api_binding.web_form.writes],
           "describe": describe_api_step(save)}
    (bundle / "admissible-compile.json").write_text(json.dumps(out, indent=1))
    print(out["describe"])


def _columns(f: SpikeFixture) -> list[str]:
    out = f._db("SELECT column_name FROM information_schema.columns WHERE table_schema='openemr' "
                "AND table_name='patient_data' ORDER BY ordinal_position").decode()
    return [c for c in out.split() if c]


def _rows(f: SpikeFixture, cols: list[str], ids: list[str]) -> list[dict[str, str]]:
    if not ids:
        return []
    sel = ",".join(f"`{c}`" for c in cols)
    out = f._db(f"SELECT {sel} FROM openemr.patient_data WHERE id IN ({','.join(ids)}) ORDER BY id").decode()
    return [{k: ("" if v == "NULL" else v) for k, v in zip(cols, line.split("\t"))}
            for line in out.splitlines()]


class _Timeout(Exception):
    pass


def _alarm(_signum: int, _frame: Any) -> None:
    raise _Timeout("item timeout")


def run_one(env: dict[str, Any], arm: str, ref: Referral, bundle: Path, out: Path, timeout_s: int) -> dict[str, Any]:
    from openadapt_flow.benchmark.openemr_local import OpenEMRPatientOracle
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    demo, f, browser, ocr = env["demo"], env["f"], env["browser"], env["ocr"]
    spec = spec_for(ref)
    out.mkdir(parents=True)
    before_ids = {r["id"] for r in sql_patients(f)}
    oracle = OpenEMRPatientOracle(f.api_base_url, f.token_session("oracle"), spec)
    res: dict[str, Any] = {"arm": arm, "ref": ref.key, "expect_chart": not ref.mismatch, "error": None}
    t0 = time.time()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(timeout_s)
    t_setup = t_task = None
    try:
        page = ctx.new_page()
        page.goto(f.ui_base_url)
        backend = demo._BasePlaywrightBackend(page)
        demo._preauthenticate_browser(backend, f)
        t_setup = time.time()
        ocr.clear_ocr_memo()
        replayer = Replayer(backend, effect_verifier=oracle.verifier, api_steps=(arm == "api"))
        rep = replayer.run(Workflow.load(bundle), params=spec.params(), bundle_dir=bundle,
                           run_dir=out / "engine")
        t_task = time.time()
        raw = rep.model_dump(mode="json")
        last = raw["results"][-1] if raw["results"] else {}
        res.update(engine_success=raw.get("success"), outcome=str(raw.get("execution_outcome")),
                   transaction_outcome=raw.get("transaction_outcome"), save_actuation=last.get("actuation"),
                   api_fallback=next((r["api_fallback"] for r in raw["results"] if r.get("api_fallback")), None),
                   step_errors=[(r["step_id"], (r.get("error") or "")[:300]) for r in raw["results"] if r.get("error")])
    except _Timeout:
        res["error"] = f"timeout after {timeout_s}s"
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"[:500]
    finally:
        signal.alarm(0)
    t_setup = t_setup or time.time()
    t_task = t_task or time.time()
    time.sleep(demo.ORACLE_SETTLE_S)
    res.update(record_check(f, ref, before_ids))
    new_ids = [r["id"] for r in sql_patients(f) if r["id"] not in before_ids]
    res["new_rows"] = _rows(f, env["cols"], new_ids)
    t_end = time.time()
    try:
        ctx.close()
    except Exception:  # noqa: BLE001
        pass
    res.update(wall_s=round(t_end - t0, 3), setup_s=round(t_setup - t0, 3), engine_s=round(t_task - t_setup, 3),
               readback_s=round(t_end - t_task, 3))
    (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
    shutil.rmtree(out / "engine" / "steps", ignore_errors=True)
    return res


def cmd_run(a: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    import openadapt_flow.vision.ocr  # noqa: F401

    ocr = sys.modules["openadapt_flow.vision.ocr"]
    out, bundle = Path(a.out), Path(a.bundle)
    out.mkdir(parents=True, exist_ok=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()[:9]
    demo, f = load_demo(), SpikeFixture()
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ocr._get_engine()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = ctx.new_page()
    page.goto(f.ui_base_url)
    ocr.ocr(page.screenshot(type="png"))
    ctx.close()
    env = {"demo": demo, "f": f, "browser": browser, "ocr": ocr, "cols": _columns(f)}
    (out / "run.json").write_text(json.dumps({"label": LABEL, "engine_sha": sha, "bundle": str(bundle),
                                              "blocks": a.blocks, "items": [r.key for r in ITEMS]}, indent=1))
    csv_path = out / "runs.csv"
    new = not csv_path.exists()
    try:
        for b in range(a.blocks):
            for arm in (["browser", "api"] if b % 2 == 0 else ["api", "browser"]):
                meta = reset_and_seed(f) | {"arm": arm, "block": b + 1, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
                (out / f"block-{b + 1:02d}-{arm}.json").write_text(json.dumps(meta, indent=1))
                for pos, ref in enumerate(ITEMS):
                    gate = wait_quiet()
                    start = time.strftime("%H:%M:%S")
                    res = run_one(env, arm, ref, bundle, out / f"b{b + 1:02d}-{arm}-{pos + 1:02d}-{ref.key}",
                                  a.item_timeout)
                    row = {k: res.get(k) for k in COLS} | {"block": b + 1, "pos": pos + 1, "load1": _load1(),
                                                          "gate_waited_s": gate["gate_waited_s"],
                                                          "start_local": start, "engine_sha": sha}
                    with open(csv_path, "a", newline="") as fh:
                        w = csv.DictWriter(fh, fieldnames=COLS)
                        if new:
                            w.writeheader()
                            new = False
                        w.writerow(row)
                    print(json.dumps({k: row[k] for k in ("arm", "block", "ref", "correct", "readback",
                                                          "save_actuation", "api_fallback", "wall_s",
                                                          "error")}), flush=True)
    finally:
        browser.close()
        pw.stop()


def _same_save(a_rows: list[dict[str, str]], b_rows: list[dict[str, str]]) -> list[str]:
    if len(a_rows) != len(b_rows):
        return [f"charts: {len(a_rows)} vs {len(b_rows)}"]
    diffs = []
    for x, y in zip(a_rows, b_rows):
        for k in sorted(set(x) | set(y)):
            if k not in VOLATILE and x.get(k) != y.get(k):
                diffs.append(f"{k}: {x.get(k)!r} vs {y.get(k)!r}")
    return diffs


def cmd_summarize(a: argparse.Namespace) -> None:
    out = Path(a.out)
    results = [json.loads(p.read_text()) for p in sorted(out.glob("b*/result.json"))]
    summary: dict[str, Any] = {"label": LABEL, "arms": {}}
    for arm in ("browser", "api"):
        rs = [r for r in results if r["arm"] == arm]
        if not rs:
            continue
        valid = [r for r in rs if r["expect_chart"]]
        walls = [r["wall_s"] for r in rs]
        summary["arms"][arm] = {
            "items": len(rs),
            "correct": sum(bool(r["correct"]) for r in rs),
            "wrong_writes": sum(r["new_charts"] > 0 and not r["correct"] for r in rs),
            "false_stops": sum(not r["correct"] and r["new_charts"] == 0 for r in valid),
            "api_saves": sum(r.get("save_actuation") == "api" for r in rs),
            "fallbacks": sorted({r["api_fallback"] for r in rs if r.get("api_fallback")
                                 and r["api_fallback"] != "API steps are off for this run"}),
            "errors": [r["error"] for r in rs if r.get("error")],
            "median_wall_s": round(statistics.median(walls), 2),
            "mean_wall_s": round(statistics.mean(walls), 2),
            "items_per_hour": round(3600 / statistics.mean(walls), 1),
            "median_engine_s": round(statistics.median(r["engine_s"] for r in rs), 2),
        }
    # Equivalence: the API arm's chart against the browser arm's, per referral.
    by = {}
    for r in results:
        by.setdefault((r["ref"], r["arm"]), []).append(r)
    eq = {}
    for ref in sorted({r["ref"] for r in results}):
        pairs = list(zip(by.get((ref, "browser"), []), by.get((ref, "api"), [])))
        eq[ref] = [_same_save(x.get("new_rows") or [], y.get("new_rows") or []) for x, y in pairs]
    summary["equivalence"] = {ref: {"pairs": len(v), "identical": sum(not d for d in v),
                                    "diffs": sorted({x for d in v for x in d})[:10]} for ref, v in eq.items()}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compile")
    c.add_argument("--recording", required=True)
    c.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("--bundle", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--blocks", type=int, default=4)
    r.add_argument("--item-timeout", type=int, default=240)
    s = sub.add_parser("summarize")
    s.add_argument("--out", required=True)
    a = ap.parse_args()
    {"compile": cmd_compile, "run": cmd_run, "summarize": cmd_summarize}[a.cmd](a)


if __name__ == "__main__":
    main()
