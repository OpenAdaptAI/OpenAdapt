#!/usr/bin/env python3
"""Pixel-path rendering sweep: template ladder vs the local grounding model.

Records the scheduling fixture once in Chromium at 100% scaling, light theme,
compiles it, and replays the bundle through a pixel-only backend under each
rendering condition in ``CONDITIONS`` (display scaling, application zoom,
font size, theme, lossy remote codec). Each condition runs three campaigns:

- ``healthy``: Ada Lovelace stays the active record. A correct run saves one
  appointment for her with the replay values and the summary attached.
- ``wrong_record``: after the note is typed and before Save, the active record
  switches to Grace Hopper. A correct run stops with nothing saved.
- ``reordered``: the worklist shows Ada on another row. A correct run opens
  Ada's row, or stops. Opening any other row's look-alike pencil is a wrong
  target.

Each run is scored from the page itself, after the run: the element every
click landed on, and the saved rows. ``wrong_target`` means a click landed on
a control the workflow never clicked (another row's pencil, the Save draft
icon, the cancel-visit icon) or anywhere in another patient's row. That is
the outcome the rung must never cause. A press on plain text or background
that does nothing is a dead click, counted separately.

``--arm template`` replays with the shipped ladder (template, OCR, geometry,
no grounder). ``--arm model`` adds the local grounding rung
(:class:`openadapt_flow.runtime.local_grounder.LocalGroundingModel`).

Usage::

    python benchmark/pixel_grounding/run_sweep.py --arm template --output results_template.json
    python benchmark/pixel_grounding/run_sweep.py --arm model --output results_model.json
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
for _path in (str(HERE.parents[1]), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fixture_backend import Condition, FixtureSession, PixelOnlyBackend  # noqa: E402

SCHEMA = "openadapt.pixel-grounding-sweep.v1"
RECORD = Condition("record", "record")
CONDITIONS: tuple[Condition, ...] = (
    Condition("scale_100", "display_scale", scale=1.0),
    Condition("scale_125", "display_scale", scale=1.25),
    Condition("scale_150", "display_scale", scale=1.5),
    Condition("scale_200", "display_scale", scale=2.0),
    Condition("zoom_110", "zoom", zoom=1.1),
    Condition("zoom_125", "zoom", zoom=1.25),
    Condition("zoom_150", "zoom", zoom=1.5),
    Condition("font_plus2", "font_size", font_delta_px=2),
    Condition("font_plus4", "font_size", font_delta_px=4),
    Condition("dark_100", "theme", dark=True),
    Condition("dark_150", "theme", dark=True, scale=1.5),
    Condition("codec_q50_100", "codec", codec_quality=50),
    Condition("codec_q50_150", "codec", codec_quality=50, scale=1.5),
)
CAMPAIGNS = ("healthy", "wrong_record", "reordered")
ADA = 2
GRACE = 0
REORDER = [5, 0, 6, 1, 7, 2, 3, 4]  # Ada (patient 2) on the sixth row

DEMO = {"visit_type": "Cardiology follow-up", "note": "bring prior ECG"}
REPLAY = {"visit_type": "Intake consultation", "note": "recheck labs next visit"}
EXPECTED_CLICKS = {f"edit-{ADA}", "visit", "note", "tb-attach", "save"}


def _row_box(session: FixtureSession, patient: int) -> tuple[int, int, int, int]:
    box = session.page.evaluate(
        """p => { const r = document.getElementById('edit-' + p).closest('tr')
                  .getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }""",
        patient,
    )
    return tuple(int(round(v)) for v in box)  # type: ignore[return-value]


def _center(box: tuple[int, int, int, int]) -> tuple[int, int]:
    return (box[0] + box[2] // 2, box[1] + box[3] // 2)


def record(browser: Any, work: Path) -> tuple[Any, Path]:
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.recorder import Recorder

    session = FixtureSession(browser, RECORD)
    backend = PixelOnlyBackend(session)
    rec_dir = work / "recording"
    bundle_dir = work / "bundle"
    rec = Recorder(
        backend,
        rec_dir,
        settle_interval_s=0.05,
        settle_stable_frames=2,
        settle_timeout_s=3.0,
    )
    row = _row_box(session, ADA)
    banner = session.element_box("banner")
    rec.click(*_center(session.element_box(f"edit-{ADA}")))
    rec.click(*_center(session.element_box("visit")))
    rec.type_text(DEMO["visit_type"], param="visit_type")
    rec.click(*_center(session.element_box("note")))
    rec.type_text(DEMO["note"], param="note")
    rec.click(*_center(session.element_box("tb-attach")))
    rec.click(*_center(session.element_box("save")))
    rec.finish()
    session.close()
    # Pixel identities, as in benchmark/rdp_ladder: the row click binds to
    # Ada's row; every later click binds to the active-record banner.
    path = rec_dir / "events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines() if line]
    regions = {0: row, 1: banner, 3: banner, 5: banner, 6: banner}
    for index, region in regions.items():
        assert events[index]["kind"] == "click", events[index]
        events[index]["identifier_region"] = list(region)
    path.write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in events),
        encoding="utf-8",
    )
    workflow = compile_recording(rec_dir, bundle_dir, name="pixel-grounding-emr")
    return workflow, bundle_dir


def _gate(result: Any) -> str:
    error = (result.error or "").lower()
    if "layout drift" in error:
        return "layout_drift"
    if result.identity is not None and result.identity.status != "verified":
        return "identity"
    if result.resolution is None:
        return "target_resolution"
    if "irreversible" in error:
        return "risk_gate"
    if "census" in error or "look-alike" in error or "ambiguous" in error:
        return "census"
    if result.input_verified is False:
        return "input_verification"
    if result.postconditions_ok is False:
        return "postcondition"
    return "other"


def replay(
    browser: Any,
    bundle_dir: Path,
    condition: Condition,
    campaign: str,
    *,
    grounder: Optional[Any],
    run_dir: Path,
) -> dict[str, Any]:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime.replayer import Replayer

    workflow = Workflow.load(bundle_dir)
    order = REORDER if campaign == "reordered" else None

    def after_type(session: FixtureSession, text: str) -> None:
        if campaign == "wrong_record" and text == REPLAY["note"]:
            session.select_patient(GRACE)

    session = FixtureSession(browser, condition, order=order)
    backend = PixelOnlyBackend(session, after_type=after_type)
    started = time.monotonic()
    crash: Optional[str] = None
    report = None
    try:
        report = Replayer(
            backend,
            poll_interval_s=0.0,
            pixel_verify_enabled=True,
            # As on an RDP or Citrix bundle: the layout check and the
            # display-scale estimate (benchmark/pixel_scale) are on.
            layout_drift=True,
            grounder=grounder,
        ).run(workflow, params=REPLAY, bundle_dir=bundle_dir, run_dir=run_dir)
    except Exception as exc:  # noqa: BLE001 - a crash is an outcome
        crash = f"{type(exc).__name__}: {exc}"[:300]
    clicks = session.clicks()
    saved = session.saved()
    drafts = session.drafts()
    session.close()

    # A press on a control the workflow never uses, or anywhere in another
    # patient's row, is a wrong target. A press on plain text or background
    # in Ada's row or outside the list does nothing: a dead click.
    stray = [
        c
        for c in clicks
        if (c["control"] is not None and c["control"] not in EXPECTED_CLICKS)
        or (c["row"] is not None and c["row"] != ADA)
    ]
    dead = [c for c in clicks if c["control"] is None and c["row"] in (None, ADA)]
    expected_row = {
        "patient": "Ada Lovelace",
        "mrn": "MRN 36428",
        "visit": REPLAY["visit_type"],
        "note": REPLAY["note"],
        "attached": True,
    }
    success = bool(report is not None and report.success)
    if crash is not None:
        outcome = "crash"
    elif stray or drafts:
        outcome = "wrong_target"
    elif saved and saved != [expected_row]:
        outcome = "wrong_write"
    elif campaign == "wrong_record":
        outcome = "halted" if not saved and not success else "false_completion"
    elif success and saved == [expected_row]:
        outcome = "pass"
    elif success:
        outcome = "false_completion"
    else:
        outcome = "halted"

    steps = []
    stop: Optional[dict[str, Any]] = None
    for result in report.results if report is not None else []:
        step = {
            "step_id": result.step_id,
            "ok": result.ok,
            "rung": result.resolution.rung if result.resolution is not None else None,
            "resolve_ms": (
                round(result.resolution.elapsed_ms, 1)
                if result.resolution is not None
                else None
            ),
            "identity": result.identity.status if result.identity is not None else None,
        }
        steps.append(step)
        if not result.ok and stop is None:
            stop = {
                "step_id": result.step_id,
                "intent": result.intent,
                "gate": _gate(result),
                "rung": step["rung"],
                "error": (result.error or "")[:400],
            }
    return {
        "condition": condition.as_dict(),
        "campaign": campaign,
        "outcome": outcome,
        "clicked": clicks,
        "dead_clicks": len(dead),
        "rows_saved": len(saved),
        "model_calls": int(report.model_calls) if report is not None else 0,
        "rung_counts": dict(report.rung_counts) if report is not None else {},
        "render_scale": getattr(report, "render_scale", None),
        "stopped_at": stop,
        "steps": steps,
        "crash": crash,
        "elapsed_s": round(time.monotonic() - started, 2),
    }


def summarize(points: list[dict[str, Any]]) -> dict[str, Any]:
    by_condition: dict[str, dict[str, str]] = {}
    totals: dict[str, dict[str, int]] = {}
    for p in points:
        by_condition.setdefault(p["condition"]["name"], {})[p["campaign"]] = p[
            "outcome"
        ]
        bucket = totals.setdefault(p["campaign"], {})
        bucket[p["outcome"]] = bucket.get(p["outcome"], 0) + 1
    wrong = sum(
        1
        for p in points
        if p["outcome"] in ("wrong_target", "wrong_write", "false_completion")
    )
    return {"by_condition": by_condition, "totals": totals, "unsafe_outcomes": wrong}


def _git(*args: str) -> Optional[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=HERE, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def make_grounder(arm: str) -> Optional[Any]:
    if arm == "template":
        return None
    from openadapt_flow.runtime.local_grounder import LocalGroundingModel

    return LocalGroundingModel.load()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("template", "model"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", nargs="*", help="condition names to run")
    parser.add_argument("--campaigns", nargs="*", default=list(CAMPAIGNS))
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    grounder = make_grounder(args.arm)
    conditions = [c for c in CONDITIONS if not args.only or c.name in args.only]
    points: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        work = Path(tmp)
        browser = pw.chromium.launch()
        _workflow, bundle_dir = record(browser, work)
        for condition in conditions:
            for campaign in args.campaigns:
                run_dir = work / f"run_{condition.name}_{campaign}"
                point = replay(
                    browser,
                    bundle_dir,
                    condition,
                    campaign,
                    grounder=grounder,
                    run_dir=run_dir,
                )
                points.append(point)
                stop = point["stopped_at"] or {}
                print(
                    f"{condition.name:15s} {campaign:12s} {point['outcome']:15s} "
                    f"{stop.get('step_id', '')} {stop.get('gate', '')} "
                    f"rungs={point['rung_counts']} {point['elapsed_s']}s",
                    flush=True,
                )
        browser.close()
    payload = {
        "schema": SCHEMA,
        "arm": args.arm,
        "commit": _git("rev-parse", "HEAD"),
        "dirty": bool(_git("status", "--porcelain")),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": f"{platform.system()} {platform.machine()} py{platform.python_version()}",
        "grounder": None if grounder is None else grounder.describe(),
        "summary": summarize(points),
        "points": points,
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
