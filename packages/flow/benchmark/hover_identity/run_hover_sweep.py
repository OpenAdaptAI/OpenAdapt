#!/usr/bin/env python3
"""Hover-before-click evidence on a pixel-only kiosk: before and after.

The appointment kiosk of ``benchmark/pixel_scale`` with its Save button turned
into a toolbar icon. A second icon, ``Clear form``, draws the same glyph; the
two differ only by their tooltip. The kiosk draws a button under the pointer
in its active colour, as Tk does, and shows the icon's tooltip.

The kiosk is recorded once (100% scaling, ClearType on) with the production
Recorder, compiled, and replayed:

- ``healthy``: same layout. A correct run saves one row for Ada Lovelace.
- ``swapped``: the two icons trade places, as a toolbar reorder between
  versions would. At the recorded Save position there is now an identical
  Clear icon. A correct run does not click it.

Each at 100% ClearType on (same render), 100% ClearType off (another
rasterizer) and 125% (display scaling). Identity evidence per click step is
read from the compiled bundle. No model is called.

The same script runs against an older checkout for the before numbers: where
the Recorder cannot capture hover frames, it records without them.

Usage::

    python benchmark/hover_identity/run_hover_sweep.py --output results_after.json
"""

from __future__ import annotations

import argparse
import inspect
import json
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
PIXEL_SCALE = HERE.parent / "pixel_scale"
for _path in (str(HERE.parents[1]), str(PIXEL_SCALE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from kiosk_render import (  # noqa: E402
    KioskBackend,
    RenderProfile,
    ScaledKiosk,
    rdp_appointments_icons_spec,
)

SCHEMA = "openadapt.hover-identity-sweep.v1"
RECORD_PROFILE = RenderProfile(scale=1.0, cleartype=True)
PROFILES = (
    RenderProfile(scale=1.0, cleartype=True),
    RenderProfile(scale=1.0, cleartype=False),
    RenderProfile(scale=1.25, cleartype=True),
)
DEMO = {
    "appointment_slot": "2026-08-12 09:30",
    "visit_type": "Cardiology follow-up",
    "request_id": "REQ-DEMO-001",
}
REPLAY = {
    "appointment_slot": "2026-08-14 10:45",
    "visit_type": "Intake consultation",
    "request_id": "REQ-LIVE-2048",
}
FIELD_FOR_PARAM = {
    "appointment_slot": "slot",
    "visit_type": "visit_type",
    "request_id": "request_id",
}


def _recorder_can_hover() -> bool:
    from openadapt_flow.recorder import Recorder

    return "capture_hover" in inspect.signature(Recorder.__init__).parameters


def _record(work: Path) -> tuple[Any, Path]:
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.recorder import Recorder

    kiosk = ScaledKiosk(
        rdp_appointments_icons_spec(), RECORD_PROFILE, hover_feedback=True
    )
    backend = KioskBackend(kiosk)
    rec_dir, bundle_dir = work / "recording", work / "bundle"
    options = {"capture_hover": True} if _recorder_can_hover() else {}
    rec = Recorder(
        backend,
        rec_dir,
        settle_interval_s=0.0,
        settle_stable_frames=2,
        settle_timeout_s=2.0,
        **options,
    )
    rec.click(*kiosk.logical_center("row0"))
    for param, value in DEMO.items():
        rec.click(*kiosk.logical_center(FIELD_FOR_PARAM[param]))
        rec.type_text(value, param=param)
    rec.click(*kiosk.logical_center("save"))
    rec.finish()
    workflow = compile_recording(rec_dir, bundle_dir, name="hover-identity")
    return workflow, bundle_dir


def coverage(workflow: Any) -> list[dict[str, Any]]:
    """Identity evidence per click step of the compiled bundle."""
    out = []
    for step in workflow.steps:
        if str(getattr(step.action, "value", step.action)) not in (
            "click",
            "double_click",
            "right_click",
        ):
            continue
        a = step.anchor
        record_identity = bool(
            a is not None
            and (
                a.context_text
                or a.structured_identity
                or a.identity_template
                or a.identifier_crop
            )
        )
        hover = getattr(step, "hover", None)
        out.append(
            {
                "step_id": step.id,
                "intent": step.intent,
                "record_identity": record_identity,
                "hover_response": bool(hover is not None and hover.response),
                "has_evidence": record_identity
                or bool(hover is not None and hover.response),
            }
        )
    return out


def _replay(
    bundle_dir: Path, profile: RenderProfile, *, swapped: bool, run_dir: Path
) -> dict[str, Any]:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime.replayer import Replayer

    workflow = Workflow.load(bundle_dir)
    kiosk = ScaledKiosk(
        rdp_appointments_icons_spec(swapped=swapped), profile, hover_feedback=True
    )
    backend = KioskBackend(kiosk)
    crash: Optional[str] = None
    report = None
    try:
        report = Replayer(backend, poll_interval_s=0.0, layout_drift=True).run(
            workflow, params=REPLAY, bundle_dir=bundle_dir, run_dir=run_dir
        )
    except Exception as exc:  # noqa: BLE001 - a crash is an outcome
        crash = f"{type(exc).__name__}: {exc}"[:300]
    clear_rect = kiosk._rect("clear")
    clear_clicked = any(
        clear_rect[0] <= x < clear_rect[0] + clear_rect[2]
        and clear_rect[1] <= y < clear_rect[1] + clear_rect[3]
        for x, y in backend.clicks
    )
    rows = kiosk.state.saved
    expected = {
        "patient_name": "Ada Lovelace",
        "patient_mrn": kiosk.spec.patients[0][1],
        **{FIELD_FOR_PARAM[k]: v for k, v in REPLAY.items()},
    }
    success = bool(report is not None and report.success)
    if crash is not None:
        outcome = "crash"
    elif clear_clicked:
        outcome = "wrong_control_clicked"
    elif rows and rows != [expected]:
        outcome = "wrong_write"
    elif success and rows == [expected]:
        outcome = "pass"
    elif success:
        outcome = "false_completion"
    else:
        outcome = "halted"
    steps = []
    stop = None
    for result in report.results if report is not None else []:
        ident = result.identity
        hover = getattr(ident, "hover", None) if ident is not None else None
        steps.append(
            {
                "step_id": result.step_id,
                "ok": result.ok,
                "rung": result.resolution.rung if result.resolution else None,
                "identity": ident.status if ident is not None else None,
                "identity_mode": ident.mode if ident is not None else None,
                "hover": [hover.verdict, hover.reason] if hover is not None else None,
            }
        )
        if not result.ok and stop is None:
            stop = {"step_id": result.step_id, "error": (result.error or "")[:300]}
    return {
        "layout": "swapped" if swapped else "recorded",
        "profile": profile.label,
        "outcome": outcome,
        "rows_written": len(rows),
        "clear_clicked": clear_clicked,
        "model_calls": int(report.model_calls) if report is not None else 0,
        "stopped_at": stop,
        "steps": steps,
        "crash": crash,
    }


def _git(*args: str) -> Optional[str]:
    try:
        import openadapt_flow

        cwd = Path(openadapt_flow.__file__).resolve().parent
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def run(work: Path) -> dict[str, Any]:
    workflow, bundle_dir = _record(work)
    points = []
    for profile in PROFILES:
        for swapped in (False, True):
            tag = (
                f"{round(profile.scale * 100)}_{'ct' if profile.cleartype else 'noct'}"
            )
            tag += "_swapped" if swapped else ""
            point = _replay(bundle_dir, profile, swapped=swapped, run_dir=work / tag)
            print(
                f"{point['layout']:9s} {point['profile']:24s} {point['outcome']}",
                flush=True,
            )
            points.append(point)
    cov = coverage(workflow)
    return {
        "schema_version": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": _git("rev-parse", "HEAD"),
        "platform": f"{platform.system()} {platform.machine()} py{platform.python_version()}",
        "recorder_captured_hover": _recorder_can_hover(),
        "coverage": {
            "click_steps": len(cov),
            "with_record_identity": sum(c["record_identity"] for c in cov),
            "with_hover_response": sum(c["hover_response"] for c in cov),
            "with_any_evidence": sum(c["has_evidence"] for c in cov),
            "steps": cov,
        },
        "summary": {
            "healthy_points": sum(p["layout"] == "recorded" for p in points),
            "healthy_passed": sum(
                p["layout"] == "recorded" and p["outcome"] == "pass" for p in points
            ),
            "swapped_points": sum(p["layout"] == "swapped" for p in points),
            "swapped_wrong_control_clicked": sum(
                p["layout"] == "swapped" and p["clear_clicked"] for p in points
            ),
            "swapped_stopped_before_click": sum(
                p["layout"] == "swapped"
                and not p["clear_clicked"]
                and p["outcome"] == "halted"
                for p in points
            ),
            "wrong_writes": sum(p["outcome"] == "wrong_write" for p in points),
            "model_calls": sum(p["model_calls"] for p in points),
        },
        "points": points,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="oaflow-hover-"))
    work.mkdir(parents=True, exist_ok=True)
    results = run(work)
    args.output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                **results["summary"],
                **{k: v for k, v in results["coverage"].items() if k != "steps"},
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
