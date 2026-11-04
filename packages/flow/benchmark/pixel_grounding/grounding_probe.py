#!/usr/bin/env python3
"""Per-target look at the grounding model under each rendering condition.

For every click step the compiler recorded, and every condition in
``run_sweep.CONDITIONS``, the probe shows the model the live frame and the
recorded crop and records:

- ``lookalikes``: how many places the model counted as the target;
- ``true_found``: whether one of them is where the target really is, scored
  by mapping the recorded click offset into the candidate box and testing it
  against the element's box in the page;
- ``true_score`` / ``best_other_score``: the model's score for the true place
  and for the best other place;
- ``ms``: wall time of the query (edge search plus model);
- ``rung_outcome``: what the resolver's rule makes of those look-alikes
  (``resolver._model_grounded`` with the step's landmarks, OCR included):
  ``target``, ``wrong_target``, ``abstain`` (a text-labelled target, left
  to OCR, or nothing found) or ``halt:<refusal>``.

It also times the template matcher on the same crop
(``openadapt_flow.vision.find_template`` at the resolver's threshold) and
records whether it found the target.

Usage::

    python benchmark/pixel_grounding/grounding_probe.py --output results_grounding.json
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _path in (str(HERE.parents[1]), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fixture_backend import FixtureSession, PixelOnlyBackend  # noqa: E402
from run_sweep import ADA, CONDITIONS, _git, record  # noqa: E402

TARGETS = ("edit-2", "visit", "note", "tb-attach", "save")
REPEATS = 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    from openadapt_flow import vision
    from openadapt_flow.runtime.local_grounder import LocalGroundingModel
    from openadapt_flow.runtime.resolver import (
        TEMPLATE_THRESHOLD,
        _model_grounded,
        _scaled_click_point,
    )
    from openadapt_flow.vision.ocr import OcrResolutionRefused

    model = LocalGroundingModel.load()
    rows = []
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        browser = pw.chromium.launch()
        workflow, bundle_dir = record(browser, Path(tmp))
        clicks = [s for s in workflow.steps if s.anchor is not None]
        assert len(clicks) == len(TARGETS)
        for condition in CONDITIONS:
            session = FixtureSession(browser, condition)
            session.select_patient(ADA)
            png = PixelOnlyBackend(session).screenshot()
            for step, target in zip(clicks, TARGETS):
                template = (bundle_dir / step.anchor.template).read_bytes()
                x, y, w, h = session.element_box(target)

                def on_target(region, anchor=step.anchor, box=(x, y, w, h)):
                    px, py = _scaled_click_point(anchor, tuple(region))
                    return (
                        box[0] <= px <= box[0] + box[2]
                        and box[1] <= py <= box[1] + box[3]
                    )

                timings = []
                for _ in range(REPEATS):
                    model._template_cache.clear()
                    started = time.monotonic()
                    found = model.locate_anchor(png, template)
                    timings.append((time.monotonic() - started) * 1000.0)
                # What the rung decides: the look-alike count and landmarks.
                try:
                    grounded = _model_grounded(
                        step.anchor, png, vision, model.locate_anchor, template
                    )
                    if grounded is None:
                        rung = "abstain"
                    else:
                        px, py = grounded[0]
                        rung = (
                            "target"
                            if x <= px <= x + w and y <= py <= y + h
                            else "wrong_target"
                        )
                except OcrResolutionRefused as exc:
                    rung = "halt:" + type(exc).__name__
                scored = model.candidates(png, template)
                true_scores = [c.score for c in scored if on_target(c.region)]
                other_scores = [c.score for c in scored if not on_target(c.region)]
                started = time.monotonic()
                tmatch = vision.find_template(
                    png, template, threshold=TEMPLATE_THRESHOLD
                )
                template_ms = (time.monotonic() - started) * 1000.0
                rows.append(
                    {
                        "condition": condition.name,
                        "axis": condition.axis,
                        "target": target,
                        "step_id": step.id,
                        "text_label": step.anchor.ocr_text,
                        "rung_outcome": rung,
                        "lookalikes": len(found),
                        "true_found": any(on_target(c.region) for c in found),
                        "false_found": [
                            list(c.region) for c in found if not on_target(c.region)
                        ],
                        "true_score": round(max(true_scores), 3)
                        if true_scores
                        else None,
                        "best_other_score": (
                            round(max(other_scores), 3) if other_scores else None
                        ),
                        "ms": round(statistics.median(timings), 1),
                        "template_found": bool(
                            tmatch is not None and on_target(tuple(tmatch.region))
                        ),
                        "template_wrong": bool(
                            tmatch is not None and not on_target(tuple(tmatch.region))
                        ),
                        "template_ms": round(template_ms, 1),
                        "frame": list(PixelOnlyBackend(session).viewport),
                    }
                )
                print(json.dumps(rows[-1]), flush=True)
            session.close()
        browser.close()
    ms = [r["ms"] for r in rows]
    payload = {
        "schema": "openadapt.pixel-grounding-probe.v1",
        "commit": _git("rev-parse", "HEAD"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": f"{platform.system()} {platform.machine()} py{platform.python_version()}",
        "grounder": model.describe(),
        "latency_ms": {
            "median": round(statistics.median(ms), 1),
            "p90": round(sorted(ms)[int(0.9 * (len(ms) - 1))], 1),
            "max": round(max(ms), 1),
        },
        "rows": rows,
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps(payload["latency_ms"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
