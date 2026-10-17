"""Parameterization-depth experiment against the OpenEMR public demo.

Question: the shipped OpenEMR benchmark parameterizes only the note TEXT.
What happens when the PATIENT is parameterized — a value that changes what
appears on screen (different search results, different chart, different
dashboard content)?

Design: record the same 18-step add-patient-note workflow as
``scripts/openemr_demo.py`` but with the patient-search text recorded as a
workflow parameter (``patient``, demonstrated value "Phil"). Then replay the
one compiled bundle with:

1. ``patient=Phil``   (control — the demonstrated patient)
2. ``patient=Susan``  (drift — a different fake demo patient,
   "Underwood, Susan Ardmore", also a unique search match)

and, in ``cross-instance`` mode, replay the control against the /b/ demo
instance (same OpenEMR 8.0.0 version, separate database) to measure
cross-instance state drift. The public farm runs one version everywhere, so
true cross-VERSION drift is not testable here.

Zero model calls: replays are compiled-replay only (no grounder, no API).
Public-demo courtesy: fresh browser per run, runs paced >= 30 s apart, fake
demo patients only. Artifacts land in ``runs/validation/track-d/``
(gitignored).

    .venv/bin/python scripts/openemr_param_depth.py record
    .venv/bin/python scripts/openemr_param_depth.py replay
    .venv/bin/python scripts/openemr_param_depth.py cross-instance
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
sys.path.insert(0, str(REPO / "scripts"))

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.recorder import Recorder

from openemr_demo import (  # noqa: E402 - sibling script, path set above
    DEMO_PASS,
    DEMO_URL,
    DEMO_USER,
    SETTLE,
    _center,
    _frame,
    _locate_any_frame,
)

# The /b/ alternate instance. The /a/ instance rejected its own published
# admin credentials on 2026-07-08 (verified with DOM selectors, so not a
# replay artifact) — demo-farm weather worth knowing about.
ALT_DEMO_URL = "https://demo.openemr.io/b/openemr/index.php"

WORK = REPO / "runs" / "validation" / "track-d"
RECORDING_DIR = WORK / "recording"
BUNDLE_DIR = WORK / "bundle"
RUNS_DIR = WORK / "runs"

RECORDED_PATIENT = "Phil"  # demonstrated value; unique match "Belford, Phil"
RECORDED_NOTE = (
    "Validation baseline: reviewed home readings, plan unchanged today."
)

PACING_S = 30.0  # public-demo courtesy between browser sessions


def record() -> Path:
    """Record the add-note demo with the patient search text parameterized."""
    if RECORDING_DIR.exists():
        shutil.rmtree(RECORDING_DIR)
    backend, close = PlaywrightBackend.launch(DEMO_URL, headless=True)
    try:
        page = backend.page
        page.wait_for_load_state("networkidle", timeout=60000)
        recorder = Recorder(backend, RECORDING_DIR, app_url=DEMO_URL, **SETTLE)

        recorder.click(*_center(page.locator("#authUser")))
        recorder.type_text(DEMO_USER)
        recorder.click(*_center(page.locator("#clearPass")))
        recorder.type_text(DEMO_PASS)
        recorder.click(*_center(page.locator("#login-button")))
        page.wait_for_load_state("networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        # The ONLY difference from openemr_demo.record: the search text is a
        # workflow parameter.
        recorder.click(*_center(page.locator("input[placeholder*='Search']")))
        recorder.type_text(RECORDED_PATIENT, param="patient")
        recorder.press("Enter")
        page.wait_for_timeout(3000)

        finder = _frame(page, "dynamic_finder")
        recorder.click(
            *_center(finder.locator("a", has_text=RECORDED_PATIENT).first)
        )
        page.wait_for_timeout(4000)

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

        recorder.click(*_center(pencil))
        page.wait_for_timeout(3000)
        pnotes = _frame(page, "pnotes_full")
        recorder.click(*_center(
            pnotes.locator("button, a").filter(has_text="Add").first
        ))
        page.wait_for_timeout(2000)
        recorder.click(*_center(_locate_any_frame(page, "textarea#note")))
        recorder.type_text(RECORDED_NOTE, param="note")
        recorder.click(*_center(_locate_any_frame(
            page, "text=Save as new message")))
        page.wait_for_timeout(3000)
        return recorder.finish()
    finally:
        close()


def compile_bundle() -> None:
    from openadapt_flow.compiler import compile_recording

    if BUNDLE_DIR.exists():
        shutil.rmtree(BUNDLE_DIR)
    wf = compile_recording(
        RECORDING_DIR, BUNDLE_DIR, name="openemr-param-depth"
    )
    print(f"compiled {len(wf.steps)} steps -> {BUNDLE_DIR}")
    for step in wf.steps:
        texts = [pc.text for pc in step.expect if pc.text]
        print(f"  {step.id}: {step.intent}  text_present={texts}")


def _replay_one(label: str, params: dict[str, str], url: str) -> dict:
    """One fresh-browser compiled replay; returns a summary row."""
    from openadapt_flow.benchmark.verify import verify_note_saved
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime.replayer import Replayer
    from openadapt_flow.vision.ocr import normalize_text, ocr

    run_dir = RUNS_DIR / f"run-{label}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    workflow = Workflow.load(BUNDLE_DIR)
    backend, close = PlaywrightBackend.launch(url, headless=True)
    try:
        backend.page.wait_for_load_state("networkidle", timeout=60000)
        t0 = time.monotonic()
        report = Replayer(backend).run(
            workflow, params=params, bundle_dir=BUNDLE_DIR, run_dir=run_dir
        )
        elapsed = time.monotonic() - t0
        final_png = backend.screenshot()
        (run_dir / "final.png").write_bytes(final_png)
    finally:
        close()

    note = params["note"]
    note_check = verify_note_saved(final_png, note)
    final_text = normalize_text(
        " ".join(line.text for line in ocr(final_png))
    )
    failed = [r for r in report.results if not r.ok]
    row = {
        "label": label,
        "url": url,
        "params": params,
        "success": report.success,
        "steps_ok": sum(1 for r in report.results if r.ok),
        "steps_total": len(workflow.steps),
        "rungs": report.rung_counts,
        "heals": report.heal_count,
        "model_calls": report.model_calls,
        "elapsed_s": round(elapsed, 1),
        "note_visible_on_final": note_check.success,
        "belford_on_final": "belford" in final_text,
        "underwood_on_final": "underwood" in final_text,
        "first_failure": (
            {"step": failed[0].step_id, "error": (failed[0].error or "")[:300]}
            if failed else None
        ),
    }
    print(json.dumps(row))
    return row


def replay() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    plan = [
        ("phil-control", {
            "patient": "Phil",
            "note": "Param depth control: confirmed refill pickup scheduled.",
        }, DEMO_URL),
        ("susan-drift", {
            "patient": "Susan",
            "note": "Param depth drift: dietary counseling summary delivered.",
        }, DEMO_URL),
    ]
    for i, (label, params, url) in enumerate(plan):
        if i:
            time.sleep(PACING_S)
        rows.append(_replay_one(label, params, url))
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "runs": rows,
    }
    (RUNS_DIR / "summary.json").write_text(json.dumps(summary, indent=2))


def cross_instance() -> None:
    """Replay the main-instance recording against the /b/ demo instance."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    time.sleep(PACING_S)  # courtesy pacing after any preceding demo session
    row = _replay_one(
        "cross-instance-b",
        {
            "patient": "Phil",
            "note": "Cross instance drift: immunization history reconciled.",
        },
        ALT_DEMO_URL,
    )
    (RUNS_DIR / "cross_instance.json").write_text(json.dumps(row, indent=2))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("record", "all"):
        rec = record()
        print("recorded ->", rec)
        compile_bundle()
    if mode == "compile":
        compile_bundle()
    if mode in ("replay", "all"):
        if mode == "all":
            time.sleep(PACING_S)
        replay()
    if mode == "cross-instance":
        cross_instance()
