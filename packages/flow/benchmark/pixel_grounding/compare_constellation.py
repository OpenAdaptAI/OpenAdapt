#!/usr/bin/env python3
"""Compare Arm A (LocalGroundingModel) vs Arm B (ConstellationGrounder).

Evaluates both arms on two existing OpenAdapt benchmark suites:

1. ``pixel_grounding`` EMR fixture (13 rendering conditions x 5 targets = 65
   cases: 2 icons, 2 blank input fields, 1 row action button).
   - Arm A: existing ``LocalGroundingModel`` + ``resolver._model_grounded``.
   - Arm B (constellation only): ``ConstellationGrounder`` captured once on the
     100% light-theme recording frame.
   - Arm B (combined): Arm A first, falling back to ``ConstellationGrounder``
     when Arm A abstains.

2. ``vision_hardening`` OCR look-alike and safety fixtures
   (``labeled_rows``, ``duplicate_buttons``, ``mrn_rows``) across rendering
   perturbations (identity, scale 0.8x/1.5x/2.0x, theme inversion, JPEG Q50):
   - Arm A: existing ``resolver.resolve`` (template + OCR + geometry).
   - Arm B: Arm A with ``ConstellationGrounder`` fallback when Arm A abstains.

Writes compact JSON results to ``--output``.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import platform
import statistics
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
for _path in (str(HERE.parents[1]), str(HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fixture_backend import FixtureSession, PixelOnlyBackend  # noqa: E402
from run_sweep import (  # noqa: E402
    ADA,
    CONDITIONS,
    DEMO,
    RECORD,
    _center,
    _git,
    _row_box,
)

TARGETS = ("edit-2", "visit", "note", "tb-attach", "save")


def _record_emr(browser: Any, work: Path) -> tuple[Any, Path]:
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
        encryption_key=None,
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
    path = rec_dir / "events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines() if line]
    regions = {0: row, 1: banner, 3: banner, 5: banner, 6: banner}
    for index, region in regions.items():
        events[index]["identifier_region"] = list(region)
    path.write_text(
        "".join(json.dumps(e, sort_keys=True) + "\n" for e in events),
        encoding="utf-8",
    )
    workflow = compile_recording(rec_dir, bundle_dir, name="pixel-grounding-emr")
    return workflow, bundle_dir


def _encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _perturb_hardening_frame(png: bytes, name: str) -> tuple[bytes, float]:
    """Return ``(perturbed_png, scale_factor)`` for a hardening fixture."""
    if name == "identity":
        return png, 1.0
    img = Image.open(io.BytesIO(png)).convert("RGB")
    if name.startswith("scale_"):
        scale = float(name.split("_", 1)[1])
        w, h = img.size
        resized = img.resize(
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            Image.Resampling.BICUBIC,
        )
        return _encode_png(resized), scale
    if name == "theme_invert":
        return _encode_png(ImageOps.invert(img)), 1.0
    if name == "jpeg_q50":
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=50)
        decoded = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
        return _encode_png(decoded), 1.0
    raise ValueError(f"unknown perturbation: {name}")


def _run_pixel_grounding_suite() -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    from openadapt_flow import vision
    from openadapt_flow.runtime.constellation_grounder import (
        ConstellationGrounder,
        capture_constellation,
    )
    from openadapt_flow.runtime.local_grounder import LocalGroundingModel
    from openadapt_flow.runtime.resolver import _model_grounded
    from openadapt_flow.vision.ocr import OcrResolutionRefused

    model = LocalGroundingModel.load()
    rows: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
        browser = pw.chromium.launch()
        workflow, bundle_dir = _record_emr(browser, Path(tmp))
        clicks = [s for s in workflow.steps if s.anchor is not None]
        assert len(clicks) == len(TARGETS)

        # Capture constellations once on the 100% light-theme recording frame.
        rec_session = FixtureSession(browser, RECORD)
        rec_session.select_patient(ADA)
        rec_png = PixelOnlyBackend(rec_session).screenshot()
        constellations = {}
        for step, target in zip(clicks, TARGETS):
            box = rec_session.element_box(target)
            constellations[target] = capture_constellation(
                rec_png,
                box,
                intent=target,
            )
        rec_session.close()

        cg = ConstellationGrounder(constellations)

        for condition in CONDITIONS:
            session = FixtureSession(browser, condition)
            session.select_patient(ADA)
            png = PixelOnlyBackend(session).screenshot()

            for step, target in zip(clicks, TARGETS):
                from openadapt_flow import sealed_set as _sealed

                template = _sealed.read_bytes(bundle_dir / step.anchor.template)
                x, y, w, h = session.element_box(target)

                def _classify_point(
                    px: int, py: int, box: tuple[int, int, int, int] = (x, y, w, h)
                ) -> str:
                    return (
                        "hit"
                        if box[0] <= px <= box[0] + box[2]
                        and box[1] <= py <= box[1] + box[3]
                        else "wrong_click"
                    )

                # Arm A: LocalGroundingModel + _model_grounded
                t0 = time.monotonic()
                try:
                    grounded = _model_grounded(
                        step.anchor, png, vision, model.locate_anchor, template
                    )
                    if grounded is None:
                        arm_a = "abstain"
                    else:
                        arm_a = _classify_point(grounded[0][0], grounded[0][1])
                except OcrResolutionRefused:
                    arm_a = "abstain"
                arm_a_ms = (time.monotonic() - t0) * 1000.0

                # Arm B (constellation standalone)
                t1 = time.monotonic()
                cmatch = cg.locate(png, target, ocr_text=None)
                arm_b_only_ms = (time.monotonic() - t1) * 1000.0
                if cmatch is None:
                    arm_b_only = "abstain"
                else:
                    arm_b_only = _classify_point(cmatch.point[0], cmatch.point[1])

                # Arm B (combined: Arm A first, ConstellationGrounder fallback on abstain)
                if arm_a != "abstain":
                    arm_b_combined = arm_a
                else:
                    arm_b_combined = arm_b_only

                rows.append(
                    {
                        "condition": condition.name,
                        "axis": condition.axis,
                        "target": target,
                        "arm_a": arm_a,
                        "arm_b_constellation_only": arm_b_only,
                        "arm_b_combined": arm_b_combined,
                        "arm_a_ms": round(arm_a_ms, 1),
                        "arm_b_only_ms": round(arm_b_only_ms, 1),
                    }
                )
            session.close()
        browser.close()

    def _tally(subset: list[dict[str, Any]], key: str) -> dict[str, int]:
        counts = Counter(r[key] for r in subset)
        return {
            "hits": counts.get("hit", 0),
            "abstentions": counts.get("abstain", 0),
            "wrong_clicks": counts.get("wrong_click", 0),
            "total": len(subset),
        }

    blank_rows = [r for r in rows if r["target"] in ("visit", "note")]
    icon_rows = [r for r in rows if r["target"] in ("tb-attach", "save")]
    row_btn_rows = [r for r in rows if r["target"] == "edit-2"]

    return {
        "total_cases": len(rows),
        "all_targets": {
            "arm_a_local_model": _tally(rows, "arm_a"),
            "arm_b_constellation_only": _tally(rows, "arm_b_constellation_only"),
            "arm_b_combined": _tally(rows, "arm_b_combined"),
        },
        "blank_fields": {
            "arm_a_local_model": _tally(blank_rows, "arm_a"),
            "arm_b_constellation_only": _tally(blank_rows, "arm_b_constellation_only"),
            "arm_b_combined": _tally(blank_rows, "arm_b_combined"),
        },
        "icons": {
            "arm_a_local_model": _tally(icon_rows, "arm_a"),
            "arm_b_constellation_only": _tally(icon_rows, "arm_b_constellation_only"),
            "arm_b_combined": _tally(icon_rows, "arm_b_combined"),
        },
        "row_action_buttons": {
            "arm_a_local_model": _tally(row_btn_rows, "arm_a"),
            "arm_b_constellation_only": _tally(
                row_btn_rows, "arm_b_constellation_only"
            ),
            "arm_b_combined": _tally(row_btn_rows, "arm_b_combined"),
        },
        "median_ms": {
            "arm_a": round(statistics.median(r["arm_a_ms"] for r in rows), 1),
            "arm_b_only": round(statistics.median(r["arm_b_only_ms"] for r in rows), 1),
        },
        "rows": rows,
    }


def _run_hardening_lookalike_suite() -> dict[str, Any]:
    from openadapt_flow.runtime.constellation_grounder import (
        ConstellationGrounder,
        capture_constellation,
    )
    from openadapt_flow.runtime.resolver import resolve
    from openadapt_flow.validation.hardening import fixtures as fx

    if not fx.ocr_available():
        return {"skipped": True, "reason": "OCR unavailable"}

    perturbations = (
        "identity",
        "scale_0.8",
        "scale_1.5",
        "scale_2.0",
        "theme_invert",
        "jpeg_q50",
    )
    lookalike_fixtures = [
        fx.labeled_rows(n=4, true_idx=0, label="Delete"),
        fx.labeled_rows(n=4, true_idx=2, label="Delete"),
        fx.labeled_rows(n=6, true_idx=2, label="Delete"),
        fx.labeled_rows(n=6, true_idx=5, label="Delete"),
    ]
    safety_fixtures = [
        fx.duplicate_buttons(label="Save", gap=200),
        fx.mrn_rows(collapse="O0", true_idx=0),
        fx.mrn_rows(collapse="l1", true_idx=1),
    ]

    def _eval_point(
        point: tuple[int, int] | None,
        fixture: fx.Fixture,
        scale: float,
    ) -> str:
        if point is None:
            return "abstain"
        tx = fixture.true_center[0] * scale
        ty = fixture.true_center[1] * scale
        tol = fixture.tolerance_px * scale
        if math.hypot(point[0] - tx, point[1] - ty) <= tol:
            return "hit"
        return "wrong_click"

    def _run_group(group: list[fx.Fixture]) -> dict[str, Any]:
        arm_a_counts: Counter[str] = Counter()
        arm_b_counts: Counter[str] = Counter()
        arm_b_only_counts: Counter[str] = Counter()
        details: list[dict[str, Any]] = []

        for fixture in group:
            assert fixture.anchor.region is not None
            constellation = capture_constellation(
                fixture.clean_png,
                fixture.anchor.region,
                target_point=fixture.true_center,
                label=fixture.label,
                intent=fixture.key(),
            )
            cg = ConstellationGrounder({fixture.key(): constellation})

            with tempfile.TemporaryDirectory() as tmp:
                bdir = Path(tmp)
                (bdir / fixture.anchor.template).write_bytes(fixture.template_png)

                for pert in perturbations:
                    frame_png, scale = _perturb_hardening_frame(fixture.clean_png, pert)

                    # Arm A: existing resolver (no constellation grounder)
                    try:
                        res_a = resolve(
                            fixture.anchor,
                            frame_png,
                            bdir,
                            intent=fixture.key(),
                        )
                        out_a = _eval_point(
                            res_a.point if res_a is not None else None, fixture, scale
                        )
                    except Exception:  # noqa: BLE001
                        out_a = "abstain"

                    # Arm B standalone: ConstellationGrounder only
                    match_b = cg.locate(
                        frame_png, fixture.key(), ocr_text=fixture.label
                    )
                    out_b_only = _eval_point(
                        match_b.point if match_b is not None else None, fixture, scale
                    )

                    # Arm B combined: Arm A with ConstellationGrounder fallback
                    out_b = out_a if out_a != "abstain" else out_b_only

                    arm_a_counts[out_a] += 1
                    arm_b_only_counts[out_b_only] += 1
                    arm_b_counts[out_b] += 1
                    details.append(
                        {
                            "fixture": fixture.key(),
                            "perturbation": pert,
                            "arm_a": out_a,
                            "arm_b_constellation_only": out_b_only,
                            "arm_b_combined": out_b,
                        }
                    )

        def _fmt(c: Counter[str]) -> dict[str, int]:
            return {
                "hits": c.get("hit", 0),
                "abstentions": c.get("abstain", 0),
                "wrong_clicks": c.get("wrong_click", 0),
                "total": sum(c.values()),
            }

        return {
            "arm_a_resolver": _fmt(arm_a_counts),
            "arm_b_constellation_only": _fmt(arm_b_only_counts),
            "arm_b_combined": _fmt(arm_b_counts),
            "cases": details,
        }

    return {
        "labeled_rows_lookalikes": _run_group(lookalike_fixtures),
        "ambiguous_and_mrn_safety": _run_group(safety_fixtures),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    pixel = _run_pixel_grounding_suite()
    hardening = _run_hardening_lookalike_suite()
    payload = {
        "schema": "openadapt.constellation-comparison.v1",
        "commit": _git("rev-parse", "HEAD"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": f"{platform.system()} {platform.machine()} py{platform.python_version()}",
        "pixel_grounding": pixel,
        "vision_hardening": hardening,
    }
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    summary = {
        "pixel_grounding_all": pixel["all_targets"],
        "pixel_grounding_blank_fields": pixel["blank_fields"],
        "pixel_grounding_row_buttons": pixel["row_action_buttons"],
        "pixel_grounding_icons": pixel["icons"],
        "pixel_grounding_median_ms": pixel["median_ms"],
        "hardening_labeled_rows": {
            k: v
            for k, v in hardening["labeled_rows_lookalikes"].items()
            if k != "cases"
        },
        "hardening_safety": {
            k: v
            for k, v in hardening["ambiguous_and_mrn_safety"].items()
            if k != "cases"
        },
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
