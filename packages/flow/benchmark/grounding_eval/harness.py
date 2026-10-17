"""Dense-list grounding eval: openadapt-grounding (OCR text-anchoring) vs the
bespoke remote-VLM grounder baseline.

Context
-------
End-to-end validation of openadapt-flow's on-prem VLM appliance (PR #38,
``benchmark/appliance_validation/REPORT.md``) found the bespoke grounder
(``RemoteGrounder.locate`` -> served ``mlx-community/Qwen3-VL-4B-Instruct-4bit``)
resolves the correct control COLUMN but not the correct ROW on a dense EMR list:
**0/6 hits, ~472 px median error** (tol 40 px), the proposed y clustering near
the top regardless of the requested patient. Single-shot VLM coordinate
regression cannot disambiguate 40+ near-identical rows.

This harness measures a genuinely different, $0/local mechanism that the
ecosystem already ships: **openadapt-grounding**'s OCR text-anchoring
(``pillow`` + ``pytesseract``; core deps only, no GPU, no paid API, no served
model). It reuses the SAME dense surface, the SAME target selection, and the
SAME ground truth (the DOM centre of each row's Open button) as the baseline so
the numbers are directly comparable.

Three methods, all built only on openadapt-grounding primitives:

  A  shipped-find-label     ElementLocator.find("Open") -- the naive shipped API
                            asked for the button by its label. One of 51
                            identical "Open" tokens; returns the first match.
                            (Expected to fail rows -- the shipped find() alone
                            has no more row information than the VLM does.)

  B  find-name + row-join   ElementLocator.find(<patient name>) locates the row
                            via its (mostly) unique name text, then a spatial
                            join picks the Open box on that same row. Uses the
                            library's registry + OCR for the anchor.

  C  ocr-row-anchor         Cluster the library's OCR boxes into rows, score
                            each row's text against the target's name + MRN
                            (the unique key, with O/0 l/1 glyph normalisation),
                            take the Open box in the winning row. The most
                            robust text-anchoring variant; disambiguates the
                            adversarial same-name collision pairs via the MRN.

No shipped runtime is edited. This adds only an eval harness + results.
"""

from __future__ import annotations

import io
import json
import re
import statistics
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from openadapt_flow.validation.dense_surface import (
    RECORD_CONDITION,
    build_dense_table,
    render_frame,
)
from openadapt_grounding.builder import Registry, RegistryBuilder
from openadapt_grounding.locator import ElementLocator
from openadapt_grounding.types import Element

HERE = Path(__file__).parent
TOL_STRICT = 40  # px, the REPORT.md headline tolerance
TOL_LOOSE = 60   # px, run_validation.run_grounder default
N_TARGETS = 6
SEED = 1
N_ROWS = 18  # floor; renders ~51 rows, exactly as the baseline harness used


# --------------------------------------------------------------------------
# OCR helpers (all boxes come from the openadapt-grounding library OCR path)
# --------------------------------------------------------------------------
def ocr_boxes_px(loc: ElementLocator, img: Image.Image) -> list[dict]:
    """Library OCR (ElementLocator._run_ocr), returned in pixel space."""
    w, h = img.size
    out = []
    for e in loc._run_ocr(img):  # noqa: SLF001 - the shipped OCR primitive
        x, y, bw, bh = e.bounds
        out.append(
            {
                "text": e.text or "",
                "cx": (x + bw / 2) * w,
                "cy": (y + bh / 2) * h,
                "x0": x * w,
                "x1": (x + bw) * w,
                "y0": y * h,
                "y1": (y + bh) * h,
                "h": bh * h,
                "conf": e.confidence,
            }
        )
    return out


def _norm_glyph(s: str) -> str:
    """Collapse the O/0 and l/1/I confusables so an OCR-noisy MRN still matches
    the target MRN by content (the same collapse the identity band worries about
    -- here it HELPS anchoring)."""
    return (
        s.lower()
        .replace("o", "0")
        .replace("l", "1")
        .replace("i", "1")
        .replace("|", "1")
    )


def cluster_rows(boxes: list[dict]) -> list[dict]:
    """Group OCR boxes into rows by y proximity. Pure spatial join over the
    library's OCR output -- no ground truth used."""
    if not boxes:
        return []
    med_h = statistics.median(b["h"] for b in boxes) or 20.0
    thresh = med_h * 0.8
    ordered = sorted(boxes, key=lambda b: b["cy"])
    rows: list[dict] = []
    cur: list[dict] = [ordered[0]]
    cur_y = ordered[0]["cy"]
    for b in ordered[1:]:
        if abs(b["cy"] - cur_y) <= thresh:
            cur.append(b)
            cur_y = sum(x["cy"] for x in cur) / len(cur)
        else:
            rows.append(_finish_row(cur))
            cur = [b]
            cur_y = b["cy"]
    rows.append(_finish_row(cur))
    return rows


def _finish_row(boxes: list[dict]) -> dict:
    text = " ".join(b["text"] for b in sorted(boxes, key=lambda b: b["cx"]))
    return {
        "boxes": boxes,
        "text": text,
        "cy": sum(b["cy"] for b in boxes) / len(boxes),
    }


def open_box_in_row(row: dict) -> Optional[dict]:
    cands = [b for b in row["boxes"] if "open" in b["text"].lower()]
    if not cands:
        return None
    # rightmost open token (the control column)
    return max(cands, key=lambda b: b["cx"])


# --------------------------------------------------------------------------
# Grounding methods
# --------------------------------------------------------------------------
def method_a_find_label(loc: ElementLocator, img: Image.Image) -> Optional[tuple]:
    """Shipped ElementLocator.find('Open') -> first OCR match, to pixels."""
    res = loc.find("Open", img)
    if not res.found:
        return None
    return res.to_pixels(*img.size)


def method_b_find_name_then_open(
    loc: ElementLocator, img: Image.Image, name: str, boxes: list[dict]
) -> Optional[tuple]:
    """Library find() on the (mostly unique) patient name -> row -> Open box."""
    res = loc.find(name, img)
    if not res.found or res.y is None:
        return None
    name_cy = res.y * img.size[1]
    med_h = statistics.median(b["h"] for b in boxes) or 20.0
    opens = [b for b in boxes if "open" in b["text"].lower()]
    if not opens:
        return None
    best = min(opens, key=lambda b: abs(b["cy"] - name_cy))
    if abs(best["cy"] - name_cy) > med_h * 1.5:
        return None
    return (int(best["cx"]), int(best["cy"]))


def method_c_row_anchor(
    rows: list[dict], name: str, mrn: str
) -> Optional[tuple]:
    """Score each OCR row against name tokens + MRN (glyph-normalised); the
    winning row's Open box is the proposal. Disambiguates same-name siblings by
    MRN."""
    name_tokens = [t for t in re.split(r"[,\s]+", name.lower()) if len(t) >= 3]
    mrn_norm = _norm_glyph(mrn)

    best_row = None
    best_score = -1.0
    for row in rows:
        rtext = row["text"].lower()
        rtext_norm = _norm_glyph(row["text"])
        score = sum(1 for t in name_tokens if t in rtext)
        # MRN is the unique key: heavy weight, allow glyph-collapsed match
        if mrn.lower() in rtext:
            score += 5
        elif mrn_norm in rtext_norm:
            score += 4
        else:
            # partial: longest shared MRN substring (OCR may drop a char)
            for L in range(len(mrn_norm), 3, -1):
                if any(
                    mrn_norm[i : i + L] in rtext_norm
                    for i in range(len(mrn_norm) - L + 1)
                ):
                    score += 2 * (L / len(mrn_norm))
                    break
        if score > best_score:
            best_score = score
            best_row = row
    if best_row is None or best_score <= 0:
        return None
    ob = open_box_in_row(best_row)
    if ob is None:
        return None
    return (int(ob["cx"]), int(ob["cy"]))


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------
def err_px(proposed: Optional[tuple], truth: tuple) -> Optional[float]:
    if proposed is None:
        return None
    return ((proposed[0] - truth[0]) ** 2 + (proposed[1] - truth[1]) ** 2) ** 0.5


def summarise(rows: list[dict]) -> dict:
    errs = [r["error_px"] for r in rows if r["error_px"] is not None]
    lat = [r["latency_s"] for r in rows]
    clean = [r for r in rows if not r["ocr_mrn_collision"]]
    coll = [r for r in rows if r["ocr_mrn_collision"]]
    return {
        "n_targets": len(rows),
        "returned_proposal_rate": len(errs) / len(rows) if rows else None,
        "hit_rate@40": sum(r["hit@40"] for r in rows) / len(rows) if rows else None,
        "hit_rate@60": sum(r["hit@60"] for r in rows) / len(rows) if rows else None,
        "median_error_px": statistics.median(errs) if errs else None,
        "mean_error_px": statistics.mean(errs) if errs else None,
        # Split: rows whose MRN is OCR-distinct vs the adversarial O0/l1
        # wrong-patient collision pairs (sibling disambiguation is the identity
        # band's job, not the grounder's).
        "clean_rows": {
            "n": len(clean),
            "hit_rate@40": (sum(r["hit@40"] for r in clean) / len(clean))
            if clean else None,
            "median_error_px": statistics.median(
                [r["error_px"] for r in clean if r["error_px"] is not None]
            ) if any(r["error_px"] is not None for r in clean) else None,
        },
        "ocr_collision_rows": {
            "n": len(coll),
            "hit_rate@40": (sum(r["hit@40"] for r in coll) / len(coll))
            if coll else None,
            "median_error_px": statistics.median(
                [r["error_px"] for r in coll if r["error_px"] is not None]
            ) if any(r["error_px"] is not None for r in coll) else None,
        },
        "latency_s": {
            "median": statistics.median(lat) if lat else None,
            "max": max(lat) if lat else None,
        },
        "rows": rows,
    }


def main() -> None:
    t_render = time.time()
    table = build_dense_table(seed=SEED, n_rows=N_ROWS)
    frame = render_frame(table, RECORD_CONDITION, top_offset_px=12)
    render_s = time.time() - t_render
    vw, vh = frame.viewport
    img = Image.open(io.BytesIO(frame.png)).convert("RGB")

    # Same target selection as run_validation.run_grounder.
    indices = sorted(frame.points.keys())
    step = max(1, len(indices) // N_TARGETS)
    chosen = indices[::step][:N_TARGETS]

    # Flag rows whose MRN glyph-collapses onto another row's MRN (the O0/l1
    # wrong-patient collision surface). On these, OCR text cannot separate the
    # target from its sibling -- that is the identity band's job, not the
    # grounder's -- so we report clean vs collision hit rates separately.
    norm_counts: dict[str, int] = {}
    for i in indices:
        norm_counts[_norm_glyph(table.rows[i].mrn)] = (
            norm_counts.get(_norm_glyph(table.rows[i].mrn), 0) + 1
        )
    ocr_collision = {
        i: norm_counts[_norm_glyph(table.rows[i].mrn)] > 1 for i in indices
    }

    # --- registries (built "offline from the demo", the intended pattern) ---
    # Label registry: a single "Open" control, as a demo would capture.
    lbl_builder = RegistryBuilder()
    lbl_builder.add_frame([Element(bounds=(0.9, 0.1, 0.05, 0.02), text="Open",
                                   element_type="button")])
    label_registry = lbl_builder.build(min_stability=0.0)
    loc_label = ElementLocator(label_registry, fuzzy_match=False)

    # Name registry: every row's patient name (unique-ish text anchors).
    name_builder = RegistryBuilder()
    name_builder.add_frame(
        [Element(bounds=(0.1, 0.1, 0.2, 0.02), text=table.rows[i].name)
         for i in indices]
    )
    name_registry = name_builder.build(min_stability=0.0)
    loc_name = ElementLocator(name_registry, fuzzy_match=True)

    # One OCR pass shared by B/C (the library primitive).
    t_ocr = time.time()
    boxes = ocr_boxes_px(loc_name, img)
    ocr_s = time.time() - t_ocr
    rows_clustered = cluster_rows(boxes)

    results: dict[str, list[dict]] = {"A_find_label": [], "B_find_name_row": [],
                                      "C_row_anchor": []}

    for i in chosen:
        name_point, open_point, y_center, _ = frame.points[i]
        row = table.rows[i]
        truth = open_point

        coll = ocr_collision[i]

        # A
        t0 = time.time()
        pa = method_a_find_label(loc_label, img)
        results["A_find_label"].append(_row(i, row, truth, pa, time.time() - t0, coll))

        # B
        t0 = time.time()
        pb = method_b_find_name_then_open(loc_name, img, row.name, boxes)
        results["B_find_name_row"].append(_row(i, row, truth, pb, time.time() - t0, coll))

        # C
        t0 = time.time()
        pc = method_c_row_anchor(rows_clustered, row.name, row.mrn)
        results["C_row_anchor"].append(_row(i, row, truth, pc, time.time() - t0, coll))

    out = {
        "meta": {
            "surface": "dense_surface.render_frame(build_dense_table(seed=1, "
            "n_rows=18), RECORD_CONDITION, top_offset_px=12)",
            "rendered_rows": len(table.rows),
            "viewport_px": [vw, vh],
            "chosen_row_indices": list(chosen),
            "truth": "DOM centre of each row's Open button (frame.points[i][1])",
            "tol_px": {"strict": TOL_STRICT, "loose": TOL_LOOSE},
            "render_s": round(render_s, 2),
            "ocr_s_once": round(ocr_s, 2),
            "ocr_engine": "pytesseract (ElementLocator._run_ocr)",
            "cost_usd": 0.0,
            "gpu": False,
            "paid_api_calls": 0,
        },
        "baseline_bespoke_vlm": {
            "source": "benchmark/appliance_validation/REPORT.md (PR #38)",
            "model": "mlx-community/Qwen3-VL-4B-Instruct-4bit (served)",
            "hit_rate@40": 0.0,
            "n_hits": "0/6",
            "median_error_px": 472,
            "latency_s": "35 (native 2240x3726); faster but unusable downscaled",
            "note": "correct column (x~=922 vs 919) but row wrong; y clusters at top",
        },
        "methods": {k: summarise(v) for k, v in results.items()},
    }
    (HERE / "results.json").write_text(json.dumps(out, indent=2))
    _print_summary(out)


def _row(i: int, row, truth: tuple, proposed: Optional[tuple], dt: float,
         ocr_collision: bool = False) -> dict:
    e = err_px(proposed, truth)
    return {
        "row": i,
        "name": row.name,
        "mrn": row.mrn,
        "ocr_mrn_collision": ocr_collision,
        "truth": list(truth),
        "proposed": list(proposed) if proposed else None,
        "error_px": e,
        "hit@40": bool(e is not None and e <= TOL_STRICT),
        "hit@60": bool(e is not None and e <= TOL_LOOSE),
        "latency_s": dt,
    }


def _print_summary(out: dict) -> None:
    b = out["baseline_bespoke_vlm"]
    print("=" * 72)
    print("Dense-list grounding: openadapt-grounding (OCR) vs bespoke VLM")
    print("=" * 72)
    print(f"surface: {out['meta']['rendered_rows']} rows @ "
          f"{out['meta']['viewport_px']}  |  targets: "
          f"{out['meta']['chosen_row_indices']}")
    print(f"BASELINE (bespoke VLM {b['model'].split('/')[-1]}): "
          f"hit@40=0/6, median_err={b['median_error_px']}px, "
          f"lat~{b['latency_s'].split(';')[0]}")
    print("-" * 72)
    for name, s in out["methods"].items():
        cr = s["clean_rows"]
        print(
            f"{name:18s} hit@40={s['hit_rate@40']*100:5.1f}%  "
            f"hit@60={s['hit_rate@60']*100:5.1f}%  "
            f"med_err={_fmt(s['median_error_px'])}px  "
            f"proposal={s['returned_proposal_rate']*100:4.0f}%  "
            f"lat_med={s['latency_s']['median']*1000:.0f}ms  "
            f"| clean {cr['n']} rows: "
            f"hit@40={(cr['hit_rate@40'] or 0)*100:.0f}% "
            f"med_err={_fmt(cr['median_error_px'])}px"
        )
    print("-" * 72)
    print(f"OCR (once, shared by B/C): {out['meta']['ocr_s_once']}s  "
          f"cost=${out['meta']['cost_usd']}  gpu={out['meta']['gpu']}")


def _fmt(v) -> str:
    return f"{v:6.1f}" if v is not None else "  None"


def sweep(seeds: list[int], targets_per_seed: int = 8) -> None:
    """Wider sample for the recommended method C across multiple rendered
    surfaces, split clean vs OCR-collision. All $0/local."""
    all_rows: list[dict] = []
    per_seed = []
    for seed in seeds:
        table = build_dense_table(seed=seed, n_rows=N_ROWS)
        frame = render_frame(table, RECORD_CONDITION, top_offset_px=12)
        img = Image.open(io.BytesIO(frame.png)).convert("RGB")
        loc = ElementLocator(Registry([]))
        boxes = ocr_boxes_px(loc, img)
        rows_clustered = cluster_rows(boxes)
        norm_counts: dict[str, int] = {}
        for i in frame.points:
            k = _norm_glyph(table.rows[i].mrn)
            norm_counts[k] = norm_counts.get(k, 0) + 1
        idx = sorted(frame.points.keys())
        step = max(1, len(idx) // targets_per_seed)
        chosen = idx[::step][:targets_per_seed]
        srows = []
        for i in chosen:
            _, open_point, _, _ = frame.points[i]
            row = table.rows[i]
            coll = norm_counts[_norm_glyph(row.mrn)] > 1
            t0 = time.time()
            p = method_c_row_anchor(rows_clustered, row.name, row.mrn)
            rr = _row(i, row, open_point, p, time.time() - t0, coll)
            srows.append(rr)
        all_rows.extend(srows)
        per_seed.append({"seed": seed, **summarise(srows)})
    agg = summarise(all_rows)
    out = {
        "method": "C_row_anchor",
        "seeds": seeds,
        "targets_per_seed": targets_per_seed,
        "aggregate": {k: v for k, v in agg.items() if k != "rows"},
        "all_rows": all_rows,
    }
    (HERE / "results_sweep.json").write_text(json.dumps(out, indent=2))
    a = out["aggregate"]
    print("\n" + "=" * 72)
    print(f"SWEEP (method C, seeds {seeds}, {len(all_rows)} targets)")
    print("=" * 72)
    print(f"overall   hit@40={a['hit_rate@40']*100:.1f}%  "
          f"med_err={_fmt(a['median_error_px'])}px")
    print(f"clean     n={a['clean_rows']['n']:2d} "
          f"hit@40={(a['clean_rows']['hit_rate@40'] or 0)*100:.1f}%  "
          f"med_err={_fmt(a['clean_rows']['median_error_px'])}px")
    print(f"collision n={a['ocr_collision_rows']['n']:2d} "
          f"hit@40={(a['ocr_collision_rows']['hit_rate@40'] or 0)*100:.1f}%  "
          f"med_err={_fmt(a['ocr_collision_rows']['median_error_px'])}px")


if __name__ == "__main__":
    import sys

    main()
    if "--sweep" in sys.argv:
        sweep(seeds=[1, 2, 3, 4, 5], targets_per_seed=10)
