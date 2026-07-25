# PowerChart verified-data batch extract

One JSON per MRN, timed. Coordinate replay + Apple Vision OCR (no tesseract).

## Layout

```
app-run/powerchart/
  patients.csv                 # MRN list
  template/rec5/events.jsonl   # open-chart gesture template
  scripts/
    batch_run.py               # main entry
    coord_replay.py            # open + section screenshots
    vision_ocr.py              # Apple Vision → table JSON
    pc_config.py               # site MRN pattern, layout fractions, labels
    bench_ocr.py               # Vision vs tesseract mid-capture bench
  golden/                      # frozen frames + expectations for pytest
  kept/                        # historical reference run
  out/                         # batch outputs: <mrn>/<mrn>.json
```

## Generalization knobs (`pc_config.py`)

| Concern | How it's handled |
|---------|------------------|
| Window size / Retina | `window_scale` = screenshot_px / window_points; Vision boxes converted via `px_to_screen` |
| Left-nav order | Click by OCR label (`Allergies`, `Histories`); pixel fallbacks only if OCR misses |
| Histories tabs | Same — Vision `"Problem History"` / `"Procedure History"` |
| Table columns | Header-derived x-bands per frame; falls back to 3440 defaults |
| Site MRN shape | `MRN_RE` in `pc_config` (default `###-###-##`) |
| Chart window | Title contains MRN or matches `… - MRN Opened by` — no patient-name literal |
| Wrong Organizer view | Preflight OCR; skips with `wrong_view` if Message Center etc. |

## Run

Requires PowerChart Organizer visible in Citrix Viewer (patient search / list view).

```bash
# Dry-run the patient list
.venv/bin/python app-run/powerchart/scripts/batch_run.py --dry-run

# Smoke-test first MRN
.venv/bin/python app-run/powerchart/scripts/batch_run.py --limit 1 --keep-shots

# Full list (resume-safe)
.venv/bin/python app-run/powerchart/scripts/batch_run.py --skip-existing

# One MRN
.venv/bin/python app-run/powerchart/scripts/batch_run.py --mrn 083-151-63
```

## Regression tests

```bash
.venv/bin/pytest tests/powerchart/ -q
```

`tests/powerchart/test_golden_extract.py` reparses `golden/*/verified` and
asserts row counts against `golden/expectations.json`. No Citrix required.

## Output per MRN

`out/<mrn>/<mrn>.json` — patient + demographics + three tables
(`allergies`, `problem_history`, `procedure_history`) with UI columns.

`out/<mrn>/timing.json` — `capture_s`, `ocr_s`, `total_s`.

`out/BATCH_SUMMARY.json` + `BATCH_TIMING.csv` — rollup.
