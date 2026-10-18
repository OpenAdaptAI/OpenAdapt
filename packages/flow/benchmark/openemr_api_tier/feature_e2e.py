"""The API-step feature end to end on the local OpenEMR referral.

Unlike the spike (``bench.py``), this uses only the shipped feature:

    record    a scripted person registers referral fax A with network capture
              on (NetworkCapture attached before sign-in, so the form page is
              captured; Recorder writes network.jsonl)
    compile   openadapt-flow's compiler (via the OpenEMR harness, which adds
              the record check and the duplicate-check rule), then
              ``propose_api_step(replace=True)`` because the rule was added
              after compile, then the admission
    matrix    the same bundle, replayed with API steps off (``browser``) and
              on (``api``), interleaved, same record check

The admission in this harness is made by the harness under the name
``benchmark-fixture`` so the matrix can run unattended. In use, a person
admits with ``openadapt-flow api admit`` at a terminal.

    python -m benchmark.openemr_api_tier.feature_e2e record --out <rec>
    python -m benchmark.openemr_api_tier.feature_e2e compile --recording <rec> --out <bundle>
    python -m benchmark.openemr_api_tier.feature_e2e matrix --bundle <bundle> --out <dir>
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
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

FAX_A = dict(title="Ms.", fname="Elena", lname="Marsh", dob="1979-06-14", sex="Female",
             street="42 HARBOR LANE", city="EXAMPLETON", state="MA", state_label="Massachusetts",
             postal_code="02139", phone_home="555-0142", email="elena.marsh@example.invalid")
NO_MATCH_TEXT = "No matches were found."


def cmd_record(a: argparse.Namespace) -> None:
    from openadapt_flow.benchmark.openemr_local import SyntheticPatientSpec
    from openadapt_flow.network_capture import NetworkCapture
    from openadapt_flow.recorder import Recorder

    demo = load_demo()
    out = Path(a.out)
    if out.exists():
        raise SystemExit(f"exists: {out}")
    f = SpikeFixture()
    reset_and_seed(f)
    backend, close = demo._BasePlaywrightBackend.launch(f.ui_base_url, headless=True)
    try:
        capture = None if a.no_capture else NetworkCapture(backend.page.context).start()
        demo._preauthenticate_browser(backend, f)
        page = backend.page
        form = demo._form_frame(page)
        rec = Recorder(backend, out, app_url=page.url, network_capture=capture, **demo.SETTLE)
        spec = SyntheticPatientSpec(**FAX_A)

        def text_field(name: str, value: str, *, param: str) -> None:
            rec.click(*demo._center(form.locator(f"#form_{name}")))
            rec.press("Control+a")
            rec.type_text(value, param=param)

        def scroll_form() -> None:
            # The wheel scrolls whatever is under the pointer: put it over the
            # form (not recorded; a pointer move is not an event).
            page.mouse.move(640, 500)
            rec.scroll(0, 600)

        def select_by_label(name: str, label: str, *, param: str) -> None:
            rec.click(*demo._center(form.locator(f"#form_{name}")))
            rec.type_text(label, param=param)
            rec.press("Enter")

        select_by_label("title", spec.title, param="title")
        text_field("fname", spec.fname, param="fname")
        text_field("lname", spec.lname, param="lname")
        text_field("DOB", spec.dob, param="DOB")
        select_by_label("sex", spec.sex, param="sex")
        rec.click(*demo._center(form.get_by_text("Contact", exact=True)))
        scroll_form()
        text_field("street", spec.street, param="street")
        text_field("city", spec.city, param="city")
        select_by_label("state", spec.state_label, param="state_label")
        text_field("postal_code", spec.postal_code, param="postal_code")
        text_field("phone_home", spec.phone_home, param="phone_home")
        text_field("email", spec.email, param="email")
        select_by_label("country_code", "USA", param="country_code")
        scroll_form()
        rec.click(*demo._center(form.locator("#create")))
        try:
            confirm = demo._confirm_create_locator(page)
        except Exception:
            page.screenshot(path=str(out.parent / "confirm-not-ready.png"))
            raise
        rec.click(*demo._center(confirm))
        recording = rec.finish()
        events = [json.loads(x) for x in (recording / "events.jsonl").read_text().splitlines() if x.strip()]
        (recording / "openemr-save-event.json").write_text(json.dumps({
            "schema_version": 1, "event_index": events[-1]["i"],
            "dom_target": "#confirmCreate inside duplicate-check iframe",
            "effect_contract": "create exactly one synthetic patient"}, indent=2, sort_keys=True) + "\n")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not sql_patients(f, "email='elena.marsh@example.invalid'"):
            time.sleep(0.2)
        print(json.dumps({"recording": str(recording), "events": len(events),
                          "exchanges": None if capture is None else capture.count,
                          "saved": len(sql_patients(f, "email='elena.marsh@example.invalid'"))}))
    finally:
        close()


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
    # The referral's business rule (as in the published bundle): confirm a new
    # chart only when OpenEMR's duplicate check finds no existing patient.
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
    print(json.dumps({"first_compile": first}, indent=1))
    print(describe_api_step(next(s for s in wf.steps if s.id == step.id)))


GATE_LOAD1 = float(os.environ.get("GATE_LOAD1", "12"))


def _load1() -> float:
    return float(subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True).stdout.split()[1])


def wait_quiet(max_s: float = 1200.0) -> dict[str, Any]:
    """Wait until the shared machine's 1-minute load is under GATE_LOAD1."""

    t0 = time.time()
    while _load1() >= GATE_LOAD1:
        if time.time() - t0 > max_s:
            return {"gate_waited_s": round(time.time() - t0, 1), "gate_gave_up": True}
        time.sleep(5)
    return {"gate_waited_s": round(time.time() - t0, 1), "gate_gave_up": False}


COLS = ["arm", "block", "pos", "ref", "mismatch", "correct", "new_charts", "readback", "engine_success",
        "outcome", "transaction_outcome", "actuation", "api_fallback", "error", "wall_s", "setup_s", "engine_s", "readback_s",
        "load1", "gate_waited_s", "gate_gave_up", "start_local", "engine_sha"]


def run_one(env: dict[str, Any], arm: str, ref_idx: int, bundle: Path, out: Path) -> dict[str, Any]:
    from openadapt_flow.benchmark.openemr_local import OpenEMRPatientOracle
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    demo, f, browser, ocr = env["demo"], env["f"], env["browser"], env["ocr"]
    ref = REFERRALS[ref_idx - 1]
    spec = spec_for(ref)
    out.mkdir(parents=True)
    before_ids = {r["id"] for r in sql_patients(f)}
    oracle = OpenEMRPatientOracle(f.api_base_url, f.token_session("oracle"), spec)
    res: dict[str, Any] = {"arm": arm, "ref": ref.key, "mismatch": ref.mismatch, "error": None}
    t0 = time.time()
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
    try:
        page = ctx.new_page()
        page.goto(f.ui_base_url)
        backend = demo._BasePlaywrightBackend(page)
        demo._preauthenticate_browser(backend, f)
        t_setup = time.time()
        ocr.clear_ocr_memo()
        rep = Replayer(backend, effect_verifier=oracle.verifier, api_steps=(arm == "api")).run(
            Workflow.load(bundle), params=spec.params(), bundle_dir=bundle, run_dir=out / "engine")
        t_task = time.time()
        raw = rep.model_dump(mode="json")
        last = raw["results"][-1] if raw["results"] else {}
        res.update(engine_success=raw.get("success"), outcome=str(raw.get("execution_outcome")),
                   transaction_outcome=raw.get("transaction_outcome"),
                   actuation=last.get("actuation"),
                   api_fallback=next((r["api_fallback"] for r in raw["results"] if r.get("api_fallback")), None),
                   steps=len(raw["results"]),
                   step_errors=[(r["step_id"], (r.get("error") or "")[:300]) for r in raw["results"] if r.get("error")])
    except Exception as exc:  # noqa: BLE001
        t_setup = locals().get("t_setup", time.time())
        t_task = time.time()
        res["error"] = f"{type(exc).__name__}: {exc}"[:500]
    time.sleep(demo.ORACLE_SETTLE_S)
    res.update(record_check(f, ref, before_ids))
    t_end = time.time()
    ctx.close()
    res.update(wall_s=round(t_end - t0, 3), setup_s=round(t_setup - t0, 3), engine_s=round(t_task - t_setup, 3),
               readback_s=round(t_end - t_task, 3))
    (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def cmd_matrix(a: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    import openadapt_flow.vision.ocr  # noqa: F401

    ocr = sys.modules["openadapt_flow.vision.ocr"]
    out, bundle = Path(a.out), Path(a.bundle)
    out.mkdir(parents=True, exist_ok=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()[:8]
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
    csv_path = out / "runs.csv"
    new = not csv_path.exists()
    try:
        for b in range(a.blocks):
            for arm in (["browser", "api"] if b % 2 == 0 else ["api", "browser"]):
                meta = reset_and_seed(f) | {"arm": arm, "block": b + 1, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
                (out / f"block-{b + 1:02d}-{arm}.json").write_text(json.dumps(meta, indent=1))
                for pos in range(a.per_block):
                    idx = ((b * a.per_block + pos) % len(REFERRALS)) + 1
                    gate = wait_quiet()
                    start = time.strftime("%H:%M:%S")
                    res = run_one(env, arm, idx, bundle, out / f"b{b + 1:02d}-{arm}-{pos + 1}-{REFERRALS[idx - 1].key}")
                    row = {k: res.get(k) for k in COLS} | gate | {"block": b + 1, "pos": pos + 1, "load1": _load1(),
                                                                  "start_local": start, "engine_sha": sha}
                    with open(csv_path, "a", newline="") as fh:
                        w = csv.DictWriter(fh, fieldnames=COLS)
                        if new:
                            w.writeheader()
                            new = False
                        w.writerow(row)
                    print(json.dumps({k: row[k] for k in ("arm", "block", "ref", "correct", "readback", "actuation",
                                                          "api_fallback", "wall_s", "error")}), flush=True)
    finally:
        browser.close()
        pw.stop()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--out", required=True)
    r.add_argument("--no-capture", action="store_true", help="Diagnostics: record without capture")
    c = sub.add_parser("compile")
    c.add_argument("--recording", required=True)
    c.add_argument("--out", required=True)
    m = sub.add_parser("matrix")
    m.add_argument("--bundle", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--blocks", type=int, default=5)
    m.add_argument("--per-block", type=int, default=4)
    a = ap.parse_args()
    {"record": cmd_record, "compile": cmd_compile, "matrix": cmd_matrix}[a.cmd](a)


if __name__ == "__main__":
    main()
