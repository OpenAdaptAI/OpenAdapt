"""Cosmetic-drift operating-envelope sweep.

Records + compiles ONE canonical MockMed triage bundle, then replays that same
bundle under a sweep of *cosmetic-only* perturbations (browser zoom, device
pixel ratio / DPI, font-size scaling, font-family substitution, and the most
realistic pairs). The target is always present and semantically identical --
only rendering changes -- so a correct run always ends by saving the encounter
to patient ``p1`` (Jane Sample). Any save to a different patient is a
WRONG-ACTION (the dangerous class this study is built to detect).

Perturbations are applied WITHOUT touching MockMed: a ``<style>`` tag injected
into ``<head>`` after navigation survives MockMed's hash-router re-renders
(rules match by selector, not inline), and ``device_scale_factor`` is set when
the page is created. CSS ``zoom`` is the same model MockMed's own bundled
``drift=zoom`` mode uses.

Outputs (written under ``benchmark/cosmetic_drift/``):

- ``results.json``  -- machine-readable matrix (one row per perturbation).
- ``results.md``    -- the human-readable outcome x rung matrix.

No Anthropic / model calls are made: the grounder rung is never installed, so
resolution is template + OCR + geometry only.

Usage::

    python -m benchmark.cosmetic_drift.sweep            # full sweep
    python -m benchmark.cosmetic_drift.sweep --quick    # smoke subset
    python -m benchmark.cosmetic_drift.sweep --only zoom,dpi
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.compiler import compile_recording
from openadapt_flow.demo_driver import record_triage_demo
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.mockmed.server import serve
from openadapt_flow.runtime import Replayer

HERE = Path(__file__).resolve().parent

NOTE_TEXT = "E2E triage booking three months"
PARAMS = {"note": NOTE_TEXT}
VIEWPORT = {"width": 1280, "height": 800}

# The correct outcome: the demo opens the FIRST referral (patient p1, Jane
# Sample) and saves an encounter, landing on #patient/p1 with a saved banner.
TARGET_HASH = "#patient/p1"

# Base font-size (px) of MockMed elements, read from styles.css. A font-size
# perturbation scales every one of these by ``fontscale`` via an injected
# head <style>, reflowing text exactly as a user-side font preference would.
BASE_FONT_PX = {
    "html, body": 16,
    "p": 16,
    "button": 16,
    "input": 16,
    "textarea": 16,
    "select": 16,
    "label": 16,
    "td, th": 15,
    ".seg-btn": 16,
    "h1": 24,
    "h2": 20,
    "#topbar": 20,
    "#subtitle": 14,
    "#patient-banner": 16,
    "#saved-banner": 17,
}

FONT_FAMILY_SELECTORS = (
    "html, body, button, input, textarea, select, p, label, td, th, "
    "h1, h2, #topbar, #subtitle, #patient-banner, #saved-banner, .seg-btn"
)


@dataclass
class Perturb:
    """One point in the cosmetic-drift sweep."""

    axis: str
    label: str
    zoom: float = 1.0
    dsf: float = 1.0
    fontscale: float = 1.0
    fontfamily: Optional[str] = None

    def css(self) -> str:
        """Head <style> content that realizes this perturbation (or "")."""
        parts: list[str] = []
        if abs(self.zoom - 1.0) > 1e-9:
            parts.append(f"body {{ zoom: {self.zoom}; }}")
        if abs(self.fontscale - 1.0) > 1e-9:
            rules = [
                f"{sel} {{ font-size: {round(base * self.fontscale)}px"
                f" !important; }}"
                for sel, base in BASE_FONT_PX.items()
            ]
            parts.append("\n".join(rules))
        if self.fontfamily:
            parts.append(
                f"{FONT_FAMILY_SELECTORS} {{ font-family: {self.fontfamily}"
                " !important; }"
            )
        return "\n".join(parts)


def build_matrix(only: Optional[set[str]], quick: bool) -> list[Perturb]:
    """Assemble the perturbation matrix, optionally filtered by axis."""
    points: list[Perturb] = [
        Perturb("baseline", "baseline (100%, 1x, default font)"),
    ]
    # Browser zoom / page scale.
    for pct in (80, 90, 110, 125, 133, 150, 175, 200):
        points.append(Perturb("zoom", f"zoom {pct}%", zoom=pct / 100.0))
    # Device-scale-factor / DPI (dsf=1 is the baseline already).
    for dsf in (1.5, 2.0, 3.0):
        points.append(Perturb("dpi", f"DPI {dsf}x", dsf=dsf))
    # Font-size scaling (1.1875 == the 16->19px bump MockMed's drift=font uses).
    for scale, note in ((1.10, "+10%"), (1.1875, "+19% (19px)"), (1.375, "+37%")):
        points.append(
            Perturb("fontsize", f"font-size {note}", fontscale=scale)
        )
    # Font-family substitution (>= 2 families; all commonly installed).
    for fam, name in (
        ("Georgia, serif", "Georgia (serif)"),
        ('"Times New Roman", serif', "Times New Roman (serif)"),
        ('"Courier New", monospace', "Courier New (monospace)"),
    ):
        points.append(Perturb("fontfamily", f"font {name}", fontfamily=fam))
    # Most-realistic combinations (varied-hardware back office).
    points.append(
        Perturb("combo", "zoom 125% + DPI 2x", zoom=1.25, dsf=2.0)
    )
    points.append(
        Perturb("combo", "zoom 133% + DPI 1.5x", zoom=1.33, dsf=1.5)
    )
    points.append(
        Perturb(
            "combo",
            "zoom 110% + font +19% + Georgia",
            zoom=1.10,
            fontscale=1.1875,
            fontfamily="Georgia, serif",
        )
    )

    if only:
        points = [p for p in points if p.axis in only or p.axis == "baseline"]
    if quick:
        # One representative per axis for a fast validation pass.
        subset: list[Perturb] = []
        for p in points:
            if p.axis == "baseline":
                subset.append(p)
            elif p.axis == "zoom" and p.label == "zoom 125%":
                subset.append(p)
            elif p.axis == "dpi" and p.label == "DPI 2x":
                subset.append(p)
            elif p.axis == "fontsize" and "19px" in p.label:
                subset.append(p)
            elif p.axis == "fontfamily" and "Georgia" in p.label:
                subset.append(p)
            elif p.axis == "combo" and p.label == "zoom 125% + DPI 2x":
                subset.append(p)
        points = subset
    return points


@dataclass
class StepObs:
    step_id: str
    intent: str
    ok: bool
    rung: Optional[str]
    postconditions_ok: Optional[bool]
    identity_status: Optional[str]
    heal: bool
    error: Optional[str]


@dataclass
class Row:
    axis: str
    label: str
    zoom: float
    dsf: float
    fontscale: float
    fontfamily: Optional[str]
    outcome: str
    safe: bool
    success: bool
    saved_banner: Optional[str]
    final_hash: Optional[str]
    heal_count: int
    rung_counts: dict
    failed_step: Optional[str]
    failed_reason: Optional[str]
    last_rung: Optional[str]
    steps_completed: int
    total_steps: int
    elapsed_s: float
    steps: list = field(default_factory=list)


def read_state(page) -> dict:
    return {
        "hash": page.evaluate("location.hash"),
        "banner": page.evaluate(
            "(document.getElementById('saved-banner') || {}).textContent"
            " || null"
        ),
    }


def classify(report: Optional[RunReport], state: dict) -> tuple[str, bool]:
    """Return ``(outcome, safe)``.

    Outcomes:
      pass         -- ran to completion, saved to the correct patient (p1).
      safe-halt    -- stopped without saving anything (no side effect).
      wrong-action -- saved (or reported success landing) on the WRONG patient.
      crash        -- the replayer raised instead of returning a report.

    ``safe`` is True for pass / safe-halt (no wrong write), False for
    wrong-action / crash.
    """
    if report is None:
        return "crash", False
    saved = state.get("banner") is not None
    right = state.get("hash") == TARGET_HASH
    if saved and not right:
        return "wrong-action", False
    if report.success and not right:
        return "wrong-action", False
    if report.success and right:
        return "pass", True
    if saved and right:
        # Save landed on the correct patient even if a trailing postcondition
        # later flagged -- no wrong write occurred.
        return "pass", True
    return "safe-halt", True


def replay_perturbed(
    browser, bundle_dir: Path, base_url: str, run_dir: Path, p: Perturb
) -> Row:
    run_dir.mkdir(parents=True, exist_ok=True)
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=p.dsf)
    report: Optional[RunReport] = None
    crash_msg: Optional[str] = None
    t0 = time.monotonic()
    try:
        page.goto(base_url)
        css = p.css()
        if css:
            page.add_style_tag(content=css)
            page.wait_for_timeout(80)  # let the reflow settle
        backend = PlaywrightBackend(page)
        workflow = Workflow.load(bundle_dir)
        try:
            report = Replayer(backend).run(
                workflow,
                params=dict(PARAMS),
                bundle_dir=Path(bundle_dir),
                run_dir=run_dir,
            )
        except Exception as exc:  # pragma: no cover - defensive
            crash_msg = f"{type(exc).__name__}: {exc}"
        state = read_state(page)
    finally:
        page.close()
    elapsed = time.monotonic() - t0

    outcome, safe = classify(report, state)
    steps: list[StepObs] = []
    failed_step = None
    failed_reason = None
    last_rung = None
    steps_completed = 0
    if report is not None:
        for r in report.results:
            rung = r.resolution.rung if r.resolution else None
            if rung:
                last_rung = rung
            if r.ok:
                steps_completed += 1
            steps.append(
                StepObs(
                    step_id=r.step_id,
                    intent=r.intent,
                    ok=r.ok,
                    rung=rung,
                    postconditions_ok=r.postconditions_ok,
                    identity_status=(
                        r.identity.status if r.identity else None
                    ),
                    heal=r.heal is not None,
                    error=r.error,
                )
            )
            if not r.ok and failed_step is None:
                failed_step = r.step_id
                failed_reason = r.error

    return Row(
        axis=p.axis,
        label=p.label,
        zoom=p.zoom,
        dsf=p.dsf,
        fontscale=p.fontscale,
        fontfamily=p.fontfamily,
        outcome=outcome,
        safe=safe,
        success=bool(report.success) if report else False,
        saved_banner=state.get("banner"),
        final_hash=state.get("hash"),
        heal_count=report.heal_count if report else 0,
        rung_counts=dict(report.rung_counts) if report else {},
        failed_step=failed_step,
        failed_reason=crash_msg or failed_reason,
        last_rung=last_rung,
        steps_completed=steps_completed,
        total_steps=len(report.results) if report else 0,
        elapsed_s=round(elapsed, 1),
        steps=[asdict(s) for s in steps],
    )


def render_markdown(rows: list[Row], meta: dict) -> str:
    lines: list[str] = []
    lines.append("# Cosmetic-drift sweep -- results matrix")
    lines.append("")
    lines.append(f"Generated: {meta['generated_at']}  ")
    lines.append(f"Platform: {meta['platform']}  ")
    lines.append(
        f"Bundle: {meta['total_steps']} steps, recorded at "
        f"{VIEWPORT['width']}x{VIEWPORT['height']} dsf=1.  "
    )
    lines.append(
        f"Template scale ladder: {meta['template_scales']}; "
        f"template threshold {meta['template_threshold']}.  "
    )
    lines.append("")
    lines.append(
        "| axis | perturbation | outcome | SAFE? | steps ok | failed step |"
        " last rung | heals | rungs used |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        safe = "safe" if r.safe else "**DANGER**"
        rungs = ", ".join(f"{k}:{v}" for k, v in sorted(r.rung_counts.items()))
        lines.append(
            f"| {r.axis} | {r.label} | {r.outcome} | {safe} | "
            f"{r.steps_completed}/{r.total_steps} | {r.failed_step or '-'} | "
            f"{r.last_rung or '-'} | {r.heal_count} | {rungs or '-'} |"
        )
    lines.append("")
    danger = [r for r in rows if not r.safe]
    lines.append(
        f"**Wrong-actions / crashes: {len(danger)} of {len(rows)} points.**"
    )
    if danger:
        for r in danger:
            lines.append(f"- {r.label}: {r.outcome} (hash={r.final_hash})")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true", help="fast subset")
    ap.add_argument(
        "--only",
        default="",
        help="comma-separated axes (zoom,dpi,fontsize,fontfamily,combo)",
    )
    ap.add_argument("--out", default=str(HERE), help="output directory")
    args = ap.parse_args()

    only = {a for a in args.only.split(",") if a} or None
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = out_dir / "runs"

    import inspect

    from openadapt_flow.runtime.resolver import TEMPLATE_THRESHOLD
    from openadapt_flow.vision.match import find_template as _ft

    scales = inspect.signature(_ft).parameters["scales"].default

    matrix = build_matrix(only, args.quick)

    from playwright.sync_api import sync_playwright

    url, stop = serve(port=0)
    rows: list[Row] = []
    try:
        # Record + compile the canonical bundle ONCE.
        rec_dir = out_dir / "_recording"
        bundle_dir = out_dir / "_bundle"
        print(f"[setup] recording demo against {url}")
        recording = record_triage_demo(url, rec_dir, note_text=NOTE_TEXT)
        print(f"[setup] compiling bundle -> {bundle_dir}")
        workflow = compile_recording(recording, bundle_dir, name="triage-demo")
        total_steps = len(workflow.steps)
        print(f"[setup] bundle has {total_steps} steps")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for i, p in enumerate(matrix):
                    run_dir = runs_dir / f"{i:02d}_{p.axis}"
                    print(
                        f"[{i + 1}/{len(matrix)}] {p.label} ...",
                        end="",
                        flush=True,
                    )
                    row = replay_perturbed(
                        browser, bundle_dir, url, run_dir, p
                    )
                    flag = "" if row.safe else "  <<< NOT SAFE"
                    print(
                        f" {row.outcome} ({row.steps_completed}/"
                        f"{row.total_steps}, {row.elapsed_s}s){flag}"
                    )
                    rows.append(row)
            finally:
                browser.close()
    finally:
        stop()

    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": _platform_str(),
        "total_steps": rows[0].total_steps if rows else 0,
        "template_scales": list(scales),
        "template_threshold": TEMPLATE_THRESHOLD,
        "note_text": NOTE_TEXT,
        "target_hash": TARGET_HASH,
    }
    doc = {"meta": meta, "rows": [asdict(r) for r in rows]}
    (out_dir / "results.json").write_text(json.dumps(doc, indent=2))
    (out_dir / "results.md").write_text(render_markdown(rows, meta))

    danger = [r for r in rows if not r.safe]
    print(f"\n[done] {len(rows)} points; wrong-actions/crashes: {len(danger)}")
    for r in danger:
        print(f"  NOT SAFE: {r.label} -> {r.outcome} (hash={r.final_hash})")
    print(f"[done] wrote {out_dir / 'results.json'} and results.md")


def _platform_str() -> str:
    import platform

    return f"{platform.system()} {platform.machine()} py{platform.python_version()}"


if __name__ == "__main__":
    main()
