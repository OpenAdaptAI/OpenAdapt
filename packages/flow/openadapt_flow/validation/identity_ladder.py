"""Integrated identity-ladder measurement on the dense glyph-collapse surface.

Homonym pairs span BOTH the O/0 and l/1 classes and BOTH alphanumeric and
PURELY NUMERIC MRNs (the 9th wrong-patient reopening: 100512 vs a homonym's
1OO512, which the earlier alpha-prefixed corpus hid).

Every number here comes from the PRODUCTION tier stack: this harness drives
the REAL ``Replayer._verify_identity`` (structured-text -> pixel-compare ->
optional VLM veto -> OCR name+DOB -> HALT), NOT a hand-built tier subset.

WHY THIS MATTERS (the measurement flaw the 8th reopening exposed): an earlier
version of this harness measured the pixel-only configs with ``[pixel]`` only,
omitting the OCR tier that ``Replayer._verify_identity`` ALWAYS appends
(replayer.py). Its "0 false-accept" table was therefore measured against a
NON-production tier stack -- it never exercised the OCR name+DOB tier where a
same-name/same-DOB homonym with a collapsible MRN actually false-accepts. This
harness closes that gap: it constructs the anchor + backend + resolution for
each substrate and calls the real method, so the OCR tier is in the stack for
every config, and the numbers are the TRUE production numbers.

Two safety numbers per config:

- **false-accept** -- a WRONG patient (a different-patient sibling sharing NAME
  and DOB, whose MRN is one glyph -- O/0 -- from the target so OCR collapses it
  to the same string) is VERIFIED. Must be **0 in every config**.
- **over-halt** -- the CORRECT patient (the recorded target, re-resolved) is
  halted instead of verified. Safe but costly; reported per config.

Configs (strongest available substrate first):

1. ``structured``           -- browser/DOM: the structured-text tier compares
   the REAL MRN strings (O and 0 distinct). 0 false-accept, 0 over-halt.
2. ``pixel_stable``         -- pure pixel, stable render, an identifier crop
   captured. The pixel-compare tier's VERIFY path is HARD-GATED (Blocker 2:
   cross-render jitter defeats a safe same/different threshold at realistic
   crop scale), so it ABSTAINS on the correct patient and MISMATCHES the
   wrong one; the OCR tier then abstains on the collapsible MRN. 0 FA;
   over-halt = all correct rows (the gated-pixel-tier cost).
3. ``pixel_drift_vlm_on``   -- pure pixel, DRIFTED render, optional VLM veto
   ON: pixel-compare ABSTAINS under drift; the VLM is VETO-ONLY (a "same"
   answer cannot grant a pass -> it abstains), so a wrong patient is vetoed
   (HALT) and a correct patient falls to the OCR tier, which also abstains on
   the collapsible MRN -> HALT. 0 FA; over-halt = all correct rows.
4. ``pixel_drift_vlm_off``  -- pure pixel, DRIFTED render, VLM OFF: pixel
   abstains, OCR tier abstains on the collapsible MRN. 0 FA; OH = all correct.
5. ``ocr_only_confusable``  -- pure pixel, NO identifier crop captured and no
   VLM: ONLY the OCR name+DOB tier can speak, and the band rests on a
   glyph-confusable MRN -> it ABSTAINS -> HALT. 0 FA; OH = all correct. This is
   the config the flawed harness never measured; it is the honest "OCR alone
   cannot verify a collapsible MRN" outcome.

NB the pixel VERIFY path is currently GATED (Blocker 2) and the compiler does
not yet capture an identifier crop, so on EVERY pure-pixel config today the
only tier that can VERIFY is structured text; a collapsible MRN on a pixel-only
substrate HALTs. The pixel/VLM tiers still fail-safe (mismatch / abstain), so
the safety invariant holds; the cost is availability, disclosed in LIMITS.md.

PRODUCT IMPLICATION (docs/LIMITS.md): on a pure-pixel substrate a band whose
identity rests on a glyph-confusable MRN is NOT safely verifiable by OCR alone;
it needs the pixel-crop tier (on a stable render) or the structured-text tier.
Under render drift with no structured text the honest outcome is HALT.

The VLM tier is driven by a ``ProbeFaithfulVLM`` reproducing the validated
local-VLM probe (benchmark/vlm_identity, PR #28): 100% detection / 0%
false-accept on the OCR-collapse surface. The real model plugs in via
``openadapt_flow.runtime.identity_vlm.MLXIdentityVLM``.

Run:
    python -m openadapt_flow.validation.identity_ladder \\
        --out benchmark/identity_ladder
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from openadapt_flow.compiler.compile import (
    MIN_OCR_CONFIDENCE,
    _discriminative_crop_region,
)
from openadapt_flow.ir import Anchor, Resolution, Step, Workflow
from openadapt_flow.runtime import identity as I
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.validation.dense_surface import DenseTable, Row, render_table_html
from openadapt_flow.validation.pixel_identity_probe import COLLAPSE_PAIRS

# Glyph-collapse pairs OCR provably collapses -- BOTH the O/0 and l/1 classes,
# and (the 9th wrong-patient reopening) the PURELY NUMERIC MRNs the earlier
# alpha-prefixed corpus hid. Each becomes a same-NAME/same-DOB HOMONYM pair:
# two DIFFERENT patients sharing name+DOB, differing only in the one-glyph MRN
# (target digit vs sibling letter). Every pair is measured; a shared name+DOB is
# assigned round-robin from the pool below (so adding pairs never truncates).
_COLLAPSE_PAIRS = list(COLLAPSE_PAIRS)

# A shared name+DOB pool (so the ONLY discriminator is the collapsible MRN --
# the exact wrong-patient shape). Names are fake.
_SHARED = [
    ("Smith, John", "01/15/1980"),
    ("Okafor, Philip", "1966-01-17"),
    ("Petrov, Robert", "1944-08-08"),
    ("Nakamura, Karen", "1947-11-05"),
    ("Fitzgerald, Susan", "1958-09-30"),
]


@dataclass
class RenderCond:
    name: str
    font_family: str = "Arial"
    font_px: int = 15
    dsf: int = 2
    dark: bool = False
    zoom: float = 1.0


RECORD = RenderCond("record")
STABLE = RenderCond("stable")
DRIFTS = [
    RenderCond("dark", dark=True),
    RenderCond("zoom", zoom=1.20),
    RenderCond("font", font_family="Georgia"),
]

_TARGET_ROW = 2  # two filler rows precede the patient row


@dataclass
class Rendered:
    png: bytes
    viewport: tuple[int, int]
    open_point: tuple[int, int]
    click_struct: Optional[str]  # DOM row text EXCLUDING the Open cell
    mrn_region: tuple[int, int, int, int]


def _filler(i: int) -> Row:
    return Row(
        f"Filler{i}, Pat", "1971-02-0%d" % (i % 9 + 1), f"ZZ{1000 + i}", "M", "Active"
    )


def _render(browser, name: str, dob: str, mrn: str, cond: RenderCond) -> Rendered:
    """Render a dense table with the patient (name/dob/mrn) at _TARGET_ROW and
    return the frame PNG plus the Open-button click point, the DOM row text at
    that point (structured identity), and the MRN cell region -- everything the
    real ladder's four tiers consume, from ONE render.

    ``browser`` is a shared, already-launched Chromium (opened once by ``run``);
    each render only opens a cheap new page, not a new browser process.
    """
    rows = [_filler(0), _filler(1), Row(name, dob, mrn, "M", "Active"), _filler(3)]
    table = DenseTable(rows=rows, pairs=[], n_rows=len(rows))
    html = render_table_html(
        table,
        font_family=cond.font_family,
        font_px=cond.font_px,
        row_pad_px=6,
        top_offset_px=0,
    )
    dsf = cond.dsf
    page = browser.new_page(
        viewport={"width": 1120, "height": 1600}, device_scale_factor=dsf
    )
    try:
        page.set_content(html, wait_until="networkidle")
        if cond.dark:
            page.add_style_tag(
                content=(
                    "body{background:#0f1720 !important;color:#e6edf3 !important;}"
                    "tbody td{color:#e6edf3 !important;}"
                    "thead th{background:#1b2530 !important;color:#dfe6ee !important;}"
                )
            )
        if cond.zoom != 1.0:
            page.add_style_tag(content=f"body{{zoom:{cond.zoom};}}")
        full_h = page.evaluate("document.body.scrollHeight")
        page.set_viewport_size({"width": 1120, "height": int(full_h) + 20})
        png = page.screenshot(full_page=True)
        vw, vh = 1120 * dsf, (int(full_h) + 20) * dsf
        k = _TARGET_ROW
        open_bb = page.eval_on_selector(
            f'[data-open="{k}"]',
            "el => { const r = el.getBoundingClientRect();"
            " return [r.x, r.y, r.width, r.height]; }",
        )
        mrn_bb = page.eval_on_selector(
            f'[data-row="{k}"] .mrn',
            "el => { const r = el.getBoundingClientRect();"
            " return [r.x, r.y, r.width, r.height]; }",
        )
        open_point = (
            int((open_bb[0] + open_bb[2] / 2) * dsf),
            int((open_bb[1] + open_bb[3] / 2) * dsf),
        )
        struct = page.evaluate(
            "([px, py]) => {"
            " const el = document.elementFromPoint(px, py);"
            " if (!el) return null;"
            " const row = el.closest('tr'); if (!row) return null;"
            " const own = el.closest('td') || el;"
            " own.setAttribute('data-o','1');"
            " const clone = row.cloneNode(true);"
            " const m = clone.querySelector('[data-o=\"1\"]'); if (m) m.remove();"
            " own.removeAttribute('data-o');"
            " return (clone.textContent||'').replace(/\\s+/g,' ').trim()||null; }",
            [open_bb[0] + open_bb[2] / 2, open_bb[1] + open_bb[3] / 2],
        )
        mrn_region = (
            int(mrn_bb[0] * dsf),
            int(mrn_bb[1] * dsf),
            int(mrn_bb[2] * dsf),
            int(mrn_bb[3] * dsf),
        )
    finally:
        page.close()
    return Rendered(
        png=png,
        viewport=(vw, vh),
        open_point=open_point,
        click_struct=struct,
        mrn_region=mrn_region,
    )


class ProbeFaithfulVLM:
    """Veto-only VLM stub reproducing the validated probe (benchmark/vlm_identity,
    PR #28): different-patient (collapse) pairs -> "different" (100% detection);
    same-value pairs -> "same". Under the VETO-ONLY contract a "same" answer no
    longer grants a pass (verify_vlm_identity folds it to abstain)."""

    def __init__(self, is_same: bool) -> None:
        self._is_same = is_same

    def same_or_different(self, recorded_png: bytes, live_png: bytes) -> str:
        return "same" if self._is_same else "different"


class _Backend:
    def __init__(self, viewport, live_png, structured_at=None):
        self.viewport = viewport
        self._live = live_png
        self._structured_at = structured_at

    def screenshot(self):
        return self._live

    # Present ONLY on the structured (browser/DOM) substrate.
    def structured_text_at(self, x, y):
        if self._structured_at is None:
            raise AttributeError("pixel-only substrate has no structured text")
        return self._structured_at


def _make_backend(viewport, live_png, structured_live):
    b = _Backend(viewport, live_png)
    if structured_live is None:
        # pixel-only: remove structured_text_at so the tier is UNAVAILABLE.
        b.structured_text_at = None  # type: ignore[assignment]
    else:
        b._structured_at = structured_live
    return b


def _anchor(
    rec: Rendered, *, with_structured: bool, with_crop: bool, bundle_dir: Optional[Path]
) -> Anchor:
    """Build the recorded anchor exactly as the compiler would for a click on
    the Open button: OCR context band (name+DOB+MRN), optional DOM structured
    identity, optional identifier crop (the MRN cell)."""
    import openadapt_flow.vision as vision
    from openadapt_flow.runtime.identity import band_region, context_from_lines

    frame_bgr = cv2.imdecode(np.frombuffer(rec.png, np.uint8), cv2.IMREAD_COLOR)
    click = rec.open_point
    crop_region = _discriminative_crop_region(frame_bgr, click)
    lines = vision.ocr(rec.png)
    from datetime import date

    context_text = context_from_lines(
        lines,
        exclude_region=crop_region,
        band=band_region(click, crop_region[3], rec.viewport),
        point=click,
        min_confidence=MIN_OCR_CONFIDENCE,
        reference_date=date.today(),
    )

    identifier_crop = None
    identifier_region = None
    if with_crop and bundle_dir is not None:
        x, y, w, h = rec.mrn_region
        crop = frame_bgr[y : y + h, x : x + w]
        ok, buf = cv2.imencode(".png", crop)
        assert ok
        (bundle_dir / "idcrop.png").write_bytes(buf.tobytes())
        identifier_crop = "idcrop.png"
        identifier_region = rec.mrn_region

    return Anchor(
        template="t.png",
        region=crop_region,
        click_point=click,
        context_text=context_text,
        structured_identity=rec.click_struct if with_structured else None,
        identifier_crop=identifier_crop,
        identifier_region=identifier_region,
    )


def _verdict(
    rec: Rendered,
    live: Rendered,
    *,
    with_structured: bool,
    with_crop: bool,
    vlm,
    bundle_dir: Optional[Path],
) -> I.IdentityCheck:
    """Drive the REAL Replayer._verify_identity for this substrate config."""
    import openadapt_flow.vision as vision

    anchor = _anchor(
        rec, with_structured=with_structured, with_crop=with_crop, bundle_dir=bundle_dir
    )
    step = Step(
        id="open",
        intent="open patient chart",
        action="click",
        anchor=anchor,
        risk="irreversible",
    )
    wf = Workflow(name="ladder", params={}, steps=[step])
    # target/sibling rendered at the SAME table index -> same geometry, so the
    # resolved point equals the recorded click point (crop/band line up).
    res = Resolution(rung="ocr", point=live.open_point, confidence=0.9, elapsed_ms=1.0)
    backend = _make_backend(
        live.viewport,
        live.png,
        structured_live=live.click_struct if with_structured else None,
    )
    replayer = Replayer(backend, vision=vision, identity_vlm=vlm)
    return replayer._verify_identity(step, res, live.png, {}, wf, bundle_dir)


def _outcome(check: I.IdentityCheck) -> str:
    """click (proceed) iff verified; every other verdict HALTs."""
    return "click" if check.status == "verified" else "halt"


def _measure(name: str, cases: list[dict]) -> dict:
    fa = sum(1 for c in cases if c["scenario"] == "wrong" and c["outcome"] == "click")
    nw = sum(1 for c in cases if c["scenario"] == "wrong")
    oh = sum(1 for c in cases if c["scenario"] == "correct" and c["outcome"] == "halt")
    nc = sum(1 for c in cases if c["scenario"] == "correct")
    return {
        "config": name,
        "n_correct": nc,
        "n_wrong": nw,
        "false_accept": fa,
        "false_accept_rate": (fa / nw) if nw else 0.0,
        "over_halt": oh,
        "over_halt_rate": (oh / nc) if nc else 0.0,
        "cases": cases,
    }


def run(out_dir: Path) -> dict:
    pairs = [(p, _SHARED[i % len(_SHARED)]) for i, p in enumerate(_COLLAPSE_PAIRS)]
    # Pre-render every (pair, condition) frame once.
    rec: dict[str, Rendered] = {}
    stable_t: dict[str, Rendered] = {}
    stable_s: dict[str, Rendered] = {}
    drift_t: dict[tuple[str, str], Rendered] = {}
    drift_s: dict[tuple[str, str], Rendered] = {}
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for p, (name, dob) in pairs:
                rec[p.label] = _render(browser, name, dob, p.target, RECORD)
                stable_t[p.label] = _render(browser, name, dob, p.target, STABLE)
                stable_s[p.label] = _render(browser, name, dob, p.sibling, STABLE)
                for d in DRIFTS:
                    drift_t[(p.label, d.name)] = _render(
                        browser, name, dob, p.target, d
                    )
                    drift_s[(p.label, d.name)] = _render(
                        browser, name, dob, p.sibling, d
                    )
        finally:
            browser.close()

    results: dict[str, dict] = {}
    tmp = Path(tempfile.mkdtemp(prefix="idladder_"))

    def config(name, *, cond_kind, with_structured, with_crop, vlm_on):
        cases = []
        for p, _ in pairs:
            bd = tmp if with_crop else None
            if cond_kind == "stable":
                live_c, live_w = stable_t[p.label], stable_s[p.label]
                chk_c = _verdict(
                    rec[p.label],
                    live_c,
                    with_structured=with_structured,
                    with_crop=with_crop,
                    vlm=(ProbeFaithfulVLM(True) if vlm_on else None),
                    bundle_dir=bd,
                )
                chk_w = _verdict(
                    rec[p.label],
                    live_w,
                    with_structured=with_structured,
                    with_crop=with_crop,
                    vlm=(ProbeFaithfulVLM(False) if vlm_on else None),
                    bundle_dir=bd,
                )
                cases.append(
                    {
                        "pair": p.label,
                        "cond": "stable",
                        "scenario": "correct",
                        "outcome": _outcome(chk_c),
                        "status": chk_c.status,
                        "mode": chk_c.mode,
                    }
                )
                cases.append(
                    {
                        "pair": p.label,
                        "cond": "stable",
                        "scenario": "wrong",
                        "outcome": _outcome(chk_w),
                        "status": chk_w.status,
                        "mode": chk_w.mode,
                    }
                )
            else:  # drift: measure each drift condition
                for d in DRIFTS:
                    live_c = drift_t[(p.label, d.name)]
                    live_w = drift_s[(p.label, d.name)]
                    chk_c = _verdict(
                        rec[p.label],
                        live_c,
                        with_structured=with_structured,
                        with_crop=with_crop,
                        vlm=(ProbeFaithfulVLM(True) if vlm_on else None),
                        bundle_dir=bd,
                    )
                    chk_w = _verdict(
                        rec[p.label],
                        live_w,
                        with_structured=with_structured,
                        with_crop=with_crop,
                        vlm=(ProbeFaithfulVLM(False) if vlm_on else None),
                        bundle_dir=bd,
                    )
                    cases.append(
                        {
                            "pair": p.label,
                            "cond": d.name,
                            "scenario": "correct",
                            "outcome": _outcome(chk_c),
                            "status": chk_c.status,
                            "mode": chk_c.mode,
                        }
                    )
                    cases.append(
                        {
                            "pair": p.label,
                            "cond": d.name,
                            "scenario": "wrong",
                            "outcome": _outcome(chk_w),
                            "status": chk_w.status,
                            "mode": chk_w.mode,
                        }
                    )
        results[name] = _measure(name, cases)

    config(
        "structured",
        cond_kind="stable",
        with_structured=True,
        with_crop=False,
        vlm_on=False,
    )
    config(
        "pixel_stable",
        cond_kind="stable",
        with_structured=False,
        with_crop=True,
        vlm_on=False,
    )
    config(
        "pixel_drift_vlm_on",
        cond_kind="drift",
        with_structured=False,
        with_crop=True,
        vlm_on=True,
    )
    config(
        "pixel_drift_vlm_off",
        cond_kind="drift",
        with_structured=False,
        with_crop=True,
        vlm_on=False,
    )
    config(
        "ocr_only_confusable",
        cond_kind="drift",
        with_structured=False,
        with_crop=False,
        vlm_on=False,
    )

    summary = {
        "surface": "dense glyph-collapse same-name/same-DOB homonyms -- O/0 and "
        "l/1, alphanumeric AND purely-numeric MRNs (different "
        "patients one MRN glyph apart; 9th reopening added numerics)",
        "n_pairs": len(pairs),
        "measured_via": "the REAL Replayer._verify_identity production tier "
        "stack (structured -> pixel -> vlm -> OCR -> halt); no "
        "hand-built tier subset",
        "vlm_source": "ProbeFaithfulVLM reproducing benchmark/vlm_identity "
        "(PR #28): 100% detection / 0% false-accept; veto-only "
        "(a 'same' answer abstains, never grants a pass)",
        "configs": {
            k: {kk: vv for kk, vv in v.items() if kk != "cases"}
            for k, v in results.items()
        },
        "safety_invariant_false_accept_zero_all_configs": all(
            v["false_accept"] == 0 for v in results.values()
        ),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "identity_ladder.json").write_text(
        json.dumps({"summary": summary, "results": results}, indent=1)
    )
    (out_dir / "IDENTITY_LADDER.md").write_text(_markdown(summary))
    return summary


def _markdown(summary: dict) -> str:
    lines = [
        "# Integrated identity ladder — measured on the dense glyph-collapse "
        "surface (O/0 + l/1, alphanumeric AND purely-numeric MRNs)",
        "",
        "Every number below comes from the **production tier stack**: this "
        "harness drives the REAL `Replayer._verify_identity` "
        "(**structured text → pixel-compare → optional VLM veto → OCR "
        "name+DOB → halt**), never a hand-built tier subset. The OCR tier the "
        "replayer ALWAYS appends is therefore in the stack for every config — "
        "closing the measurement flaw that hid the 8th wrong-patient "
        "reopening.",
        "",
        f"Surface: {summary['surface']} ({summary['n_pairs']} homonym pairs, "
        "each measured CORRECT-resolution and WRONG-resolution).",
        "",
        "| Config | substrate | false-accept | over-halt |",
        "|---|---|---:|---:|",
    ]
    labels = {
        "structured": "browser/DOM (structured text)",
        "pixel_stable": "pixel-only, stable render, crop (pixel VERIFY gated)",
        "pixel_drift_vlm_on": "pixel-only, drifted render, VLM ON (veto-only)",
        "pixel_drift_vlm_off": "pixel-only, drifted render, VLM OFF",
        "ocr_only_confusable": "pixel-only, NO crop / NO VLM → OCR tier only",
    }
    for key, cfg in summary["configs"].items():
        fa = f"{cfg['false_accept']}/{cfg['n_wrong']} ({cfg['false_accept_rate']:.0%})"
        oh = f"{cfg['over_halt']}/{cfg['n_correct']} ({cfg['over_halt_rate']:.0%})"
        lines.append(f"| `{key}` | {labels[key]} | {fa} | {oh} |")
    inv = summary["safety_invariant_false_accept_zero_all_configs"]
    lines += [
        "",
        f"**Safety invariant — 0 false-accept across ALL configs, measured on "
        f"the real replayer stack: {'HOLDS' if inv else 'VIOLATED'}.**",
        "",
        "- **OCR alone cannot verify a collapsible MRN.** On a pure-pixel "
        "substrate, a band whose identity rests on a glyph-confusable MRN (ANY "
        "identifier-position token carrying an O/0 or l/1/I — numeric OR "
        "alphanumeric, the 9th reopening) is NOT safely verifiable "
        "by OCR: a same-name/same-DOB homonym whose distinguishing glyph OCR "
        "collapsed is indistinguishable. The OCR tier ABSTAINS → HALT (the "
        "`ocr_only_confusable` and `pixel_drift_*` over-halt). Safe "
        "verification needs the **structured-text tier** (DOM/a11y) — and, "
        "once Blocker 2's crop capture + jitter-robust distance land, the "
        "**pixel-crop tier** on a stable render. The OCR name+DOB tier alone "
        "is NOT a safe identity check on a collapsible MRN; on a pure-pixel "
        "substrate without structured text the honest outcome is HALT.",
        '- The VLM tier is **veto-only**: a `"same"` answer never grants a '
        "pass (it abstains), so under drift a correct patient falls through to "
        "the OCR tier and HALTs; the VLM can only REJECT a wrong patient. This "
        "is why `pixel_drift_vlm_on` over-halts on all correct rows.",
        "- The VLM tier is OPTIONAL and OFF by default: the default install "
        "runs structured-text + pixel-compare + OCR + halt with no model.",
        "- **Blocker 2**: the pixel-compare VERIFY path is HARD-GATED "
        "(cross-render sub-pixel jitter defeats a safe same/different "
        "threshold at realistic crop scale, and an absolute whole-crop "
        "threshold false-accepts a diluted one-glyph difference). The pixel "
        "tier may only MISMATCH (scale-invariant localized spike → safe HALT) "
        "or ABSTAIN until a fixed-size crop capture + jitter-robust distance "
        "land — so on a pure-pixel substrate the only tier that VERIFIES today "
        "is structured text.",
        f"- VLM verdicts: {summary['vlm_source']}.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="benchmark/identity_ladder", type=Path)
    args = ap.parse_args(argv)
    summary = run(args.out)
    print(json.dumps(summary["configs"], indent=1))
    print(
        "0 false-accept all configs (real stack):",
        summary["safety_invariant_false_accept_zero_all_configs"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
