#!/usr/bin/env python3
"""Probe the two safety properties of Flow 1.28.0's remote frame-lease delivery.

openadapt-flow ``c9618cc`` (released in 1.28.0) changed how a *consequential*
remote pointer edge is delivered.  Before it, such a step required
``GuardedRemotePointerActionBackend`` -- the backend that returns an explicit
typed, target-bound delivery receipt -- so every opaque remote surface that can
offer only the documented one-shot actuation lease (the no-DOM HTML5-canvas
class Citrix Workspace-web presents) halted on its write step.  After it, the
runtime delivers through the lease when no typed receipt exists.

Relaxing a refusal is exactly the kind of change that can buy a green
demonstration with a silent wrong write, so the release note's two claims are
measured here rather than trusted:

1. **A governed run still refuses.**  Under a ``GovernedRunAuthorization``
   (Standard/Regulated), a consequential remote click on a backend that cannot
   bind its exact fresh frame and target to delivery must stop *before the first
   input edge*.
2. **The lease-delivered result is never a production success.**  It carries no
   ``actuation`` tier and no ``delivery_receipt``, so Flow's own outcome
   classifier must map it to ``COMPLETED_UNVERIFIED`` and never to ``VERIFIED``,
   under every execution profile.

A third cell measures the property the change relies on: the lease itself.  A
remote frame that changes between ``acquire_actuation_frame`` and the input edge
must abort delivery, otherwise "the lease is the safety property" is not true.

Method matches the sibling runners in this directory: the exact published wheel
is extracted and imported from that extraction, the wheel is bound to
release-tagged tracked-clean source, ``>=3`` trials per cell, no retries, no
model calls, no network access, and every invariant is reported with the
denominator it was evaluated over.  A violated invariant is counted and (unless
``--no-fail-on-violation``) exits non-zero.  It is never silently downgraded.

The backend here is a fake: it implements only the two-phase remote actuation
lease, which is the exact protocol surface a pixel-only canvas backend exposes.
That is deliberate -- the question is what the *runtime* does when a backend can
offer nothing more, and a fake is the only way to hold everything else constant.
This probe therefore measures a runtime contract, not a real remote session.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import platform
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_SIBLING = Path(__file__).resolve().parent / "run_current_flow_local_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("_current_flow_local_benchmark", _SIBLING)
assert _SPEC is not None and _SPEC.loader is not None
_BENCH = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BENCH)
capture_runtime = _BENCH.capture_runtime

# Reuse the sibling's exact source-binding discipline so the reports in one
# evidence set can never drift on how they pin a wheel, a commit, or a tag.
_sha256 = _BENCH._sha256
_source_binding = _BENCH._source_binding
_extract_wheel = _BENCH._extract_wheel
_git = _BENCH._git

VIEWPORT = (300, 200)
CLICK_POINT = (110, 105)
REGION = (100, 100, 50, 20)

#: The only outcome the runtime may treat as a production success.
_PRODUCTION_SUCCESS = "VERIFIED"

#: Every posture the outcome classifier supports. The claim under test is that
#: none of them can turn a receipt-less lease delivery into a production
#: success, so all three are measured rather than only the governed ones.
PROFILES = ("demo", "standard", "regulated")

CELLS = ("ungoverned_lease", "governed_lease", "lease_frame_changed")


def _make_png(
    size: tuple[int, int] = VIEWPORT, color: tuple[int, int, int] = (240, 240, 240)
) -> bytes:
    from PIL import Image  # noqa: PLC0415

    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class _Match:
    """The minimal shape the resolver reads back from a vision call."""

    def __init__(self, point, region, confidence=0.95):
        self.point = point
        self.region = region
        self.confidence = confidence


class _ScriptedVision:
    """Scripted vision namespace covering everything the Replayer touches.

    Nothing here is measured. It exists so the resolution ladder settles on one
    fixed point with no model call and no real screen, leaving the delivery
    decision as the only thing that varies between cells.
    """

    def __init__(self) -> None:
        self.template_results: list[_Match] = []
        self.settle_count = 0

    def find_template(
        self,
        screen_png,
        template_png,
        *,
        search_region=None,
        prefer_near=None,
        scales=(0.85, 1.0, 1.18),
        threshold=0.82,
    ):
        if self.template_results:
            return self.template_results.pop(0)
        return None

    def find_structural_template(
        self,
        screen_png,
        template_png,
        *,
        search_region=None,
        prefer_near=None,
        scales=(0.85, 1.0, 1.18),
        threshold=0.8,
    ):
        return None

    def find_text(self, screen_png, text, *, region=None, min_ratio=0.8, raise_on_ambiguity=False):
        return None

    def text_present(self, screen_png, text, *, region=None, min_ratio=0.8):
        return False

    def ocr(self, screen_png, *, region=None):
        return []

    def pixels_changed(self, before_png, after_png, *, region=None, threshold=20, min_pixels=4):
        return True

    def phash_png(self, png, region=None):
        return "aa"

    def phash_distance(self, a, b):
        return 0

    def wait_settled(self, backend, *, interval_s=0.1, stable_frames=2, timeout_s=3.0):
        self.settle_count += 1
        return backend.screenshot()


class PixelOnlyRemoteBackend:
    """Opaque remote surface exposing ONLY the two-phase actuation lease.

    This is the protocol surface of a no-DOM HTML5-canvas remote backend:
    pixels in, coordinates out, no structural tree, no identity seam, and no
    typed delivery receipt.  ``acquire_actuation_frame`` takes the one-shot
    lease; the next input method consumes it and refuses when the frame content
    changed in between.  That refusal is the safety property the 1.28.0 change
    relies on, so this fake implements it exactly and cell
    ``lease_frame_changed`` exercises it.
    """

    def __init__(self, *, viewport=VIEWPORT, frame_after_lease: bytes | None = None):
        self._frame = _make_png(viewport)
        self._viewport = viewport
        self.actions: list[tuple] = []
        self.prepared_pointer_points: list[tuple[int, int]] = []
        self.acquire_count = 0
        self._leased_frame_sha256: str | None = None
        self.frame_after_lease = frame_after_lease

    @property
    def viewport(self):
        return self._viewport

    def screenshot(self) -> bytes:
        return self._frame

    def prepare_pointer_actuation(self, x, y) -> None:
        self._leased_frame_sha256 = None
        self.prepared_pointer_points.append((int(x), int(y)))

    def acquire_actuation_frame(self) -> bytes:
        self.acquire_count += 1
        self._leased_frame_sha256 = hashlib.sha256(self._frame).hexdigest()
        if self.frame_after_lease is not None:
            self._frame = self.frame_after_lease
        return self._frame

    def _consume_lease(self) -> None:
        leased = self._leased_frame_sha256
        self._leased_frame_sha256 = None
        if leased is None:
            return
        if hashlib.sha256(self._frame).hexdigest() != leased:
            raise RuntimeError("remote frame content changed before the input edge")

    def click(self, x, y, *, double=False) -> None:
        self._consume_lease()
        self.actions.append(("click", int(x), int(y), bool(double)))

    def type_text(self, text) -> None:
        self._consume_lease()
        self.actions.append(("type", text))

    def press(self, key) -> None:
        self._consume_lease()
        self.actions.append(("press", key))

    def scroll(self, dx, dy) -> None:
        self.actions.append(("scroll", dx, dy))


def _run_cell(
    cell: str,
    trial: int,
    root: Path,
    flow: dict[str, Callable[..., Any]],
) -> dict[str, Any]:
    """Run one trial of one cell and record what the runtime did."""

    ActionKind = flow["ActionKind"]
    Anchor = flow["Anchor"]
    Step = flow["Step"]
    Workflow = flow["Workflow"]
    Replayer = flow["Replayer"]
    GovernedRunAuthorization = flow["GovernedRunAuthorization"]
    runtime_inputs_digest = flow["runtime_inputs_digest"]
    stamp_execution_outcome = flow["stamp_execution_outcome"]

    bundle = root / f"{cell}-{trial}" / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(_make_png((50, 20)))
    run_dir = root / f"{cell}-{trial}" / "run"

    step = Step(
        id="s1",
        # The step is IRREVERSIBLE: this is the write, the one edge whose
        # delivery decision the 1.28.0 change alters.
        intent="click 'Save'",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/btn.png",
            region=REGION,
            click_point=CLICK_POINT,
            ocr_text="Save",
            landmarks=[],
        ),
        expect=[],
        risk="irreversible",
    )
    workflow = Workflow(name="remote-lease-probe", steps=[step])

    backend = PixelOnlyRemoteBackend(
        frame_after_lease=(_make_png(color=(10, 20, 30)) if cell == "lease_frame_changed" else None)
    )
    vision = _ScriptedVision()
    vision.template_results = [
        _Match(point=CLICK_POINT, region=REGION),
        _Match(point=CLICK_POINT, region=REGION),
    ]

    authorization = None
    if cell == "governed_lease":
        # A governed run is bound to a sealed bundle, so the workflow must be
        # saved and reloaded to carry a manifest digest.
        workflow.save(bundle)
        workflow = Workflow.load(bundle)
        assert workflow.manifest is not None
        authorization = GovernedRunAuthorization(
            bundle_content_digest=workflow.manifest.content_digest,
            runtime_inputs_digest=runtime_inputs_digest(workflow, None, None),
            admitted_policy_name="remote-lease-safety-probe",
        )

    report = Replayer(
        backend,
        vision=vision,
        governed_authorization=authorization,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)

    result = report.results[0] if report.results else None
    row: dict[str, Any] = {
        "cell": cell,
        "trial": trial,
        "governed": authorization is not None,
        "replayer_success": bool(report.success),
        "input_edges_delivered": len(backend.actions),
        "backend_actions": [list(action) for action in backend.actions],
        "prepared_pointer_points": [list(p) for p in backend.prepared_pointer_points],
        "lease_acquisitions": backend.acquire_count,
        "actuation_tier": getattr(result, "actuation", None) if result else None,
        "delivery_receipt_present": bool(
            getattr(result, "delivery_receipt", None) is not None if result else False
        ),
        "step_error": (getattr(result, "error", None) if result else None),
        "safety_halt": bool(getattr(result, "safety_halt", False)) if result else False,
        "model_calls": int(report.model_calls or 0),
        "model_cost_usd": float(getattr(report, "cost_usd", 0.0) or 0.0),
        "profiles": {},
    }

    # Stamp the SAME report under every profile on independent copies: the
    # classifier mutates the report it is given.
    for profile in PROFILES:
        stamped = copy.deepcopy(report)
        stamp_execution_outcome(stamped, workflow, profile)
        row["profiles"][profile] = {
            "execution_outcome": stamped.execution_outcome,
            "transaction_outcome": stamped.transaction_outcome,
            "transaction_billable": stamped.transaction_billable,
            "production_eligible": bool(stamped.production_eligible),
            "reported_success": bool(stamped.success),
        }
    return row


def _invariant(
    identifier: str,
    statement: str,
    applicable: Callable[[dict[str, Any]], bool],
    violated: Callable[[dict[str, Any]], bool],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    applicable_rows = [row for row in rows if applicable(row)]
    violations = [row for row in applicable_rows if violated(row)]
    return {
        "id": identifier,
        "statement": statement,
        "applicable_count": len(applicable_rows),
        "violation_count": len(violations),
        "holds": not violations,
        "vacuous": not applicable_rows,
        "violations": [
            {
                "cell": row["cell"],
                "trial": row["trial"],
                "input_edges_delivered": row["input_edges_delivered"],
                "profiles": row["profiles"],
            }
            for row in violations
        ],
    }


def evaluate_invariants(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The point of this script. Each invariant states its own denominator."""

    def any_profile(row: dict[str, Any], predicate: Callable[[dict], bool]) -> bool:
        return any(predicate(value) for value in row["profiles"].values())

    return [
        _invariant(
            "governed_run_refuses_receiptless_remote_write",
            "A governed run must refuse a consequential remote click on a "
            "backend that cannot bind its exact fresh frame and target to "
            "delivery, before the first input edge.",
            lambda row: row["governed"],
            lambda row: (
                row["input_edges_delivered"] != 0
                or row["replayer_success"]
                or not row["safety_halt"]
            ),
            rows,
        ),
        _invariant(
            "lease_delivery_is_never_a_production_success",
            "A remote write delivered through the frame lease alone carries no "
            "typed receipt, so no execution profile may classify it VERIFIED or "
            "production-eligible.",
            lambda row: True,
            lambda row: any_profile(
                row,
                lambda value: (
                    value["transaction_outcome"] == _PRODUCTION_SUCCESS
                    or value["execution_outcome"] == _PRODUCTION_SUCCESS
                    or value["production_eligible"]
                ),
            ),
            rows,
        ),
        _invariant(
            "lease_delivery_carries_no_receipt_or_actuation_tier",
            "A lease-only delivery must leave the result unlabeled: no typed "
            "delivery receipt and no recorded actuation tier.",
            lambda row: row["input_edges_delivered"] > 0,
            lambda row: row["delivery_receipt_present"] or row["actuation_tier"] is not None,
            rows,
        ),
        _invariant(
            "changed_frame_aborts_delivery",
            "The lease is the safety property: a remote frame that changed "
            "between the lease and the input edge must stop delivery.",
            lambda row: row["cell"] == "lease_frame_changed",
            lambda row: row["input_edges_delivered"] != 0 or row["replayer_success"],
            rows,
        ),
        _invariant(
            "zero_model_calls_and_cost",
            "A compiled remote replay makes no model calls and costs $0.",
            lambda row: True,
            lambda row: row["model_calls"] != 0 or row["model_cost_usd"] != 0.0,
            rows,
        ),
    ]


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for cell in CELLS:
        cell_rows = [row for row in rows if row["cell"] == cell]
        if not cell_rows:
            continue
        summary[cell] = {
            "n": len(cell_rows),
            "delivered_count": sum(1 for row in cell_rows if row["input_edges_delivered"] > 0),
            "refused_count": sum(1 for row in cell_rows if row["input_edges_delivered"] == 0),
            "replayer_success_count": sum(1 for row in cell_rows if row["replayer_success"]),
            "delivery_receipt_count": sum(
                1 for row in cell_rows if row["delivery_receipt_present"]
            ),
            "actuation_tiers": sorted({str(row["actuation_tier"]) for row in cell_rows}),
            "transaction_outcomes": {
                profile: sorted(
                    {str(row["profiles"][profile]["transaction_outcome"]) for row in cell_rows}
                )
                for profile in PROFILES
            },
            "model_calls_total": sum(row["model_calls"] for row in cell_rows),
            "model_cost_usd_total": sum(row["model_cost_usd"] for row in cell_rows),
        }
    return summary


def _render_report(document: dict[str, Any]) -> str:
    source = document["source"]
    lines = [
        "# Remote frame-lease delivery: the two safety properties, measured",
        "",
        "openadapt-flow `c9618cc` (1.28.0) lets a consequential remote click be",
        "delivered through the backend's one-shot actuation lease when no typed",
        "delivery receipt exists. That relaxes a refusal, so its two stated",
        "safety properties are measured here rather than trusted.",
        "",
        "## Source and environment",
        "",
        f"- Flow commit: `{source['flow']['commit']}` "
        f"(version `{source['flow']['version']}`; tracked-clean source)",
        f"- Release tag: `{source['flow']['release_tag']}`",
        f"- Wheel SHA-256: `{source['flow']['artifact']['sha256']}`",
        f"- Evals base commit: `{source['evals']['commit']}`",
        f"- Runner SHA-256: `{source['runner_sha256']}`",
        f"- Platform: `{document['environment']['platform']}`",
        f"- Python: `{document['environment']['python']}`",
        "- Network/provider use: none. No server, no browser, no model API.",
        "",
        "## Counted result",
        "",
        f"{document['trials_per_cell']} trials per cell. "
        "`input edges` counts what the backend was actually asked to deliver.",
        "",
        "| Cell | Runs | Delivered | Refused | Receipts | Actuation tier | "
        "`transaction_outcome` (demo / standard / regulated) |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for cell, values in document["aggregate"].items():
        outcomes = " / ".join(
            ", ".join(values["transaction_outcomes"][profile]) for profile in PROFILES
        )
        lines.append(
            f"| `{cell}` | {values['n']} | {values['delivered_count']} | "
            f"{values['refused_count']} | {values['delivery_receipt_count']} | "
            f"{', '.join(values['actuation_tiers'])} | {outcomes} |"
        )
    lines += [
        "",
        "## Invariants",
        "",
        "Every invariant states the denominator it was evaluated over. A "
        "`vacuous` invariant had no applicable run and proves nothing.",
        "",
        "| Invariant | Applicable runs | Violations | Holds |",
        "|---|---:|---:|---|",
    ]
    for invariant in document["invariants"]:
        verdict = "vacuous" if invariant["vacuous"] else ("yes" if invariant["holds"] else "**NO**")
        lines.append(
            f"| {invariant['statement']} | {invariant['applicable_count']} | "
            f"{invariant['violation_count']} | {verdict} |"
        )
    lines += [
        "",
        "## Scope",
        "",
        "- The backend is a fake implementing ONLY the two-phase remote",
        "  actuation lease, which is the exact protocol surface a pixel-only",
        "  no-DOM canvas backend exposes. This measures a runtime contract, not",
        "  a real Citrix or RDP session.",
        "- One synthetic single-step workflow whose only step is the",
        "  irreversible write. Resolution is scripted to one fixed point so the",
        "  delivery decision is the only thing that varies between cells.",
        "- No claim is made here about wrong-target immunity on a real remote",
        "  surface, about identity coverage, or about any hosted lifecycle.",
        "",
        f"Reproduce: `{document['reproduce']}`",
        "",
    ]
    return "\n".join(lines)


def probe(flow_source: Path, flow_wheel: Path, out_dir: Path, *, trials: int = 3) -> dict[str, Any]:
    if trials < 3:
        raise ValueError("comparative evaluation requires at least three trials")
    flow_source = flow_source.resolve()
    flow_wheel = flow_wheel.resolve()
    out_dir = out_dir.resolve()
    if not (flow_source / "openadapt_flow").is_dir():
        raise ValueError(f"not an openadapt-flow source checkout: {flow_source}")
    if not flow_wheel.is_file() or flow_wheel.suffix != ".whl":
        raise ValueError(f"not a Flow wheel: {flow_wheel}")

    source = _source_binding(flow_source)
    artifact_temp = tempfile.TemporaryDirectory(prefix="oa-flow-wheel-")
    artifact_root = Path(artifact_temp.name).resolve()
    _extract_wheel(flow_wheel, artifact_root)
    sys.path.insert(0, str(artifact_root))

    from openadapt_flow.execution_profiles import stamp_execution_outcome  # noqa: PLC0415
    from openadapt_flow.ir import (  # noqa: PLC0415
        ActionKind,
        Anchor,
        Step,
        Workflow,
    )
    from openadapt_flow.runtime.authorization import (  # noqa: PLC0415
        GovernedRunAuthorization,
        runtime_inputs_digest,
    )
    from openadapt_flow.runtime.replayer import Replayer  # noqa: PLC0415

    imported = Path(sys.modules["openadapt_flow"].__file__).resolve()
    if artifact_root not in imported.parents:
        raise RuntimeError(f"Flow import escaped requested wheel: {imported}")
    flow_version = str(sys.modules["openadapt_flow"].__version__)
    release_tag = f"v{flow_version}"
    if release_tag not in source["tags"]:
        raise RuntimeError(f"source commit is not tagged {release_tag}: {source['tags']}")

    flow = {
        "ActionKind": ActionKind,
        "Anchor": Anchor,
        "Step": Step,
        "Workflow": Workflow,
        "Replayer": Replayer,
        "GovernedRunAuthorization": GovernedRunAuthorization,
        "runtime_inputs_digest": runtime_inputs_digest,
        "stamp_execution_outcome": stamp_execution_outcome,
    }

    evals_root = Path(__file__).resolve().parents[1]
    evals_commit = _git(evals_root, "rev-parse", "HEAD")
    out_dir.mkdir(parents=True, exist_ok=False)

    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="oa-remote-lease-probe-") as tmp:
        root = Path(tmp)
        for cell in CELLS:
            for trial in range(1, trials + 1):
                row = _run_cell(cell, trial, root, flow)
                rows.append(row)
                print(
                    f"{cell} trial={trial}: delivered={row['input_edges_delivered']} "
                    f"success={row['replayer_success']} "
                    f"actuation={row['actuation_tier']} "
                    f"receipt={row['delivery_receipt_present']} "
                    f"standard={row['profiles']['standard']['transaction_outcome']}"
                )

    document = {
        "schema_version": 1,
        "scope": "remote frame-lease delivery safety properties (runtime contract)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trials_per_cell": trials,
        "cells": list(CELLS),
        "profiles": list(PROFILES),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "runtime": capture_runtime(out_dir, browser_required=False),
        "source": {
            "evals": {"commit": evals_commit},
            "flow": {
                "commit": source["commit"],
                "tags": source["tags"],
                "tracked_clean": source["tracked_clean"],
                "version": flow_version,
                "release_tag": release_tag,
                "artifact": {
                    "filename": flow_wheel.name,
                    "sha256": _sha256(flow_wheel),
                    "import_mode": "locally extracted published wheel",
                },
            },
            "runner_sha256": _sha256(Path(__file__).resolve()),
        },
        "paid_or_remote_mutations": [],
        "runs": rows,
        "aggregate": _aggregate(rows),
        "invariants": evaluate_invariants(rows),
        "caveats": [
            "The backend is a fake implementing only the two-phase remote "
            "actuation lease; this measures a runtime contract, not a real "
            "remote session.",
            "One synthetic single-step workflow; resolution is scripted so the "
            "delivery decision is the only variable.",
            "No wrong-target immunity, identity coverage, or hosted lifecycle claim is made here.",
        ],
        "reproduce": (
            "python scripts/probe_remote_lease_safety.py "
            f"--flow-source <v{flow_version}-checkout> "
            f"--flow-wheel <{flow_wheel.name}> "
            "--out <new-output-directory>"
        ),
    }
    (out_dir / "results.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "REPORT.md").write_text(_render_report(document), encoding="utf-8")
    print(f"Wrote {out_dir / 'results.json'} and {out_dir / 'REPORT.md'}")
    artifact_temp.cleanup()
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure the governed refusal and the never-VERIFIED property of "
            "Flow's remote frame-lease delivery."
        )
    )
    parser.add_argument("--flow-source", type=Path, required=True)
    parser.add_argument("--flow-wheel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument(
        "--no-fail-on-violation",
        action="store_true",
        help="Write the artifacts and exit 0 even when an invariant is violated.",
    )
    args = parser.parse_args(argv)
    document = probe(args.flow_source, args.flow_wheel, args.out, trials=args.trials)
    violated = [item for item in document["invariants"] if not item["holds"]]
    for item in violated:
        print(
            f"VIOLATED: {item['id']}: {item['violation_count']} of "
            f"{item['applicable_count']} applicable runs",
            file=sys.stderr,
        )
    if violated and not args.no_fail_on_violation:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
