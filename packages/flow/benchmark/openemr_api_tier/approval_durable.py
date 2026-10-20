"""The OpenEMR referral with the API step under each_item approval and a durable run.

Label every number from this harness exactly as: "API step under each_item
approval + durable, no run gate; harness answers approvals; approval wait
excluded". It is not the governed production profile. The referral bundle
doesn't pass the run gate yet (GUI-path admission; see docs/API_TIER.md and
the admission list in the session notes), so this measures the parts that
do run: the person approving the exact request (docs/APPROVAL_MODES.md) and
the durable checkpoints (docs/API_TIER.md "In a durable run").

- Arms: ``browser`` (API steps off) and ``api`` (API steps on), both under
  ``ApprovalGate("each_item")`` with ``durable=True``. Interleaved blocks; each
  block resets the fixture and seeds the chart r6 must not duplicate.
- Approvals: the harness stands in for the person and answers each request
  as soon as it is written (``decided_by="human"``, route ``desktop_run_it``,
  approver ``harness-approver``). Time spent waiting for the answer is
  measured and subtracted: ``engine_s`` excludes it, ``approval_wait_s`` is it.
- Read-back: the test setup's own direct SQL check (``common.record_check``),
  never the arm under test. r6 is right when no chart is created.
- Load: each item waits until the 1-minute load is under GATE_LOAD1 (12).

    PYTHONPATH=packages/flow python -m benchmark.openemr_api_tier.approval_durable \\
        compile --recording <rec> --out <bundle>
    PYTHONPATH=packages/flow python -m benchmark.openemr_api_tier.approval_durable \\
        run --bundle <bundle> --out <results dir> [--blocks 5 --per-block 4]
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import statistics
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
from benchmark.openemr_api_tier.feature_e2e import NO_MATCH_TEXT, _load1, wait_quiet
from benchmark.openemr_api_tier.spike_fixture import SpikeFixture

LABEL = (
    "API step under each_item approval + durable, no run gate; harness answers "
    "approvals; approval wait excluded"
)
APPROVER = "harness-approver"
COLS = [
    "arm",
    "block",
    "pos",
    "ref",
    "mismatch",
    "correct",
    "readback",
    "new_charts",
    "engine_success",
    "transaction_outcome",
    "actuation",
    "api_fallback",
    "approvals",
    "approval_kinds",
    "approval_wait_s",
    "engine_s",
    "wall_s",
    "checkpoints",
    "error",
    "load_start",
    "load_end",
    "gate_waited_s",
    "engine_sha",
]


def cmd_compile(a: argparse.Namespace) -> None:
    """The harness compile (feature_e2e), admitted with a right-record parameter."""

    from openadapt_flow.compiler.api_proposal import (
        admit_api_step,
        describe_api_step,
        propose_api_step,
    )
    from openadapt_flow.ir import Postcondition, PostconditionKind, Workflow

    demo = load_demo()
    rec, bundle = Path(a.recording), Path(a.out)
    demo.compile_recording(rec, bundle)
    wf = Workflow.load(bundle)
    dialog = [
        s
        for s in wf.steps
        if s.anchor is not None
        and s.anchor.structural is not None
        and s.anchor.structural.selector == "#create"
    ]
    assert len(dialog) == 1
    dialog[0].expect = list(dialog[0].expect) + [
        Postcondition(
            kind=PostconditionKind.TEXT_PRESENT, text=NO_MATCH_TEXT, timeout_s=10.0
        )
    ]
    wf, step, reasons = propose_api_step(wf, rec, replace=True)
    if step is None:
        raise SystemExit(f"no API step: {reasons}")
    # The harness admits under the fixture's name so the matrix runs
    # unattended; in use a person admits with `openadapt-flow api admit`.
    wf, _ = admit_api_step(
        wf, admitted_by="benchmark-fixture", identity={"subject_name": "lname"}
    )
    wf.save(bundle)
    print(describe_api_step(next(s for s in wf.steps if s.id == step.id)))


class HarnessPerson:
    """Answers every waiting approval request at once, and times the wait."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.waited_s = 0.0
        self.answered: list[str] = []

    def sleep(self, seconds: float) -> None:
        from openadapt_flow.approval_mode.item_approval import make_answer

        t0 = time.monotonic()
        for request in self.store.requests():
            if request.request_id in self.answered:
                continue
            if self.store.status(request) != "waiting":
                continue
            self.store.write_answer(
                make_answer(
                    request,
                    decision="approve",
                    decided_by="human",
                    route="desktop_run_it",
                    approver_id=APPROVER,
                    auth_method="cloud_aal2",
                )
            )
            self.answered.append(request.request_id)
        time.sleep(min(seconds, 0.02))
        self.waited_s += time.monotonic() - t0


def run_one(
    env: dict[str, Any], arm: str, ref_idx: int, bundle: Path, out: Path
) -> dict[str, Any]:
    from openadapt_flow.approval_mode.gate import ApprovalGate
    from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
    from openadapt_flow.benchmark.openemr_local import OpenEMRPatientOracle
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore

    demo, f, browser, ocr = env["demo"], env["f"], env["browser"], env["ocr"]
    ref = REFERRALS[ref_idx - 1]
    spec = spec_for(ref)
    out.mkdir(parents=True)
    engine_dir = out / "engine"
    before_ids = {r["id"] for r in sql_patients(f)}
    oracle = OpenEMRPatientOracle(f.api_base_url, f.token_session("oracle"), spec)
    res: dict[str, Any] = {
        "arm": arm,
        "ref": ref.key,
        "mismatch": ref.mismatch,
        "error": None,
    }
    store = ItemApprovalStore(engine_dir)
    person = HarnessPerson(store)
    gate = ApprovalGate(
        "each_item",
        approval_store=store,
        approval_wait_s=120.0,
        approval_poll_s=0.05,
        sleep=person.sleep,
        monotonic=time.monotonic,
    )
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
        rep = Replayer(
            backend,
            effect_verifier=oracle.verifier,
            api_steps=(arm == "api"),
            approval_gate=gate,
            durable=True,
        ).run(
            Workflow.load(bundle),
            params=spec.params(),
            bundle_dir=bundle,
            run_dir=engine_dir,
        )
        t_task = time.time()
        raw = rep.model_dump(mode="json")
        last = raw["results"][-1] if raw["results"] else {}
        res.update(
            engine_success=raw.get("success"),
            transaction_outcome=raw.get("transaction_outcome"),
            actuation=last.get("actuation"),
            api_fallback=next(
                (r["api_fallback"] for r in raw["results"] if r.get("api_fallback")),
                None,
            ),
            step_errors=[
                (r["step_id"], (r.get("error") or "")[:300])
                for r in raw["results"]
                if r.get("error")
            ],
        )
    except Exception as exc:  # noqa: BLE001
        t_setup = locals().get("t_setup", time.time())
        t_task = time.time()
        res["error"] = f"{type(exc).__name__}: {exc}"[:500]
    time.sleep(demo.ORACLE_SETTLE_S)
    res.update(record_check(f, ref, before_ids))
    t_end = time.time()
    ctx.close()
    requests = store.requests()
    res.update(
        approvals=len(requests),
        approval_kinds="+".join(r.form_state_kind for r in requests),
        approval_wait_s=round(person.waited_s, 3),
        engine_s=round(t_task - t_setup - person.waited_s, 3),
        wall_s=round(t_end - t0, 3),
        checkpoints=len(CheckpointStore(engine_dir).checkpoints()),
    )
    (out / "result.json").write_text(json.dumps(res, indent=1, default=str))
    shutil.rmtree(engine_dir / "steps", ignore_errors=True)
    return res


def cmd_run(a: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    import openadapt_flow.vision.ocr  # noqa: F401

    ocr = sys.modules["openadapt_flow.vision.ocr"]
    out, bundle = Path(a.out), Path(a.bundle)
    out.mkdir(parents=True, exist_ok=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()[:9]
    demo, f = load_demo(), SpikeFixture()
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ocr._get_engine()
    env = {"demo": demo, "f": f, "browser": browser, "ocr": ocr}
    csv_path = out / "runs.csv"
    rows: list[dict[str, Any]] = []
    done = (
        {
            d.name
            for d in (Path(a.skip_done) / "items").iterdir()
            if (d / "result.json").is_file()
        }
        if a.skip_done
        else set()
    )
    try:
        for b in range(a.from_block - 1, a.blocks):
            for arm in ["browser", "api"] if b % 2 == 0 else ["api", "browser"]:
                names = [
                    f"b{b + 1:02d}-{arm}-{pos + 1}-"
                    f"{REFERRALS[(b * a.per_block + pos) % len(REFERRALS)].key}"
                    for pos in range(a.per_block)
                ]
                if all(n in done for n in names):
                    continue
                reset_and_seed(f)
                for pos in range(a.per_block):
                    idx = ((b * a.per_block + pos) % len(REFERRALS)) + 1
                    if names[pos] in done:
                        continue
                    gate = wait_quiet()
                    load_start = _load1()
                    res = run_one(
                        env,
                        arm,
                        idx,
                        bundle,
                        out
                        / "items"
                        / f"b{b + 1:02d}-{arm}-{pos + 1}-{REFERRALS[idx - 1].key}",
                    )
                    row = {k: res.get(k) for k in COLS} | {
                        "block": b + 1,
                        "pos": pos + 1,
                        "load_start": load_start,
                        "load_end": _load1(),
                        "gate_waited_s": gate["gate_waited_s"],
                        "engine_sha": sha,
                    }
                    rows.append(row)
                    print(
                        json.dumps(
                            {
                                k: row[k]
                                for k in (
                                    "arm",
                                    "block",
                                    "ref",
                                    "correct",
                                    "actuation",
                                    "approvals",
                                    "engine_s",
                                    "approval_wait_s",
                                    "error",
                                )
                            }
                        ),
                        flush=True,
                    )
    finally:
        browser.close()
        pw.stop()
        with open(csv_path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            w.writeheader()
            w.writerows(rows)
        (out / "summary.json").write_text(
            json.dumps(summarize(rows, sha), indent=1) + "\n"
        )


def summarize(rows: list[dict[str, Any]], sha: str) -> dict[str, Any]:
    """Per arm: correctness by read-back, engine time, approvals."""

    out: dict[str, Any] = {"label": LABEL, "engine_sha": sha, "arms": {}}
    for arm in ("browser", "api"):
        mine = [r for r in rows if r["arm"] == arm]
        if not mine:
            continue
        times = [r["engine_s"] for r in mine if r["engine_s"] is not None]
        out["arms"][arm] = {
            "runs": len(mine),
            "correct": sum(bool(r["correct"]) for r in mine),
            "errors": sum(bool(r["error"]) for r in mine),
            "api_saves": sum(r["actuation"] == "api" for r in mine),
            "approvals_per_item": sorted({r["approvals"] for r in mine}),
            "approval_kinds": sorted({r["approval_kinds"] for r in mine}),
            "median_engine_s": round(statistics.median(times), 2) if times else None,
            "median_approval_wait_s": round(
                statistics.median(r["approval_wait_s"] for r in mine), 3
            ),
            "max_load_start": max(
                (r["load_start"] for r in mine if r["load_start"] is not None),
                default=None,
            ),
            # Stopped before saving, nothing written (a fail-closed halt or a
            # refused duplicate): not a wrong save.
            "safe_stops": sum(
                not r["correct"] and r.get("new_charts") == 0 for r in mine
            ),
            "wrong_saves": sum(
                not r["correct"] and (r.get("new_charts") or 0) > 0 for r in mine
            ),
        }
    return out


def rows_from_items(out: Path, sha: str) -> list[dict[str, Any]]:
    """Rebuild the rows from each item's result.json (an interrupted run)."""

    rows = []
    for item in sorted((out / "items").iterdir()):
        result = item / "result.json"
        if not result.is_file():
            continue  # interrupted before the item finished
        res = json.loads(result.read_text())
        block, arm, pos = item.name.split("-")[:3]
        rows.append(
            {k: res.get(k) for k in COLS}
            | {
                "block": int(block[1:]),
                "pos": int(pos),
                "load_start": None,
                "load_end": None,
                "gate_waited_s": None,
                "engine_sha": sha,
            }
        )
    return rows


def cmd_summarize(a: argparse.Namespace) -> None:
    """Combine the finished items of one or more --out directories into --to."""

    out = Path(a.to)
    out.mkdir(parents=True, exist_ok=True)
    rows = [row for d in a.out for row in rows_from_items(Path(d), a.engine_sha)]
    with open(out / "runs.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    (out / "summary.json").write_text(
        json.dumps(summarize(rows, a.engine_sha), indent=1) + "\n"
    )
    print(json.dumps(summarize(rows, a.engine_sha), indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sm = sub.add_parser(
        "summarize", help="Rebuild runs.csv and summary.json from items/"
    )
    sm.add_argument("--out", required=True, action="append")
    sm.add_argument("--to", required=True)
    sm.add_argument("--engine-sha", required=True)
    c = sub.add_parser("compile")
    c.add_argument("--recording", required=True)
    c.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("--bundle", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--blocks", type=int, default=5)
    r.add_argument("--per-block", type=int, default=4)
    r.add_argument("--from-block", type=int, default=1)
    r.add_argument(
        "--skip-done", default=None, help="An earlier --out; skip its finished items"
    )
    a = ap.parse_args()
    {"compile": cmd_compile, "run": cmd_run, "summarize": cmd_summarize}[a.cmd](a)


if __name__ == "__main__":
    main()
