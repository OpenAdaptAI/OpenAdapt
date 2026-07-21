"""Jitter-robust pixel-identity battery (evidence for ``PIXEL_VERIFY_ENABLED``).

De-risks the positive MATCH path of the pixel identity tier
(:func:`openadapt_flow.runtime.identity.verify_pixel_identity`) BEFORE its
default is flipped on. The naive un-aligned localized spike could MISMATCH a
wrong record but never safely VERIFY the correct one, because sub-pixel
cross-render JITTER of the SAME value spikes larger than a one-glyph change. The
tier now sub-pixel-ALIGNS the crops first; this module measures whether that
makes VERIFY safe.

The battery is fully self-contained (``cv2`` + ``numpy`` only, no browser, no
system fonts): identifier crops are rendered with ``cv2.putText`` (a
deterministic vector font whose O/0 and l/1 glyphs are distinct) and combined
with the committed real-browser-render crops under
``benchmark/pixel_identity/``. Each is then re-rendered under realistic
cross-render jitter (sub-pixel shift, JPEG q<=10, 105-150% DPI scale, theme
inversion) and scored by the SAME production metric the runtime uses
(:func:`openadapt_flow.runtime.identity.pixel_identity_distance`).

Two record classes are measured:

- **same-record** -- recorded crop vs a jittered re-render of the SAME
  identifier. Must MATCH on a matching render (low false-mismatch) and may
  safely ABSTAIN under heavy drift.
- **different-record** -- recorded crop vs a jittered render of a
  glyph-collapse sibling (O/0, l/1 -- the OCR-blind wrong patient) OR a wholly
  different MRN. Must NEVER MATCH (zero false-accept, the hard requirement);
  MISMATCH or ABSTAIN are both safe.

``run_battery`` returns per-trial rows and a summary; ``render_markdown``
formats the committed evidence. The unit battery (``tests/
test_pixel_identity_aligned.py``) asserts the safety invariants directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from openadapt_flow.runtime import identity as I

# --- identifier corpus -----------------------------------------------------
# Each collapse pair is ONE OCR-confusable glyph apart (a DIFFERENT patient the
# string layer cannot tell apart -- exactly the wrong-record class the pixel
# tier must catch). Spans O/0 and l/1 in digit- and alpha-flanked positions,
# plus the rn/m ligature collapse from the vision review.
COLLAPSE_PAIRS: list[tuple[str, str]] = [
    ("MG4408", "MG44O8"),
    ("AC50061", "AC5OO61"),
    ("RC90210", "RC9O210"),
    ("MG4118", "MG41l8"),
    ("AC50161", "AC50l61"),
    ("PL1X904", "PLlX904"),
    ("MRN00042", "MRN000A2"),
    ("PT1099", "PTl099"),
    ("100011", "1OO011"),
    ("801100", "8O1100"),
    ("rn0012", "m0012"),
    ("ID10058", "IDl0058"),
    ("B0OK21", "BOOK21"),
]

# Wholly different MRNs (multi-glyph): a wrong record that is NOT a near-homonym.
WRONG_PAIRS: list[tuple[str, str]] = [
    ("MG4408", "XT7213"),
    ("AC50061", "RC90210"),
    ("RC90210", "AC50161"),
    ("PT1099", "ZQ8845"),
    ("ID10058", "MG4408"),
]

_CROP_DIR = Path(__file__).resolve().parents[2] / "benchmark" / "pixel_identity"
_REAL_CROP_STEMS = ("crop_O0_digit_1", "crop_O0_digit_2", "crop_O0_digit_3")

# A render is "matching" (MATCH-eligible) when it carries only sub-pixel jitter
# or mild compression; DPI scale and theme inversion are "drift" (ABSTAIN-ok).
_MATCHING_CONDS = ("perfect", "jit", "jpeg")


def _matching(cond: str) -> bool:
    return any(cond == c or cond.startswith(c) for c in _MATCHING_CONDS)


# --- rendering + jitter transforms -----------------------------------------


def _to_png(arr: Any) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".png", arr)
    if not ok:  # pragma: no cover - imencode does not fail on valid arrays
        raise RuntimeError("cv2.imencode failed")
    return bytes(buf.tobytes())


def render_mrn(text: str, *, px: int = 44, thickness: int = 2, pad: int = 12) -> Any:
    """A white identifier cell with ``text`` drawn in a deterministic vector
    font (``cv2.putText``; O/0 and l/1 render distinctly). Portable across
    platforms with no system-font dependency."""
    import cv2
    import numpy as np

    scale = px / 40.0
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    img = np.full((th + 2 * pad, tw + 2 * pad, 3), 255, np.uint8)
    cv2.putText(
        img,
        text,
        (pad, pad + th),
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (0, 0, 0),
        thickness,
        cv2.LINE_AA,
    )
    return img


def _subpixel_shift(arr: Any, dx: float, dy: float) -> Any:
    import cv2
    import numpy as np

    m = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(
        arr,
        m,
        (arr.shape[1], arr.shape[0]),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def _jpeg(arr: Any, quality: int) -> Any:
    import cv2

    ok, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _scale(arr: Any, factor: float) -> Any:
    import cv2

    h, w = arr.shape[:2]
    return cv2.resize(
        arr,
        (max(1, int(w * factor)), max(1, int(h * factor))),
        interpolation=cv2.INTER_CUBIC,
    )


def jitter_variants(base: Any) -> list[tuple[str, Any]]:
    """Realistic cross-render re-renders of ``base``: a perfect re-render, a
    sub-pixel jitter grid, mild-to-heavy JPEG, DPI scales, and theme inversion.
    Same generator for same- and different-record crops so the comparison is
    like-for-like."""
    out: list[tuple[str, Any]] = [("perfect", base.copy())]
    for dx in (0.25, 0.5, 0.75, 1.0, 1.5):
        for dy in (0.0, 0.5, 1.0):
            out.append((f"jit_{dx}_{dy}", _subpixel_shift(base, dx, dy)))
    for q in (25, 18, 10):
        out.append((f"jpeg_{q}", _jpeg(_subpixel_shift(base, 0.8, 0.6), q)))
    for f in (1.05, 1.10, 1.25, 1.5):
        out.append(
            (f"scale_{int(f * 100)}", _subpixel_shift(_scale(base, f), 0.5, 0.4))
        )
    out.append(("dark", 255 - base))
    return out


# --- decision + battery ----------------------------------------------------


def decide(recorded_png: bytes, live_png: bytes, *, enable_verify: bool) -> str:
    """Production three-way verdict via the runtime tier: match / mismatch /
    abstain."""
    check = I.verify_pixel_identity(recorded_png, live_png, enable_verify=enable_verify)
    if check is None:
        return "abstain"
    return "match" if check.status == "verified" else check.status


def _real_crop_png(stem: str, kind: str) -> Optional[bytes]:
    path = _CROP_DIR / f"{stem}_{kind}.png"
    if not path.exists():
        return None
    return path.read_bytes()


def _real_crop_arr(stem: str, kind: str) -> Optional[Any]:
    import cv2

    path = _CROP_DIR / f"{stem}_{kind}.png"
    if not path.exists():
        return None
    return cv2.imread(str(path))


def run_battery(*, enable_verify: bool = True) -> dict[str, Any]:
    """Run the full jitter battery. Returns ``{"rows": [...], "summary": {...}}``.

    Each row is ``(record_class, condition, matching_render, decision)``.
    """
    rows: list[tuple[str, str, bool, str]] = []

    def _record(kind: str, rec_png: bytes, live_arr: Any, cond: str) -> None:
        d = decide(rec_png, _to_png(live_arr), enable_verify=enable_verify)
        rows.append((kind, cond, _matching(cond), d))

    # synthetic cv2-rendered corpus
    for tgt, sib in COLLAPSE_PAIRS:
        rec = render_mrn(tgt)
        rec_png = _to_png(rec)
        for cond, live in jitter_variants(rec):
            _record("same", rec_png, live, cond)
        sib_img = render_mrn(sib)
        for cond, live in jitter_variants(sib_img):
            _record("diff_collapse", rec_png, live, cond)
    for tgt, sib in WRONG_PAIRS:
        rec_png = _to_png(render_mrn(tgt))
        sib_img = render_mrn(sib)
        for cond, live in jitter_variants(sib_img):
            _record("diff_wrong", rec_png, sib_img if cond == "perfect" else live, cond)

    # committed real-browser-render crops
    for stem in _REAL_CROP_STEMS:
        tgt_arr = _real_crop_arr(stem, "target")
        sib_arr = _real_crop_arr(stem, "sibling")
        tgt_png = _real_crop_png(stem, "target")
        if tgt_arr is None or sib_arr is None or tgt_png is None:
            continue
        for cond, live in jitter_variants(tgt_arr):
            _record("same", tgt_png, live, cond)
        for cond, live in jitter_variants(sib_arr):
            _record("diff_collapse", tgt_png, live, cond)

    return {"rows": rows, "summary": summarize(rows)}


def summarize(rows: list[tuple[str, str, bool, str]]) -> dict[str, Any]:
    same = [r for r in rows if r[0] == "same"]
    diff = [r for r in rows if r[0].startswith("diff")]
    same_matching = [r for r in same if r[2]]
    diff_collapse_matching = [r for r in rows if r[0] == "diff_collapse" and r[2]]

    def _rate(subset: list[tuple[str, str, bool, str]], dec: str) -> float:
        return (sum(1 for r in subset if r[3] == dec) / len(subset)) if subset else 0.0

    false_accept = sum(1 for r in diff if r[3] == "match")
    false_mismatch = sum(1 for r in same if r[3] == "mismatch")
    return {
        "n_same": len(same),
        "n_diff": len(diff),
        "false_accept": false_accept,
        "false_mismatch": false_mismatch,
        "same_match_rate_matching_render": _rate(same_matching, "match"),
        "same_mismatch_rate": (false_mismatch / len(same)) if same else 0.0,
        "diff_collapse_mismatch_rate_matching_render": _rate(
            diff_collapse_matching, "mismatch"
        ),
        "n_diff_wrong_matched": sum(
            1 for r in rows if r[0] == "diff_wrong" and r[3] == "match"
        ),
    }


def distance_stats() -> dict[str, Any]:
    """Aggregate the raw ``max_window`` (whole-crop match statistic) for the
    same-record MATCHING renders vs every different-record, to expose the gap
    the VERIFY gate sits in."""
    import numpy as np

    same_matching: list[float] = []
    diff_all: list[float] = []
    for tgt, sib in COLLAPSE_PAIRS:
        rec_png = _to_png(render_mrn(tgt))
        for cond, live in jitter_variants(render_mrn(tgt)):
            dist = I.pixel_identity_distance(rec_png, _to_png(live))
            if dist and _matching(cond):
                same_matching.append(dist.max_window)
        for _cond, live in jitter_variants(render_mrn(sib)):
            dist = I.pixel_identity_distance(rec_png, _to_png(live))
            if dist:
                diff_all.append(dist.max_window)
    for tgt, sib in WRONG_PAIRS:
        rec_png = _to_png(render_mrn(tgt))
        for _cond, live in jitter_variants(render_mrn(sib)):
            dist = I.pixel_identity_distance(rec_png, _to_png(live))
            if dist:
                diff_all.append(dist.max_window)
    sm = np.array(same_matching)
    da = np.array(diff_all)
    return {
        "same_matching_max_window_max": float(sm.max()),
        "same_matching_max_window_p95": float(np.percentile(sm, 95)),
        "diff_min_max_window": float(da.min()),
        "diff_p5_max_window": float(np.percentile(da, 5)),
        "gate": I.PIXEL_VERIFY_MAX_WINDOW,
        "gap": float(da.min() - sm.max()),
    }


def render_markdown() -> str:
    res = run_battery(enable_verify=True)
    s = res["summary"]
    d = distance_stats()
    lines = [
        "# Jitter-robust pixel-identity battery",
        "",
        "Evidence for the positive VERIFY (MATCH) path of the pixel identity "
        "tier (`runtime.identity.verify_pixel_identity`). Self-contained "
        "(`cv2`+`numpy`, no browser/system fonts): `cv2.putText`-rendered MRNs "
        "and the committed real-browser-render crops, each re-rendered under "
        "sub-pixel jitter, JPEG q<=10, 105-150% DPI, and theme inversion, then "
        "scored by the SAME production metric the runtime uses.",
        "",
        "## Safety invariant (the hard requirement)",
        "",
        f"- **false-accept (different record -> MATCH): {s['false_accept']} / "
        f"{s['n_diff']} different-record trials** — MUST be 0.",
        f"- false-mismatch (same record -> MISMATCH): {s['false_mismatch']} / "
        f"{s['n_same']} ({s['same_mismatch_rate']:.1%}) — safe over-halt.",
        "",
        "## Utility",
        "",
        f"- same-record MATCH rate on matching renders: "
        f"{s['same_match_rate_matching_render']:.0%}",
        f"- glyph-collapse sibling MISMATCH (HALT) on matching renders: "
        f"{s['diff_collapse_mismatch_rate_matching_render']:.0%}",
        "",
        "## Why VERIFY is now safe (the clean gap)",
        "",
        "The worst aligned window (whole-crop match statistic) separates:",
        "",
        f"- same-record matching renders: max {d['same_matching_max_window_max']:.4f} "
        f"(p95 {d['same_matching_max_window_p95']:.4f})",
        f"- every different-record: min {d['diff_min_max_window']:.4f} "
        f"(p5 {d['diff_p5_max_window']:.4f})",
        f"- VERIFY gate `PIXEL_VERIFY_MAX_WINDOW` = {d['gate']:.4f} sits in the "
        f"gap (margin to nearest different-record = {d['gap']:.4f}).",
        "",
        "## Enable bar",
        "",
        "This evidence is SYNTHETIC (rendered + committed browser crops), so "
        "`PIXEL_VERIFY_ENABLED` stays `False` by default. The exact bar to flip "
        "it: reproduce `false_accept == 0` with a comparable gap on a REAL "
        "captured RDP/Citrix/HDX identifier corpus. See `docs/LIMITS.md`.",
        "",
    ]
    return "\n".join(lines)
