"""Held-out test of the local grounding rung (#1596) on real AVC444 frames.

The rung's thresholds (``ACCEPT_SCORE``, ``LOOKALIKE_MARGIN``) were set on a
synthetic Chromium fixture (``benchmark/pixel_grounding``). This script
applies it, unchanged, to frames captured from a real H.264/AVC444 RDP
session: for every click step of each campaign's compiled bundle, and every
condition whose run frames were kept, it shows the model the replay's
``before`` frame and the recorded crop, and records what the resolver's rule
makes of the look-alikes (``_model_grounded``), the look-alike count, the
true and best-other scores, the query time, and the template matcher on the
same crop for comparison.

Ground truth is the place the target really was on that frame:

- for a frame from a trial whose save the system of record confirmed, the
  point that trial's replay clicked (its report's resolution);
- otherwise the recorded click point, when the condition leaves the layout
  unchanged (healthy, 16-bit colour, Patient Notes reorder), shifted to the
  right row for the Patient Notes look-alike rows (decoy, siblings);
- otherwise no truth: the row is reported as ``truth: null`` and not scored.

A point is on target within ``TOL_X`` x ``TOL_Y`` pixels of the truth.

    python grounding_heldout.py <campaign out dir> [...] --output results_grounding_heldout.json
"""

from __future__ import annotations

import argparse
import io
import json
import platform
import statistics
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

TOL_X, TOL_Y = 24, 12
PN_ROW_PITCH = 44
# Rows above Neil Sorenson after searching "Neil", per pn_db.py's ordering.
PN_ROWS_ABOVE = {"decoy": 1, "siblings": 1}
LAYOUT_SAME = {"healthy", "bpp16", "reorder", "decoy", "siblings"}
REPEATS = 3


def _load_bundle(campaign: Path, app: str, tmp: Path):
    from openadapt_flow.ir import Workflow

    dest = tmp / f"{campaign.name}_bundle"
    with zipfile.ZipFile(campaign / f"{app}_bundle.zip") as zf:
        zf.extractall(dest)
    return Workflow.load(dest), dest


def _verified_points(campaign: Path, trials: list[dict], condition: str) -> dict:
    """step_id -> clicked point, from the condition's first trial if verified."""
    first = next((i for i, t in enumerate(trials) if t["condition"] == condition), None)
    if first is None or trials[first]["class"] != "verified_save":
        return {}
    return {s["step_id"]: tuple(s["point"]) for s in trials[first]["steps"] if s["point"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaigns", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from openadapt_flow import vision
    from openadapt_flow.runtime.local_grounder import ACCEPT_SCORE, LOOKALIKE_MARGIN, LocalGroundingModel
    from openadapt_flow.runtime.resolver import TEMPLATE_THRESHOLD, _model_grounded, _scaled_click_point
    from openadapt_flow.vision.ocr import OcrResolutionRefused

    model = LocalGroundingModel.load()
    rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for campaign in args.campaigns:
            trials = json.loads((campaign / "trials.json").read_text())
            app = "pn" if (campaign / "pn_bundle.zip").exists() else "emr"
            workflow, bundle_dir = _load_bundle(campaign, app, tmp)
            clicks = {s.id: s for s in workflow.steps if s.anchor is not None}
            for run_zip in sorted(campaign.glob(f"{app}_run_*.zip")):
                condition = run_zip.stem.split("_run_", 1)[1]
                verified = _verified_points(campaign, trials, condition)
                with zipfile.ZipFile(run_zip) as zf:
                    names = set(zf.namelist())
                    for step_id, step in clicks.items():
                        name = f"steps/{step_id}_before.png"
                        if name not in names:
                            continue
                        png = zf.read(name)
                        anchor = step.anchor
                        if step_id in verified:
                            truth, source = verified[step_id], "verified_trial"
                        elif condition in LAYOUT_SAME:
                            x, y = anchor.click_point
                            if app == "pn" and abs(y - 145) <= 6:
                                y += PN_ROW_PITCH * PN_ROWS_ABOVE.get(condition, 0)
                            truth, source = (x, y), "recorded_layout"
                        else:
                            truth, source = None, None
                        template = (bundle_dir / anchor.template).read_bytes()

                        def on_target(pt, truth=truth):
                            return (truth is not None and abs(pt[0] - truth[0]) <= TOL_X
                                    and abs(pt[1] - truth[1]) <= TOL_Y)

                        timings, found = [], []
                        for _ in range(REPEATS):
                            model._template_cache.clear()
                            started = time.monotonic()
                            found = model.locate_anchor(png, template)
                            timings.append((time.monotonic() - started) * 1000.0)
                        try:
                            grounded = _model_grounded(anchor, png, vision, model.locate_anchor, template)
                            if grounded is None:
                                rung = "abstain"
                            elif truth is None:
                                rung = "picked_unscored"
                            else:
                                rung = "target" if on_target(grounded[0]) else "wrong_target"
                        except OcrResolutionRefused as exc:
                            rung = "halt:" + type(exc).__name__
                        scored = model.candidates(png, template)
                        point_of = lambda c, a=anchor: _scaled_click_point(a, tuple(c.region))  # noqa: E731
                        true_scores = [c.score for c in scored if on_target(point_of(c))]
                        other_scores = [c.score for c in scored if not on_target(point_of(c))]
                        started = time.monotonic()
                        tmatch = vision.find_template(png, template, threshold=TEMPLATE_THRESHOLD)
                        template_ms = (time.monotonic() - started) * 1000.0
                        tpoint = _scaled_click_point(anchor, tuple(tmatch.region)) if tmatch else None
                        rows.append({
                            "app": app, "campaign": campaign.name, "condition": condition,
                            "step_id": step_id, "text_label": anchor.ocr_text,
                            "truth": list(truth) if truth else None, "truth_source": source,
                            "rung_outcome": rung, "lookalikes": len(found),
                            "true_found": any(on_target(point_of(c)) for c in found),
                            "true_score": round(max(true_scores), 3) if true_scores else None,
                            "best_other_score": round(max(other_scores), 3) if other_scores else None,
                            "ms": round(statistics.median(timings), 1),
                            "template_found": bool(tpoint and on_target(tpoint)),
                            "template_wrong": bool(tpoint and truth and not on_target(tpoint)),
                            "template_ms": round(template_ms, 1),
                        })
                        print(json.dumps(rows[-1]), flush=True)

    scored = [r for r in rows if r["truth"] is not None]
    summary = {
        "frames_steps": len(rows), "scored": len(scored),
        "rung_outcomes": {k: sum(1 for r in scored if r["rung_outcome"] == k)
                          for k in sorted({r["rung_outcome"] for r in scored})},
        "wrong_target": sum(1 for r in scored if r["rung_outcome"] == "wrong_target"),
        "abstain": sum(1 for r in scored if r["rung_outcome"] == "abstain"),
        "abstain_text_label": sum(1 for r in scored if r["rung_outcome"] == "abstain" and r["text_label"]),
        "template_found": sum(1 for r in scored if r["template_found"]),
        "template_wrong": sum(1 for r in scored if r["template_wrong"]),
        "median_ms": round(statistics.median(r["ms"] for r in rows), 1) if rows else None,
        "max_ms": max((r["ms"] for r in rows), default=None),
    }
    args.output.write_text(json.dumps({
        "schema": "openadapt.rdp-avc444-grounding-heldout.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": {"machine": platform.machine(), "system": platform.system(), "python": sys.version.split()[0]},
        "thresholds": {"accept_score": ACCEPT_SCORE, "lookalike_margin": LOOKALIKE_MARGIN,
                       "template_threshold": TEMPLATE_THRESHOLD, "tolerance_px": [TOL_X, TOL_Y]},
        "summary": summary, "rows": rows,
    }, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
