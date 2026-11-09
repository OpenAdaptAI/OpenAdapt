"""Derive every reported figure from the raw run files.

    python summarize.py --run-id launch-4

Writes ``results/<run id>/summary.json`` and prints a Markdown table. The
README and the video take their numbers from here, so a figure can always be
traced to ``agent_ledger.json``, ``agent_run.json`` and ``batch.csv``.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _q(values: list[float], p: float) -> float:
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def summarize(run: Path) -> dict:
    ledger = json.loads((run / "agent_ledger.json").read_text())
    agent = json.loads((run / "agent_run.json").read_text())
    rows = list(csv.DictReader((run / "batch.csv").open()))
    by_record: dict[int, list[dict]] = {}
    for r in rows:
        by_record.setdefault(int(r["record"]), []).append(r)
    finals = {k: v[-1] for k, v in by_record.items()}
    firsts = {k: v[0] for k, v in by_record.items()}

    def ok(r: dict) -> bool:
        return r["success"] == "True" and r["readback_correct"] == "True"

    saved = [r for r in finals.values() if ok(r)]
    stopped = [r for r in finals.values() if r["success"] != "True"]
    wrong = [
        r
        for r in finals.values()
        if r["success"] == "True" and r["readback_correct"] != "True"
    ]
    written_by_stopped = [r for r in stopped if int(r["readback_rows"]) != 0]
    t_saved = [float(r["program_s"]) for r in saved]
    before = [k for k, r in firsts.items() if r["ui_changed"] != "True"]
    after = [k for k, r in firsts.items() if r["ui_changed"] == "True"]
    halts = Counter(
        (r["halted_step"], r["halt_reason"].split(":")[0][:80])
        for r in rows
        if r["success"] != "True"
    )
    agent_wall = ledger["finished_at"] - ledger["started_at"]
    return {
        "agent": {
            "model": ledger["model"],
            "tool": ledger["tool"],
            "wall_s": round(agent_wall, 2),
            "model_calls": len(ledger["calls"]),
            "actions": sum(len(c["actions"]) for c in ledger["calls"]),
            "cost_usd": round(sum(c["cost_usd"] for c in ledger["calls"]), 4),
            "input_tokens": sum(c["input_tokens"] for c in ledger["calls"]),
            "output_tokens": sum(c["output_tokens"] for c in ledger["calls"]),
            "cache_read_tokens": sum(
                c["cache_read_input_tokens"] for c in ledger["calls"]
            ),
            "cache_write_tokens": sum(
                c["cache_creation_input_tokens"] for c in ledger["calls"]
            ),
            "claim_correct_on_read_back_afterwards": agent["record_correct"],
            "checked_its_own_save": False,
        },
        "program": {
            "claims": len(finals),
            "attempts": len(rows),
            "model_calls": sum(int(r["model_calls"]) for r in rows),
            "saved_and_read_back": len(saved),
            "first_attempt_saved": sum(1 for r in firsts.values() if ok(r)),
            "stopped_safely": len(stopped),
            "stopped_with_a_row_written": len(written_by_stopped),
            "saved_but_read_back_wrong": len(wrong),
            "time_per_saved_claim_s": {
                "median": round(statistics.median(t_saved), 2) if t_saved else None,
                "p10": round(_q(t_saved, 0.1), 2) if t_saved else None,
                "p90": round(_q(t_saved, 0.9), 2) if t_saved else None,
                "n": len(t_saved),
            },
            "before_ui_change": {
                "claims": len(before),
                "saved": sum(1 for k in before if ok(finals[k])),
            },
            "after_ui_change": {
                "claims": len(after),
                "saved": sum(1 for k in after if ok(finals[k])),
            },
            "halts_by_step": [
                {"step": s, "reason": why, "attempts": n}
                for (s, why), n in halts.most_common()
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    run = HERE / "results" / args.run_id
    s = summarize(run)
    (run / "summary.json").write_text(json.dumps(s, indent=2) + "\n")
    a, p = s["agent"], s["program"]
    t = p["time_per_saved_claim_s"]
    print("| | Agent (once) | Compiled program |")
    print("|---|---|---|")
    print(
        f"| Time | {a['wall_s']} s | median {t['median']} s per saved claim (n={t['n']}) |"
    )
    print(f"| Model calls | {a['model_calls']} | {p['model_calls']} |")
    print(f"| Cost (list price) | ${a['cost_usd']} | $0 |")
    print(
        f"| Save checked against the database | no (correct when we read it "
        f"afterwards: {a['claim_correct_on_read_back_afterwards']}) | "
        f"{p['saved_and_read_back']} of {p['claims']} saved and read back |"
    )
    print(
        f"| Stopped before saving | - | {p['stopped_safely']} (rows written: "
        f"{p['stopped_with_a_row_written']}) |"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
