#!/usr/bin/env python3
"""Batch PowerChart verified-data extract: one timed JSON per MRN.

Pipeline per patient:
  1. Coordinate open (header MRN → type → open chart) from template events
  2. Capture Allergies / Problem History / Procedure History screenshots
  3. Close chart
  4. Apple Vision OCR → details JSON with table columns
  5. Record wall-clock timings

Usage:
  # Smoke-test first patient
  .venv/bin/python app-run/powerchart/scripts/batch_run.py --limit 1

  # Full list
  .venv/bin/python app-run/powerchart/scripts/batch_run.py

  # Resume / subset
  .venv/bin/python app-run/powerchart/scripts/batch_run.py --mrn 083-151-63
  .venv/bin/python app-run/powerchart/scripts/batch_run.py --skip-existing
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # openadapt/
SCRIPTS = Path(__file__).resolve().parent
POWERCHART = SCRIPTS.parent

sys.path.insert(0, str(SCRIPTS))

import coord_replay  # noqa: E402
import vision_ocr  # noqa: E402


def mrn_slug(mrn: str) -> str:
    return mrn.strip().replace("/", "-")


def load_patients(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip().strip('"')
            mrn = (row.get("mrn") or "").strip()
            if mrn:
                rows.append({"name": name, "mrn": mrn})
    return rows


def run_patient(
    *,
    name: str,
    mrn: str,
    events: Path,
    out_root: Path,
    keep_shots: bool,
) -> dict:
    slug = mrn_slug(mrn)
    run_dir = out_root / slug
    json_path = run_dir / f"{slug}.json"
    timing: dict = {"mrn": mrn, "name": name}

    t0 = time.perf_counter()

    t_cap = time.perf_counter()
    summary = coord_replay.run_one(
        mrn=mrn,
        events_path=events,
        out=run_dir,
        skip_events=False,
        skip_sections=False,
        skip_close=False,
    )
    timing["capture_s"] = round(time.perf_counter() - t_cap, 2)
    status = summary.get("status") or "ok"
    timing["status"] = status
    timing["skip"] = summary.get("skip")
    timing["chart_opened"] = bool(summary.get("verified_sections"))
    timing["closed"] = summary.get("closed")

    t_ocr = time.perf_counter()
    if status in ("unauthorized", "chart_not_open", "screen_busy", "wrong_view"):
        reasons = {
            "unauthorized": (
                "No authorized lifetime relationships — doctor has no "
                "authorization to open this patient"
            ),
            "chart_not_open": "Chart did not open after open sequence",
            "screen_busy": (
                "Another app was covering the screen — Citrix never came "
                "to front; re-run when the machine is idle"
            ),
            "wrong_view": (
                "Organizer was not on the patient-search / list view "
                "(e.g. Message Center) — switch views and re-run"
            ),
        }
        details = {
            "mrn": mrn,
            "patient_name_expected": name,
            "status": status,
            "skip_reason": reasons.get(status, status),
            "skip": summary.get("skip"),
            "source_run": str(run_dir),
            "engine": "apple-vision",
            "tables": {},
            "timing": timing,
        }
        timing["ocr_s"] = 0.0
        timing["ok"] = False
        timing["skipped"] = status == "unauthorized"
    elif not (run_dir / "verified").is_dir():
        details = {
            "mrn": mrn,
            "patient_name_expected": name,
            "status": status or "chart_not_open",
            "skip_reason": "Chart did not open; no verified screenshots",
            "skip": summary.get("skip"),
            "source_run": str(run_dir),
            "engine": "apple-vision",
            "tables": {},
            "timing": timing,
        }
        timing["ocr_s"] = 0.0
        timing["ok"] = False
    else:
        details = vision_ocr.extract_details(run_dir, mrn=mrn)
        details["patient_name_expected"] = name
        details["status"] = status
        timing["ocr_s"] = round(time.perf_counter() - t_ocr, 2)
        tables = details.get("tables") or {}
        timing["tables"] = {
            k: {
                "row_count": v.get("row_count"),
                "verified_count_ui": v.get("verified_count_ui"),
                "complete": v.get("complete"),
            }
            for k, v in tables.items()
        }
        timing["ok"] = bool(timing["chart_opened"]) and all(
            t.get("complete") for t in timing["tables"].values()
        ) if timing["tables"] else False

    timing["total_s"] = round(time.perf_counter() - t0, 2)
    details["timing"] = timing

    json_path.write_text(json.dumps(details, indent=2))
    (run_dir / "timing.json").write_text(json.dumps(timing, indent=2))

    if not keep_shots:
        # Keep verified/ + dialog/ + json + timing; drop bulky per-event PNGs
        import shutil

        for drop in ("events", "close"):
            p = run_dir / drop
            if p.exists():
                shutil.rmtree(p)

    tag = {
        "unauthorized": "SKIP-AUTH",
        "screen_busy": "SKIP-BUSY",
        "wrong_view": "SKIP-VIEW",
    }.get(status, "ok" if timing.get("ok") else "fail")
    print(
        f"[{mrn}] {tag} total={timing['total_s']}s "
        f"capture={timing['capture_s']}s ocr={timing.get('ocr_s', 0)}s "
        f"→ {json_path}",
        flush=True,
    )
    return timing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--patients",
        type=Path,
        default=POWERCHART / "patients.csv",
    )
    ap.add_argument(
        "--events",
        type=Path,
        default=POWERCHART / "template" / "rec5" / "events.jsonl",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=POWERCHART / "out",
    )
    ap.add_argument("--limit", type=int, default=0, help="Process only first N patients")
    ap.add_argument("--mrn", action="append", default=[], help="Only these MRNs (repeatable)")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument(
        "--keep-shots",
        action="store_true",
        help="Keep event/close PNGs (default: keep verified/ only)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    patients = load_patients(args.patients)
    if args.mrn:
        want = set(args.mrn)
        patients = [p for p in patients if p["mrn"] in want]
    if args.limit and args.limit > 0:
        patients = patients[: args.limit]

    print(f"Patients: {len(patients)}  out={args.out}", flush=True)
    if args.dry_run:
        for p in patients:
            print(f"  {p['mrn']}  {p['name']}")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    timings: list[dict] = []
    batch_t0 = time.perf_counter()

    for i, p in enumerate(patients, 1):
        slug = mrn_slug(p["mrn"])
        json_path = args.out / slug / f"{slug}.json"
        print(f"\n=== [{i}/{len(patients)}] {p['name']}  MRN={p['mrn']} ===", flush=True)
        if args.skip_existing and json_path.exists():
            print(f"skip existing {json_path}", flush=True)
            try:
                timings.append(json.loads((args.out / slug / "timing.json").read_text()))
            except Exception:
                timings.append({"mrn": p["mrn"], "skipped": True})
            continue
        try:
            timings.append(
                run_patient(
                    name=p["name"],
                    mrn=p["mrn"],
                    events=args.events,
                    out_root=args.out,
                    keep_shots=args.keep_shots,
                )
            )
        except Exception as e:
            err = {
                "mrn": p["mrn"],
                "name": p["name"],
                "ok": False,
                "error": str(e),
                "traceback": traceback.format_exc(),
            }
            timings.append(err)
            err_dir = args.out / slug
            err_dir.mkdir(parents=True, exist_ok=True)
            (err_dir / "error.json").write_text(json.dumps(err, indent=2))
            print(f"ERROR {p['mrn']}: {e}", flush=True)

    batch_total = round(time.perf_counter() - batch_t0, 2)
    summary = {
        "patients": len(patients),
        "ok": sum(1 for t in timings if t.get("ok")),
        "unauthorized": sum(
            1 for t in timings if t.get("status") == "unauthorized" or t.get("skipped")
        ),
        "failed": sum(
            1
            for t in timings
            if t.get("ok") is False
            and not t.get("skipped")
            and t.get("status") != "unauthorized"
        ),
        "batch_total_s": batch_total,
        "avg_total_s": round(
            sum(t.get("total_s", 0) for t in timings if "total_s" in t)
            / max(1, sum(1 for t in timings if "total_s" in t)),
            2,
        ),
        "timings": timings,
    }
    (args.out / "BATCH_SUMMARY.json").write_text(json.dumps(summary, indent=2))

    # also a flat CSV for quick glance
    with (args.out / "BATCH_TIMING.csv").open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "mrn",
                "name",
                "ok",
                "total_s",
                "capture_s",
                "ocr_s",
                "chart_opened",
                "error",
            ],
            extrasaction="ignore",
        )
        w.writeheader()
        for t in timings:
            w.writerow(t)

    print(
        f"\nBatch done: {summary['ok']}/{summary['patients']} ok  "
        f"total={batch_total}s  avg={summary['avg_total_s']}s/pt  "
        f"→ {args.out / 'BATCH_SUMMARY.json'}",
        flush=True,
    )
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
