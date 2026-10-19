"""Record, compile, propose, admit and run one app's save workflow, two arms.

    python -m benchmark.api_generalization.harness up      --app redmine
    python -m benchmark.api_generalization.harness record  --app redmine --out <rec>
    python -m benchmark.api_generalization.harness compile --app redmine --recording <rec> --out <bundle>
    python -m benchmark.api_generalization.harness matrix  --app redmine --bundle <bundle> --out <dir>
    python -m benchmark.api_generalization.harness timing  --app redmine --bundle <bundle> --out <dir> --reps 10
    python -m benchmark.api_generalization.harness down    --app redmine

``compile`` uses only the frozen compiler: ``compile_recording``, then the
save step's record check (the adapter's ``effects``, as a person adds it),
then ``propose_api_step`` (``openadapt-flow api propose``) and, when there is
a proposal, ``admit_api_step`` under the name ``benchmark-fixture`` so the
matrix can run unattended (``openadapt-flow api admit`` in use).

Arms: ``gui`` is ``Replayer(api_steps=False)``; ``api`` is
``Replayer(api_steps=True)``. Both use the same bundle, the same record
check, the same proxy and the same judge.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from benchmark.api_generalization.common import AppBase, Case, classify, judge
from benchmark.api_generalization.proxy import serve

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]
SETTLE = {"settle_timeout_s": 10.0, "settle_stable_frames": 3, "settle_interval_s": 0.3}
VIEWPORT = {"width": 1280, "height": 800}


def load_app(name: str) -> AppBase:
    return importlib.import_module(f"benchmark.api_generalization.apps.{name}").App()


def engine_sha() -> str:
    return subprocess.run(["git", "-C", str(FLOW), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def frozen_check() -> dict[str, Any]:
    """The runtime and compiler must be the frozen commit's (no local changes)."""
    frozen = os.environ.get("APIGEN_FROZEN", "")
    diff = subprocess.run(
        ["git", "-C", str(FLOW), "diff", "--stat", frozen or "HEAD", "--", "openadapt_flow", "../types"],
        capture_output=True, text=True).stdout.strip()
    return {"frozen": frozen, "head": engine_sha(), "engine_diff_vs_frozen": diff}


def state_dir(app: AppBase) -> Path:
    d = Path(os.environ.get("APIGEN_STATE", Path.home() / "oa/scratch/api-generalization")) / app.name
    d.mkdir(parents=True, exist_ok=True)
    return d


def set_fault(app: AppBase, mode: str) -> None:
    (state_dir(app) / "mode.json").write_text(json.dumps(
        {"mode": mode, "path_regex": app.save_path_regex, "body_exclude": app.save_body_exclude,
         "body_require": app.save_body_require}))


def proxy_log_len(app: AppBase) -> int:
    p = state_dir(app) / "requests.jsonl"
    return len(p.read_text().splitlines()) if p.exists() else 0


def proxy_log_since(app: AppBase, n: int) -> list[dict[str, Any]]:
    p = state_dir(app) / "requests.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()[n:]] if p.exists() else []


def start_proxy(app: AppBase) -> None:
    set_fault(app, "pass")
    serve(app.proxy_port, app.upstream_port, state_dir(app))


# -- record ------------------------------------------------------------------------


def cmd_record(a: argparse.Namespace, app: AppBase) -> None:
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.network_capture import NetworkCapture
    from openadapt_flow.recorder import Recorder

    out = Path(a.out)
    if out.exists():
        raise SystemExit(f"exists: {out}")
    start_proxy(app)
    values = app.recording_values()
    before = app.rows()
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    try:
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        page = ctx.new_page()
        page.goto(app.base + "/")
        if app.login_required:
            app.login(page)
        capture = NetworkCapture(ctx).start()
        app.open_form(page)
        rec = Recorder(PlaywrightBackend(page), out, app_url=page.url, network_capture=capture, **SETTLE)
        app.record(rec, page, values)
        recording = rec.finish()
        time.sleep(1.5)
        after = app.rows()
        case = Case(id="recording", kind="normal", params=values, expect_new=1,
                    expect_fields=app.expect_fields(values))
        state = judge(before, after, case, app.key)
        events = [json.loads(x) for x in (recording / "events.jsonl").read_text().splitlines() if x.strip()]
        print(json.dumps({"recording": str(recording), "events": len(events), "exchanges": capture.count,
                          "judge": state}, indent=1))
        if not state["state_ok"]:
            raise SystemExit("the demonstration did not save the expected record; record again")
    finally:
        browser.close()
        pw.stop()


# -- compile -----------------------------------------------------------------------


def cmd_compile(a: argparse.Namespace, app: AppBase) -> None:
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.compiler.api_proposal import (
        admit_api_step,
        describe_api_step,
        propose_api_step,
    )
    from openadapt_flow.ir import Workflow

    rec, bundle = Path(a.recording), Path(a.out)
    t0 = time.time()
    compile_recording(rec, bundle, name=f"{app.name}-{app.workflow}")
    first = json.loads((bundle / "api_proposal.json").read_text()) if (bundle / "api_proposal.json").exists() else None
    wf = Workflow.load(bundle)
    # A person marks the fields that may be left blank as optional (compile
    # makes every parameter required, and a run refuses "" for a required one).
    optional = sorted(k for k, v in app.blank_values.items() if v == "" and k in wf.param_specs)
    for k in optional:
        wf.param_specs[k].required = False
    save = wf.steps[-1]
    save.effects = app.effects()
    save.risk = "irreversible"
    save.risk_explanation = f"the {app.workflow} save"
    save.risk_review_required = False
    wf, step, reasons = propose_api_step(wf, rec, replace=True)
    result: dict[str, Any] = {"app": app.name, "workflow": app.workflow, "steps": len(wf.steps),
                              "params": sorted(wf.params), "optional_params": optional, "compile_proposal": first,
                              "proposed": step is not None, "reasons": reasons,
                              "compile_s": round(time.time() - t0, 1)} | frozen_check()
    if step is not None:
        wf, admitted = admit_api_step(wf, admitted_by="benchmark-fixture")
        result["admitted"] = admitted
        result["describe"] = describe_api_step(next(s for s in wf.steps if s.id == step.id))
        result["binding"] = step.api_binding.web_form.model_dump(mode="json") if step.api_binding else None
    wf.save(bundle)
    (bundle / "apigen-compile.json").write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps({k: v for k, v in result.items() if k != "binding"}, indent=1, default=str))


# -- run ---------------------------------------------------------------------------

COLS = ["app", "workflow", "bundle_kind", "case", "kind", "arm", "rep", "correct", "incorrect_write", "false_success", "stopped_after_right_write", "missed_write",
        "new_records", "field_mismatches", "collateral", "engine_success", "outcome", "transaction_outcome",
        "save_actuation", "api_fallback", "halt", "save_posts", "fault", "injected", "error", "wall_s", "setup_s", "engine_s",
        "load1", "start_local", "engine_sha"]


def _load1() -> float:
    return float(subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True).stdout.split()[1])


def run_one(env: dict[str, Any], app: AppBase, case: Case, arm: str, bundle: Path, out: Path, rep: int = 1) -> dict[str, Any]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    out.mkdir(parents=True, exist_ok=True)
    res: dict[str, Any] = {"app": app.name, "workflow": app.workflow, "case": case.id, "kind": case.kind,
                           "arm": arm, "rep": rep, "fault": case.fault, "error": None}
    t0 = time.time()
    ctx = env["browser"].new_context(viewport=VIEWPORT, device_scale_factor=1)
    restore = False
    try:
        page = ctx.new_page()
        page.goto(app.base + "/")
        if app.login_required:
            app.login(page)
        if case.kind == "changed_default":
            app.change_default()
            restore = True
        app.open_form(page)
        if case.kind == "expired_session":
            app.expire_session(page)
        set_fault(app, case.fault or "pass")
        before = app.rows()
        n_log = proxy_log_len(app)
        t_setup = time.time()
        rep_ = Replayer(PlaywrightBackend(page), effect_verifier=app.verifier(), api_steps=(arm == "api")).run(
            Workflow.load(bundle), params=dict(case.params), bundle_dir=bundle, run_dir=out / "engine")
        t_task = time.time()
        raw = rep_.model_dump(mode="json")
        last = raw["results"][-1] if raw["results"] else {}
        res.update(engine_success=raw.get("success"), outcome=str(raw.get("execution_outcome")),
                   transaction_outcome=raw.get("transaction_outcome"), save_actuation=last.get("actuation"),
                   api_fallback=next((r["api_fallback"] for r in raw["results"] if r.get("api_fallback")), None),
                   halt=next(((r["step_id"] + ": " + (r.get("error") or ""))[:240] for r in raw["results"] if r.get("error")), None),
                   step_errors=[(r["step_id"], (r.get("error") or "")[:300]) for r in raw["results"] if r.get("error")])
    except Exception as exc:  # noqa: BLE001
        t_setup = locals().get("t_setup", time.time())
        t_task = time.time()
        res["error"] = f"{type(exc).__name__}: {exc}"[:500]
        before = locals().get("before")
        n_log = locals().get("n_log", proxy_log_len(app))
    time.sleep(1.5)
    set_fault(app, "pass")
    after = app.rows()
    if before is None:
        before = after
        res["error"] = (res["error"] or "") + " [no before-state]"
    state = judge(before, after, case, app.key)
    log = proxy_log_since(app, n_log)
    posts = [r for r in log if r.get("save")]
    res["injected"] = next((r["fault"] for r in log if r.get("fault") not in (None, "pass")), None)
    res.update(state)
    res["save_posts"] = len(posts)
    res.update(classify(case, state, res.get("engine_success"), res.get("transaction_outcome")))
    if restore:
        app.restore_default()
    ctx.close()
    t_end = time.time()
    res.update(wall_s=round(t_end - t0, 3), setup_s=round(t_setup - t0, 3), engine_s=round(t_task - t_setup, 3))
    (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
    shutil.rmtree(out / "engine" / "steps", ignore_errors=True)
    return res


def _env() -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    return {"pw": pw, "browser": pw.chromium.launch(headless=True)}


def _write_row(csv_path: Path, row: dict[str, Any]) -> None:
    new = not csv_path.exists()
    with open(csv_path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in row.items()})


def cmd_matrix(a: argparse.Namespace, app: AppBase) -> None:
    out, bundle = Path(a.out), Path(a.bundle)
    out.mkdir(parents=True, exist_ok=True)
    (out / "engine.json").write_text(json.dumps(frozen_check(), indent=1))
    start_proxy(app)
    env = _env()
    cases = [c for c in app.cases() if not a.only or c.id in a.only.split(",")]
    arms = a.arms.split(",")
    try:
        for i, case in enumerate(cases):
            for arm in (arms if i % 2 == 0 else list(reversed(arms))):
                start = time.strftime("%H:%M:%S")
                res = run_one(env, app, app.unique(case, f"{case.id}{arm[0]}{a.tag}"), arm, bundle,
                              out / f"{case.id}-{arm}")
                row = res | {"load1": _load1(), "start_local": start, "engine_sha": engine_sha()[:8],
                             "bundle_kind": a.bundle_kind}
                _write_row(out / "runs.csv", row)
                print(json.dumps({k: row.get(k) for k in ("case", "arm", "correct", "incorrect_write", "false_success",
                                                           "new_records", "engine_success", "save_actuation",
                                                           "api_fallback", "save_posts", "engine_s", "error")}), flush=True)
    finally:
        env["browser"].close()
        env["pw"].stop()


def cmd_timing(a: argparse.Namespace, app: AppBase) -> None:
    out, bundle = Path(a.out), Path(a.bundle)
    out.mkdir(parents=True, exist_ok=True)
    (out / "engine.json").write_text(json.dumps(frozen_check(), indent=1))
    start_proxy(app)
    env = _env()
    normal = [c for c in app.cases() if c.kind == "normal"]
    try:
        for r in range(a.reps):
            for arm in (["gui", "api"] if r % 2 == 0 else ["api", "gui"]):
                case = app.unique(normal[r % len(normal)], f"t{r:02d}{arm[0]}")
                start = time.strftime("%H:%M:%S")
                res = run_one(env, app, case, arm, bundle, out / f"r{r:02d}-{arm}", rep=r + 1)
                row = res | {"load1": _load1(), "start_local": start, "engine_sha": engine_sha()[:8],
                             "bundle_kind": a.bundle_kind}
                _write_row(out / "runs.csv", row)
                print(json.dumps({k: row.get(k) for k in ("rep", "arm", "correct", "save_actuation", "engine_s",
                                                           "wall_s", "load1", "error")}), flush=True)
    finally:
        env["browser"].close()
        env["pw"].stop()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["up", "down", "record", "compile", "matrix", "timing", "rows"])
    ap.add_argument("--app", required=True)
    ap.add_argument("--out")
    ap.add_argument("--recording")
    ap.add_argument("--bundle")
    ap.add_argument("--only", default="")
    ap.add_argument("--arms", default="gui,api")
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--bundle-kind", default="shipped", choices=["shipped", "counterfactual"])
    ap.add_argument("--tag", default="", help="suffix that keeps a re-run's values unique")
    a = ap.parse_args()
    app = load_app(a.app)
    if a.cmd == "up":
        app.up()
    elif a.cmd == "down":
        app.down()
    elif a.cmd == "rows":
        print(json.dumps(app.rows()[-5:], indent=1, default=str))
    else:
        {"record": cmd_record, "compile": cmd_compile, "matrix": cmd_matrix, "timing": cmd_timing}[a.cmd](a, app)


if __name__ == "__main__":
    sys.exit(main())
