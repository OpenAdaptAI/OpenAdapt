"""Lending (MockLoan) transactional fault-model study runner.

The non-healthcare mirror of ``benchmark/fault_model/run.py``. Records + compiles
ONE canonical MockLoan disbursement-authorize bundle, then replays that SAME
bundle through the REAL ``Replayer`` against a persistence-backed MockLoan under
each transactional fault class (see ``benchmark/lending_fault_model/faults.py``).
Every replay is judged by the backend ledger (ground truth via ``GET /api/db``)
and its halt state, NOT by the replay's own vision-based self-report - that gap
is exactly what this study measures.

This is the SCREEN-ONLY baseline arm: the compiled replay verifies each step
with vision postconditions only (``text_present`` / ``region_stable`` /
``url_changed``). The effect-verified arm that closes the gap is in
``benchmark/lending_fault_model/swer.py`` (the same independent ``/api/db``
oracle, scored through the shared EffectBench contract).

No Anthropic / model calls are made: the grounder rung is never installed and
``ANTHROPIC_API_KEY`` is unset, so resolution is template + OCR + geometry only.

Outputs (written under ``benchmark/lending_fault_model/``):

- ``results.json``  -- machine-readable per-run + aggregate matrix.
- ``LENDING_FAULT_MODEL.md`` -- the outcome taxonomy per fault class, the honest
                        verdict, and the recommended first-class handling.

Usage::

    python -m benchmark.lending_fault_model.run                # full study
    python -m benchmark.lending_fault_model.run --repeats 10   # more counts
    python -m benchmark.lending_fault_model.run --quick        # 2 repeats, smoke
    python -m benchmark.lending_fault_model.run --only optimistic,duplicate
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import requests

from benchmark.lending_fault_model import faults as F
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.compiler import compile_recording
from openadapt_flow.demo_driver import record_disbursement_demo
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.mockloan.fault_server import serve as fault_serve
from openadapt_flow.runtime import Replayer

HERE = Path(__file__).resolve().parent
PARAMS = {"memo": F.MEMO_TEXT}
VIEWPORT = {"width": 1280, "height": 800}


@dataclass
class RunObs:
    """One replay of the bundle under one fault mode."""

    mode: str
    repeat: int
    report_success: bool
    authorize_postconditions_ok: Optional[bool]
    db_records: list
    rejected_writes: int
    outcome: str
    reason: str
    silently_mishandled: bool
    elapsed_s: float


def _authorize_step_pcs_ok(report: RunReport) -> Optional[bool]:
    """postconditions_ok of the final (Authorize Disbursement) step, if run."""
    for r in reversed(report.results):
        if "authorize" in r.step_id.lower() or "Authorize" in r.intent:
            return r.postconditions_ok
    return report.results[-1].postconditions_ok if report.results else None


def replay_once(
    browser,
    bundle_dir: Path,
    base_url: str,
    run_dir: Path,
    fault: F.Fault,
    repeat: int,
) -> RunObs:
    run_dir.mkdir(parents=True, exist_ok=True)
    # Reset the ground-truth ledger (seed a concurrent row only for stale mode).
    requests.post(
        base_url + "api/reset",
        json={"seed_concurrent": fault.seed_concurrent},
        timeout=10,
    )
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
    report: Optional[RunReport] = None
    t0 = time.monotonic()
    try:
        page.goto(f"{base_url}?fault={fault.mode}")
        backend = PlaywrightBackend(page)
        report = Replayer(backend).run(
            Workflow.load(bundle_dir),
            params=dict(PARAMS),
            bundle_dir=Path(bundle_dir),
            run_dir=run_dir,
        )
    finally:
        page.close()
    elapsed = time.monotonic() - t0

    snap = requests.get(base_url + "api/db", timeout=10).json()
    records = snap.get("records", [])
    report_success = bool(report.success) if report else False
    outcome, reason = F.classify(
        report_success=report_success,
        records=records,
        seeded_concurrent=fault.seed_concurrent,
    )
    return RunObs(
        mode=fault.mode,
        repeat=repeat,
        report_success=report_success,
        authorize_postconditions_ok=(
            _authorize_step_pcs_ok(report) if report else None
        ),
        db_records=records,
        rejected_writes=int(snap.get("rejected_writes", 0)),
        outcome=outcome,
        reason=reason,
        silently_mishandled=F.is_silently_mishandled(outcome, report_success),
        elapsed_s=round(elapsed, 1),
    )


@dataclass
class ClassResult:
    """Aggregated outcomes for one fault class across repeats."""

    mode: str
    title: str
    fault_class: str
    injected: str
    expected_outcome: str
    headline: str
    repeats: int
    outcome_counts: dict = field(default_factory=dict)
    dominant_outcome: str = ""
    silently_mishandled_count: int = 0
    example_reason: str = ""
    example_db: list = field(default_factory=list)
    example_report_success: bool = False


def aggregate(fault: F.Fault, obs: list[RunObs]) -> ClassResult:
    counts: dict[str, int] = {}
    for o in obs:
        counts[o.outcome] = counts.get(o.outcome, 0) + 1
    dominant = max(counts, key=counts.get) if counts else ""
    sm = sum(1 for o in obs if o.silently_mishandled)
    ex = obs[0]
    return ClassResult(
        mode=fault.mode,
        title=fault.title,
        fault_class=fault.fault_class,
        injected=fault.injected,
        expected_outcome=fault.expected_outcome,
        headline=fault.headline,
        repeats=len(obs),
        outcome_counts=counts,
        dominant_outcome=dominant,
        silently_mishandled_count=sm,
        example_reason=ex.reason,
        example_db=ex.db_records,
        example_report_success=ex.report_success,
    )


# -- report rendering ---------------------------------------------------------

_RECOMMENDATIONS = """\
## What the current system covers, and what it does not

The compiled replay verifies each step with **vision postconditions** -
`text_present`, `region_stable`, `url_changed`. Every one of these reads the
*screen*. For a consequential write, the screen is the wrong witness: it shows
what the UI painted, not what the loan-servicing core booked. The study makes
the gap concrete:

- **Detected (safe-halt).** Only the fault that also breaks the *screen*
  (session expiry bounces to the login page, so the authorized-banner
  postcondition is never met) is caught. The replay halts with no side effect.
- **Silently mishandled.** Every fault that leaves the success screen intact is
  reported as a clean success while ground truth disagrees: partial save (memo
  dropped), optimistic-UI rejection (nothing booked), duplicate submission /
  double-click (two disbursements - the borrower is paid twice), and stale
  overwrite (a concurrent fraud hold lost). None of these is a drift problem -
  the recorded pixels match perfectly - so no amount of self-healing or template
  tolerance would catch them.
- **Conservatively wrong.** Timeout-after-write halts (safe now) but leaves the
  effect unverified; the natural human/agent response - retry - turns it into a
  duplicate disbursement, which the system also cannot detect.

## Recommended first-class handling

For consequential money-movement writes, idempotency and effect-verification are
safety requirements, not niceties:

1. **At-most-once via idempotency keys.** Attach a per-intent idempotency key to
   any disbursement step and require the core to de-duplicate on it. The
   `idempotent` control shows the duplicate/double-click hazard collapsing to a
   single disbursement once a key is present.
2. **Effect-verification postconditions.** A write step must be able to assert
   its effect against a *structured* read of the record system (an API/DB read),
   not only against a banner. `optimistic` and `partial` become detectable the
   moment the postcondition reads back the persisted memo instead of trusting
   the toast. This is exactly what `benchmark/lending_fault_model/swer.py`
   demonstrates.
3. **Explicit write outcomes over optimistic banners.** Treat a write as pending
   until the core confirms it; do not let an optimistic banner satisfy a
   postcondition. This closes the phantom-success class.
4. **Concurrency / version checks.** Carry a version or etag on the target loan
   and refuse a last-write-wins overwrite; surface the conflict as a halt rather
   than a silent lost update.

These are properties of the *write step contract*, not of the vision layer.
"""


def render_markdown(results: list[ClassResult], meta: dict) -> str:
    lines: list[str] = []
    lines.append("# Lending (MockLoan) transactional fault-model study")
    lines.append("")
    lines.append(
        "The non-healthcare replication of `benchmark/fault_model` on a "
        "distinct system of record: a loan-origination console whose "
        "consequential write authorizes a **disbursement of funds to a "
        "borrower** (an irreversible money-movement write). Prior rigor studies "
        "stressed *UI drift*; this one stresses the *persistence boundary*, "
        "which UI drift never touches - on a second, non-clinical domain."
    )
    lines.append("")
    lines.append(f"Generated: {meta['generated_at']}  ")
    lines.append(f"Platform: {meta['platform']}  ")
    lines.append(
        f"Bundle: {meta['total_steps']} steps (login -> open loan -> new "
        f"disbursement -> Personal -> memo -> **Authorize Disbursement**).  "
    )
    lines.append(f"Repeats per fault class: {meta['repeats']}.  ")
    lines.append(
        "Model calls: **0** (compiled replay; `ANTHROPIC_API_KEY` unset, "
        "grounder rung never installed).  "
    )
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append(
        "The bundled MockLoan app is a client-side SPA with no backend, so an "
        '"authorize" only mutates in-page state - the UI *is* the source of '
        "truth. This study adds a real persistence boundary "
        "(`openadapt_flow/mockloan/fault_server.py`): a flag-gated `?fault=` "
        "hook in the app (mirroring the `?drift=` hooks) routes the Authorize "
        "write through a backend API with an isolated SQLite ledger. **With no "
        "`?fault` query the app never calls the API and the normal benchmark "
        "is byte-for-byte unaffected** (pinned by a test). Each fault class is "
        "injected at that boundary; the SAME recorded bundle is replayed "
        "through the REAL `Replayer` against it, and the outcome is judged by "
        "`GET /api/db` (ground truth) plus whether the replay halted - never by "
        "the replay's self-report. `/api/db` is a read path the SPA itself "
        "never calls, so the oracle cannot be gamed by the screen."
    )
    lines.append("")
    lines.append("### Outcome taxonomy")
    lines.append("")
    lines.append(
        "| outcome | meaning |\n|---|---|\n"
        "| SUCCESS | ran to completion; exactly one correct, complete "
        "disbursement booked |\n"
        "| SAFE-HALT | stopped without completing; **no** side effect |\n"
        "| WRONG-ACTION | a wrong write landed (duplicate / lost update / "
        "persisted-after-halt) |\n"
        "| FALSE-ABORT | the disbursement landed but the replay reported failure "
        "(effect unverified; retry double-pays) |\n"
        "| UNDETECTED-FAILURE | replay reported **success** but nothing was "
        "booked, or it was booked wrong (phantom / partial) |"
    )
    lines.append("")
    lines.append("## Results by fault class")
    lines.append("")
    lines.append(
        "| fault class | title | injected at the boundary | outcome (n) | "
        "replay said | silently mishandled? |"
    )
    lines.append("|---|---|---|---|---|---|")
    for r in results:
        counts = ", ".join(f"{k} x{v}" for k, v in sorted(r.outcome_counts.items()))
        said = "SUCCESS" if r.example_report_success else "FAILURE"
        sm = (
            f"**YES ({r.silently_mishandled_count}/{r.repeats})**"
            if r.silently_mishandled_count
            else "no"
        )
        lines.append(
            f"| {r.fault_class} | {r.title} | {r.injected} | {counts} | {said} | {sm} |"
        )
    lines.append("")

    silent = [r for r in results if r.silently_mishandled_count]
    transactional = [r for r in results if r.fault_class[0].isdigit()]
    lines.append("## The headline: silently mishandled transactional faults")
    lines.append("")
    lines.append(
        "A silently-mishandled fault on a consequential write - the replay "
        "reports a clean success while the record system is wrong - is the "
        "dangerous case. On this corpus the screen-only replay **silently "
        f"mishandles {len(silent)} of the {len(transactional)} transactional "
        "fault classes** (the replay reports success while ground truth is "
        "wrong):"
    )
    lines.append("")
    for r in silent:
        lines.append(f"- **{r.title}** - {r.headline}")
    lines.append("")
    lines.append(_RECOMMENDATIONS)
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```\npython -m benchmark.lending_fault_model.run\n```")
    lines.append("")
    lines.append(
        "Deterministic: every fault is injected at the boundary and the "
        "compiled replay is fixed, so repeats agree. Counts are shown for "
        "completeness."
    )
    lines.append("")
    return "\n".join(lines)


def build_faults(only: Optional[set[str]]) -> list[F.Fault]:
    if not only:
        return list(F.FAULTS)
    return [f for f in F.FAULTS if f.mode in only]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repeats", type=int, default=10, help="runs per fault")
    ap.add_argument("--quick", action="store_true", help="2 repeats, smoke")
    ap.add_argument("--only", default="", help="comma-separated fault modes to run")
    ap.add_argument("--out", default=str(HERE), help="output directory")
    args = ap.parse_args()

    # Hard guarantee: no Anthropic calls.
    os.environ.pop("ANTHROPIC_API_KEY", None)

    repeats = 2 if args.quick else args.repeats
    only = {a for a in args.only.split(",") if a} or None
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = out_dir / "runs"
    faults = build_faults(only)

    from playwright.sync_api import sync_playwright

    url, _db, stop = fault_serve(port=0)
    all_obs: list[RunObs] = []
    results: list[ClassResult] = []
    try:
        rec_dir = out_dir / "_recording"
        bundle_dir = out_dir / "_bundle"
        print(f"[setup] recording demo against {url}")
        recording = record_disbursement_demo(url, rec_dir, memo_text=F.MEMO_TEXT)
        print(f"[setup] compiling bundle -> {bundle_dir}")
        workflow = compile_recording(recording, bundle_dir, name="disburse-demo")
        total_steps = len(workflow.steps)
        print(f"[setup] bundle has {total_steps} steps")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for fault in faults:
                    obs: list[RunObs] = []
                    for rep in range(repeats):
                        run_dir = runs_dir / f"{fault.mode}_{rep}"
                        o = replay_once(browser, bundle_dir, url, run_dir, fault, rep)
                        obs.append(o)
                        all_obs.append(o)
                    res = aggregate(fault, obs)
                    results.append(res)
                    flag = (
                        "  <<< SILENTLY MISHANDLED"
                        if res.silently_mishandled_count
                        else ""
                    )
                    print(
                        f"[{fault.mode:11s}] "
                        f"{res.dominant_outcome:18s} "
                        f"(said {'SUCCESS' if res.example_report_success else 'FAIL'})"
                        f"{flag}"
                    )
            finally:
                browser.close()
    finally:
        stop()

    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": (
            f"{platform.system()} {platform.machine()} py{platform.python_version()}"
        ),
        "total_steps": total_steps,
        "repeats": repeats,
        "memo_text": F.MEMO_TEXT,
        "target_loan": F.TARGET_LOAN,
        "model_calls": 0,
    }
    doc = {
        "meta": meta,
        "classes": [asdict(r) for r in results],
        "runs": [asdict(o) for o in all_obs],
    }
    (out_dir / "results.json").write_text(json.dumps(doc, indent=2))
    (out_dir / "LENDING_FAULT_MODEL.md").write_text(render_markdown(results, meta))

    silent = [r for r in results if r.silently_mishandled_count]
    print(
        f"\n[done] {len(all_obs)} replays; "
        f"silently-mishandled fault classes: {len(silent)}"
    )
    for r in silent:
        print(f"  SILENT: {r.title} -> {r.dominant_outcome}")
    print(f"[done] wrote {out_dir / 'results.json'} and LENDING_FAULT_MODEL.md")


if __name__ == "__main__":
    main()
