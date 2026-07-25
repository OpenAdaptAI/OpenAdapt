#!/usr/bin/env python3
"""Benchmark Apple Vision vs tesseract for PowerChart mid-capture OCR.

Runs the same checks coord_replay uses (stop markers, pager, tab labels,
verified-local) on existing verified/*.png shots. Reports latency + agreement.

Usage:
  .venv/bin/python app-run/powerchart/scripts/bench_ocr.py \\
      --run app-run/powerchart/out/027-307-03 \\
      --run app-run/powerchart/kept/run-rec5-coord4 \\
      --reps 3
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import vision_ocr  # noqa: E402

PAGER_RE = re.compile(r"page\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)
CROPS = {
    "content": (600, 500, 2400, 1200),
    "pager": (600, 430, 2600, 1280),
    "strip": (600, 420, 2000, 520),
}
MARKERS = (
    "home medications",
    "important links",
    "recommendations",
    "verified local",
    "problem history",
    "procedure history",
    "population health record",
    "condition type",
)


def tesseract_text(path: Path, crop: tuple[int, int, int, int] | None = None) -> str:
    src = path
    tmp: Path | None = None
    try:
        if crop is not None:
            tmp = Path(tempfile.mkstemp(suffix=".png")[1])
            Image.open(path).crop(crop).save(tmp)
            src = tmp
        r = subprocess.run(
            ["tesseract", str(src), "stdout", "--psm", "6"],
            capture_output=True,
        )
        return (r.stdout or b"").decode("utf-8", errors="replace")
    except Exception as e:
        return f"__ERR__{e}"
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def vision_text(path: Path, crop: tuple[int, int, int, int] | None = None) -> str:
    return vision_ocr.ocr_text(path, crop=crop)


def detect_flags(text: str) -> dict[str, bool]:
    low = text.lower()
    flags = {m: m in low for m in MARKERS}
    m = PAGER_RE.search(low)
    flags["pager"] = bool(m)
    flags["pager_tuple"] = m.groups() if m else None  # type: ignore[assignment]
    return flags


def timed(fn, *args, reps: int = 3) -> tuple[list[float], object]:
    times: list[float] = []
    out = None
    for i in range(reps):
        t0 = time.perf_counter()
        out = fn(*args)
        times.append(time.perf_counter() - t0)
        # Drop first as warm-up for Vision's framework load.
        if i == 0 and reps > 1:
            times.clear()
            continue
    return times, out


def collect_shots(run: Path, limit: int) -> list[Path]:
    verified = run / "verified"
    if not verified.is_dir():
        # Some kept layouts nest differently.
        candidates = list(run.rglob("verified"))
        verified = candidates[0] if candidates else run
    shots: list[Path] = []
    for kind in ("allergies", "problem_history", "procedure_history"):
        d = verified / kind
        if not d.is_dir():
            continue
        for p in sorted(d.glob(f"{kind}_*.png")):
            if "_small" in p.name or "ocr_crop" in p.name:
                continue
            shots.append(p)
    # Prefer page-1 scroll frames + a few pager pages for variety.
    if len(shots) > limit:
        pager = [p for p in shots if "_p" in p.name]
        base = [p for p in shots if "_p" not in p.name]
        take = base[: max(1, limit - min(3, len(pager)))] + pager[:3]
        shots = take[:limit]
    return shots


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=Path, action="append", default=[])
    ap.add_argument("--reps", type=int, default=3, help="Timed reps per image (1st discarded)")
    ap.add_argument("--limit", type=int, default=12, help="Max shots per run")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("app-run/powerchart/out/OCR_BENCH.json"),
    )
    args = ap.parse_args()
    runs = args.run or [
        Path("app-run/powerchart/out/027-307-03"),
        Path("app-run/powerchart/kept/run-rec5-coord4"),
    ]

    # Warm Vision once so first-image cold start isn't charged to a shot.
    warm = None
    for run in runs:
        shots = collect_shots(run, 1)
        if shots:
            warm = shots[0]
            break
    if warm:
        vision_text(warm)

    rows: list[dict] = []
    print(f"{'shot':48} {'vis_ms':>8} {'tess_ms':>8} {'speedup':>8}  agreement")
    print("-" * 100)

    for run in runs:
        shots = collect_shots(run, args.limit)
        if not shots:
            print(f"WARN: no shots under {run}")
            continue
        print(f"\n== {run}  ({len(shots)} shots) ==")
        for path in shots:
            # Mid-capture path: content-pane crop (stop marker / secondary list).
            crop = CROPS["content"]
            vt, vout = timed(vision_text, path, crop, reps=args.reps)
            tt, tout = timed(tesseract_text, path, crop, reps=args.reps)
            vflags = detect_flags(str(vout))
            tflags = detect_flags(str(tout))
            agree_keys = [k for k in MARKERS] + ["pager"]
            agree = sum(1 for k in agree_keys if bool(vflags[k]) == bool(tflags[k]))
            disagree = [
                k for k in agree_keys if bool(vflags[k]) != bool(tflags[k])
            ]
            v_ms = 1000 * statistics.mean(vt) if vt else float("nan")
            t_ms = 1000 * statistics.mean(tt) if tt else float("nan")
            speedup = (t_ms / v_ms) if v_ms and v_ms > 0 else float("nan")
            label = f"{path.parent.name}/{path.name}"
            print(
                f"{label:48} {v_ms:7.0f}ms {t_ms:7.0f}ms {speedup:7.2f}x  "
                f"{agree}/{len(agree_keys)}"
                + (f"  disagree={disagree}" if disagree else "")
            )
            rows.append(
                {
                    "run": str(run),
                    "file": str(path),
                    "vision_ms": round(v_ms, 1),
                    "tesseract_ms": round(t_ms, 1),
                    "speedup": round(speedup, 2),
                    "agree": agree,
                    "agree_total": len(agree_keys),
                    "disagree": disagree,
                    "vision_flags": {k: vflags[k] for k in agree_keys},
                    "tesseract_flags": {k: tflags[k] for k in agree_keys},
                    "vision_pager": vflags.get("pager_tuple"),
                    "tesseract_pager": tflags.get("pager_tuple"),
                }
            )

    if not rows:
        print("No results.")
        return 1

    v_all = [r["vision_ms"] for r in rows]
    t_all = [r["tesseract_ms"] for r in rows]
    agree_all = sum(r["agree"] for r in rows)
    agree_tot = sum(r["agree_total"] for r in rows)
    summary = {
        "shots": len(rows),
        "reps_timed": max(1, args.reps - 1),
        "vision_ms_mean": round(statistics.mean(v_all), 1),
        "vision_ms_median": round(statistics.median(v_all), 1),
        "tesseract_ms_mean": round(statistics.mean(t_all), 1),
        "tesseract_ms_median": round(statistics.median(t_all), 1),
        "speedup_mean": round(statistics.mean(t_all) / statistics.mean(v_all), 2),
        "flag_agreement": f"{agree_all}/{agree_tot}",
        "flag_agreement_pct": round(100 * agree_all / agree_tot, 1),
        "rows": rows,
    }
    print("\n" + "=" * 60)
    print(
        f"Vision  mean={summary['vision_ms_mean']:.0f}ms  "
        f"median={summary['vision_ms_median']:.0f}ms"
    )
    print(
        f"Tesseract mean={summary['tesseract_ms_mean']:.0f}ms  "
        f"median={summary['tesseract_ms_median']:.0f}ms"
    )
    print(
        f"Speedup  {summary['speedup_mean']:.2f}x (tesseract/vision)  "
        f"flag agreement {summary['flag_agreement']} "
        f"({summary['flag_agreement_pct']}%)"
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
