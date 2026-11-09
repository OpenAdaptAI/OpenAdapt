"""Import a worklist run of this harness into the scoreboard's rows.

    python -m benchmark.openemr_throughput.to_scoreboard \\
        --src OUT/TAG --arm program_fast|parallel --run-id ID

Reads the harness's ``summary.json``, ``items.json`` and each item's final
``report.json`` and ``host_load.json``, and writes
``scoreboard/raw/openemr_referral/<arm>/<run id>/items.csv`` plus a
``meta.json`` naming the source files and their SHA-256 (the scoreboard's
PROTOCOL.md). The outcome comes from the test setup's database read-back,
never from the engine:

- readback: ``correct`` (one new chart, 12 of 12 fields), ``missing`` (no
  chart), ``duplicate`` (more than one), ``wrong`` (one chart that doesn't
  match). The possible-duplicate item is fault ``duplicate_check``; no chart
  there is the right answer and counts as a safe stop.
- reported success: the engine's own verdict in the item's final report
  (after any pipelined read-back settled): the run succeeded and every
  record check it ran confirmed the save.
- item_s: engine time; wall_s: the item from claim to finish.
- An item the pool never started (the run stopped first) is ``paused``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

TASK = "openemr_referral"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _readback(item: dict[str, Any]) -> str:
    charts = int(item["new_charts"])
    if charts == 0:
        return "missing"
    if charts > 1:
        return "duplicate"
    return "correct" if item["correct"] else "wrong"


def _engine_verdict(report: dict[str, Any]) -> tuple[bool, float, str]:
    """(reported success, engine seconds, halt reason) from a final report."""
    checks = [
        r.get("effect_verified")
        for r in report.get("results", [])
        if r.get("effect_verified") is not None
    ]
    confirmed = bool(checks) and all(c is True for c in checks)
    ok = bool(report.get("success")) and confirmed
    halt = (report.get("halt") or {}).get("reason") or ""
    return ok, round((report.get("total_ms") or 0) / 1000, 3), halt


def rows_for(src: Path, arm: str, run_id: str) -> list[Any]:
    from scoreboard import schema

    items = json.loads((src / "items.json").read_text())
    out = []
    for item in items:
        fault = "duplicate_check" if item["duplicate_case"] else "none"
        item_dir = src / "pool" / "items" / f"{item['idx']:05d}"
        common = dict(
            task=TASK, arm=arm, run_id=run_id, item=int(item["idx"]) + 1,
            fault=fault, model_calls=0, input_tokens=0, output_tokens=0,
            cache_read_tokens=0, cache_write_tokens=0, cost_usd=0.0,
            approvals_asked=0,
        )
        if item["state"] == "pending":
            out.append(schema.Row(
                **common, attempt=0, reported_success=False, readback="not_checked",
                outcome="paused", item_s=0.0, wall_s=0.0, host_load_1m=-1.0,
                timed=False, detail="not started: the worklist stopped first",
            ))
            continue
        report_path = item_dir / "report.json"
        report = json.loads(report_path.read_text()) if report_path.is_file() else {}
        ok, engine_s, halt = _engine_verdict(report)
        load_path = item_dir / "host_load.json"
        load = json.loads(load_path.read_text()) if load_path.is_file() else {}
        start, end = load.get("start", -1.0), load.get("end")
        readback = _readback(item)
        outcome = schema.classify(ok, readback, fault)
        out.append(schema.Row(
            **common, attempt=1, reported_success=ok, readback=readback,
            outcome=outcome, item_s=engine_s, wall_s=round(float(item["wall_s"] or 0), 3),
            host_load_1m=start,
            timed=start >= 0 and end is not None and schema.timed(start, end),
            detail=(f"worker {item['worker']}; state {item['state']}; " + halt)[:300],
        ))
    return out


def main() -> int:
    from scoreboard import schema

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", required=True, type=Path, help="the harness's OUT/TAG")
    ap.add_argument("--arm", required=True, choices=["program", "program_fast", "parallel"])
    ap.add_argument("--run-id", required=True)
    a = ap.parse_args()
    summary = json.loads((a.src / "summary.json").read_text())
    out = schema.run_dir(TASK, a.arm, a.run_id)
    items_csv = out / "items.csv"
    if items_csv.is_file():
        items_csv.unlink()
    for row in rows_for(a.src, a.arm, a.run_id):
        schema.append(items_csv, row)
    schema.write_meta(
        out, task=TASK, arm=a.arm, imported_from=str(a.src),
        source_sha256={p: _sha(a.src / p) for p in ("summary.json", "items.json")},
        workers=summary["workers"], readback_window=summary["window"],
        processes=summary.get("processes"),
        speed={k: summary.get(k) for k in ("speed_profile", "native_fill", "page_quiet_ms", "ocr_threads")},
        harness_engine_sha=summary["engine_sha"], wall_s=summary["wall_s"],
        sign_in_s=summary["sign_in_s"], stop=summary["stop"],
        load1_mean=summary.get("load1_mean"),
    )
    print(f"wrote {items_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
