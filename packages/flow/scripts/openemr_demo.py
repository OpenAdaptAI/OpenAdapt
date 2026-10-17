"""Record, compile, and replay a clinical workflow against the OpenEMR demo.

Target: the official OpenEMR public demo (https://demo.openemr.io/openemr),
which ships fake patients only and resets itself daily. Workflow: log in as
the demo admin, search the demo patient "Phil" (Belford, Phil), open the
chart, scroll the dashboard to the Messages card, open Patient Messages,
add a note (parameterized), save, and land back on the message list.

Record time cheats with Playwright locators to find pixel coordinates
(exactly like ``demo_driver.record_triage_demo``); every action goes through
``Recorder`` so frames/events are captured. Replay is vision-only.

Not shipped in the package — this is the showcase driver:

    .venv/bin/python scripts/openemr_demo.py record     # record + compile
    .venv/bin/python scripts/openemr_demo.py replay     # replay 5x, report
    .venv/bin/python scripts/openemr_demo.py all
    .venv/bin/python scripts/openemr_demo.py benchmark  # compiled vs agent

Showcase artifacts land in docs/showcase-openemr/; ``benchmark`` records
a fresh demonstration into a temporary directory and writes results to
benchmark/openemr/ (see openadapt_flow.benchmark.openemr_benchmark).
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.recorder import Recorder

DEMO_URL = "https://demo.openemr.io/openemr/index.php"
SHOWCASE = REPO / "docs" / "showcase-openemr"
RECORDING_DIR = SHOWCASE / "recording"
BUNDLE_DIR = SHOWCASE / "bundle"
RUNS_DIR = SHOWCASE / "runs"

# Demo credentials published at https://www.open-emr.org/demo/ (fake data,
# resets daily). Never point this script at a real OpenEMR install.
DEMO_USER = "admin"
DEMO_PASS = "pass"
PATIENT_SEARCH = "Phil"  # matches demo patient "Belford, Phil" only

RECORDED_NOTE = (
    "Reviewed hypertension follow-up; BP within target range today."
)

# Distinct per-run values so replay success proves parameter substitution,
# not replay of the baked-in literal.
REPLAY_NOTES = [
    "Replay run 1: medication adherence confirmed, refill authorized.",
    "Replay run 2: patient reports no dizziness since dose change.",
    "Replay run 3: schedule renal panel before next visit.",
    "Replay run 4: discussed low-sodium diet, handout provided.",
    "Replay run 5: home BP log reviewed, readings stable.",
]

SETTLE = dict(settle_timeout_s=10.0, settle_stable_frames=3,
              settle_interval_s=0.3)


def _center(locator) -> tuple[int, int]:
    """Center viewport-pixel coordinates of a (frame-)locator's element."""
    locator.wait_for(state="visible", timeout=30000)
    box = locator.bounding_box()
    if box is None:
        raise RuntimeError(f"no bounding box for {locator}")
    return (int(box["x"] + box["width"] / 2),
            int(box["y"] + box["height"] / 2))


def _frame(page, url_fragment: str):
    """The first frame whose URL contains ``url_fragment`` (waits up to 30s)."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for frame in page.frames:
            if url_fragment in frame.url:
                return frame
        page.wait_for_timeout(250)
    raise RuntimeError(f"no frame matching {url_fragment!r}")


def _locate_any_frame(page, selector: str):
    """First visible match for ``selector`` across all frames (30s budget).

    OpenEMR opens its add-note modal as a second nested ``pnotes_full``
    iframe, so matching by frame URL is ambiguous — match by content.
    """
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for frame in page.frames:
            try:
                locator = frame.locator(selector).first
                if locator.count() and locator.is_visible():
                    return locator
            except Exception:  # noqa: BLE001 - frame may be mid-navigation
                continue
        page.wait_for_timeout(250)
    raise RuntimeError(f"no visible {selector!r} in any frame")


def record(headed: bool = False, recording_dir: Path = RECORDING_DIR) -> Path:
    """Record the OpenEMR demonstration; returns the recording dir."""
    if recording_dir.exists():
        shutil.rmtree(recording_dir)
    backend, close = PlaywrightBackend.launch(DEMO_URL, headless=not headed)
    try:
        page = backend.page
        page.wait_for_load_state("networkidle", timeout=60000)
        recorder = Recorder(backend, recording_dir, app_url=DEMO_URL, **SETTLE)

        # -- login -----------------------------------------------------------
        recorder.click(*_center(page.locator("#authUser")))
        recorder.type_text(DEMO_USER)
        recorder.click(*_center(page.locator("#clearPass")))
        recorder.type_text(DEMO_PASS)
        recorder.click(*_center(page.locator("#login-button")))
        page.wait_for_load_state("networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        # -- search the demo patient ------------------------------------------
        recorder.click(*_center(page.locator("input[placeholder*='Search']")))
        recorder.type_text(PATIENT_SEARCH)
        recorder.press("Enter")
        page.wait_for_timeout(3000)

        # -- open the chart ----------------------------------------------------
        finder = _frame(page, "dynamic_finder")
        recorder.click(*_center(finder.locator("a", has_text=PATIENT_SEARCH).first))
        page.wait_for_timeout(4000)

        # -- scroll the dashboard until the Messages card pencil is on screen --
        demographics = _frame(page, "demographics.php")
        pencil = demographics.locator("a[href*='pnotes_full']").first
        pencil.wait_for(state="attached", timeout=30000)
        for _ in range(12):
            box = pencil.bounding_box()
            if box is not None and 100 < box["y"] < 700:
                break
            recorder.scroll(0, 400)
        else:
            raise RuntimeError("Messages card never scrolled into view")

        # -- open Patient Messages, add a note, save ---------------------------
        recorder.click(*_center(pencil))
        page.wait_for_timeout(3000)
        pnotes = _frame(page, "pnotes_full")
        recorder.click(*_center(pnotes.locator("button, a").filter(
            has_text="Add").first))
        page.wait_for_timeout(2000)
        # The add-note form opens as a second nested pnotes_full iframe.
        recorder.click(*_center(_locate_any_frame(page, "textarea#note")))
        recorder.type_text(RECORDED_NOTE, param="note")
        recorder.click(*_center(_locate_any_frame(
            page, "text=Save as new message")))
        page.wait_for_timeout(3000)

        return recorder.finish()
    finally:
        close()


def compile_bundle(
    recording_dir: Path = RECORDING_DIR, bundle_dir: Path = BUNDLE_DIR
) -> None:
    from openadapt_flow.compiler import compile_recording

    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    workflow = compile_recording(
        recording_dir, bundle_dir, name="openemr-add-patient-note"
    )
    print(f"compiled {len(workflow.steps)} steps -> {bundle_dir}")
    for step in workflow.steps:
        print(f"  {step.id}: {step.intent}  expect={len(step.expect)}")


def _note_visible(final_png: bytes, note: str) -> bool:
    """True when the replayed note text is visible on the final screen.

    Delegates to the benchmark's shared arm-independent check
    (``verify_note_saved``) so the criterion is implemented exactly once.
    """
    from openadapt_flow.benchmark.verify import verify_note_saved

    return verify_note_saved(final_png, note).success


def replay(n: int = 5) -> dict:
    """Replay the bundle n times, fresh browser each; returns summary."""
    from openadapt_flow.ir import Workflow
    from openadapt_flow.report import render_run_report
    from openadapt_flow.runtime.replayer import Replayer
    import openadapt_flow.vision as vision

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    summary: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "demo_url": DEMO_URL,
        "runs": [],
    }
    for i in range(n):
        run_dir = RUNS_DIR / f"run-{i + 1}"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        note = REPLAY_NOTES[i % len(REPLAY_NOTES)]
        workflow = Workflow.load(BUNDLE_DIR)
        backend, close = PlaywrightBackend.launch(DEMO_URL, headless=True)
        try:
            backend.page.wait_for_load_state("networkidle", timeout=60000)
            replayer = Replayer(backend)
            t0 = time.monotonic()
            report = replayer.run(
                workflow,
                params={"note": note},
                bundle_dir=BUNDLE_DIR,
                run_dir=run_dir,
            )
            elapsed = time.monotonic() - t0
            # Extra verification (not a workflow postcondition, since the
            # note value is parameterized): the replayed note text must be
            # visible on the final screen.
            final_png = backend.screenshot()
            (run_dir / "final.png").write_bytes(final_png)
            note_visible = _note_visible(final_png, note)
        except Exception as exc:  # noqa: BLE001 - record and continue
            summary["runs"].append({
                "run": i + 1, "success": False, "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"run {i + 1}: EXCEPTION {exc}")
            continue
        finally:
            close()
        failed = [r for r in report.results if not r.ok]
        summary["runs"].append({
            "run": i + 1,
            "success": report.success,
            "note_visible": note_visible,
            "steps_ok": sum(1 for r in report.results if r.ok),
            "steps_total": len(workflow.steps),
            "rung_counts": report.rung_counts,
            "heal_count": report.heal_count,
            "total_s": round(elapsed, 1),
            "first_failure": (
                {"step": failed[0].step_id, "error": failed[0].error}
                if failed else None
            ),
        })
        try:
            render_run_report(run_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"run {i + 1}: report render failed: {exc}")
        print(f"run {i + 1}: success={report.success} "
              f"note_visible={note_visible} rungs={report.rung_counts} "
              f"heals={report.heal_count} {elapsed:.0f}s")
    ok = sum(1 for r in summary["runs"] if r.get("success"))
    verified = sum(
        1 for r in summary["runs"] if r.get("success") and r.get("note_visible")
    )
    summary["success_rate"] = f"{ok}/{len(summary['runs'])}"
    summary["verified_rate"] = f"{verified}/{len(summary['runs'])}"
    (RUNS_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return summary


def benchmark() -> None:
    """Record a fresh demonstration, then run compiled-vs-agent benchmark.

    Records into a temporary directory (the committed showcase artifacts
    under docs/showcase-openemr/ are left untouched) and writes rows.jsonl
    (incremental, per run) plus results.json / BENCHMARK.md /
    latency_cost.png to benchmark/openemr/.

    Hard cost guardrails (see openadapt_flow.benchmark.openemr_benchmark):
    an API preflight before any spend, a $1.50 per-run cap, an $8.00
    total agent-arm ceiling, and an abort on consecutive billing errors.
    All at list price — billed cost under the intro rate is lower.
    """
    import tempfile

    from openadapt_flow.benchmark import agent_baseline
    from openadapt_flow.benchmark.openemr_benchmark import (
        MAX_TOTAL_COST_USD,
        run_openemr_benchmark,
    )

    with tempfile.TemporaryDirectory(prefix="oaf-openemr-rec-") as tmp_str:
        tmp = Path(tmp_str)
        recording_dir = tmp / "recording"
        bundle_dir = tmp / "bundle"
        rec = record(recording_dir=recording_dir)
        print("recorded ->", rec)
        compile_bundle(recording_dir=recording_dir, bundle_dir=bundle_dir)
        run_openemr_benchmark(
            REPO / "benchmark" / "openemr",
            bundle_dir,
            max_cost_per_run_usd=agent_baseline.MAX_COST_USD,
            max_total_cost_usd=MAX_TOTAL_COST_USD,
        )


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("record", "all"):
        rec = record()
        print("recorded ->", rec)
        compile_bundle()
    if mode == "compile":
        compile_bundle()
    if mode in ("replay", "all"):
        replay(5)
    if mode == "benchmark":
        benchmark()
