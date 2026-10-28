"""Summarize results/<app>/runs.jsonl into results/<app>/summary.json.

python benchmark/skill_cache/summarize.py mockmed
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(app: str) -> int:
    rows = [
        json.loads(line)
        for line in (HERE / "results" / app / "runs.jsonl").read_text().splitlines()
        if line.strip()
    ]
    first = [r for r in rows if r["path"] == "agent"]
    later = [r for r in rows if r["path"] == "skill"]

    def med(values: list[float]) -> float:
        return round(statistics.median(values), 3) if values else 0.0

    summary = {
        "app": app,
        "trials": len(first),
        "model": first[0]["model"] if first else None,
        "engine_commit": sorted({r["engine_commit"] for r in rows}),
        "run1": {
            "runs": len(first),
            "admitted": sum(r["status"] == "admitted" for r in first),
            "verified": sum(r["verified"] for r in first),
            "model_calls_median": med([r["model_calls"] for r in first]),
            "cost_usd_median": med([r["cost_usd"] for r in first]),
            "cost_usd_each": [r["cost_usd"] for r in first],
            "agent_seconds_median": med([r["agent_seconds"] for r in first]),
            "seconds_median": med([r["seconds"] for r in first]),
            "seconds_each": [r["seconds"] for r in first],
        },
        "later": {
            "runs": len(later),
            "verified": sum(r["verified"] for r in later),
            "receipt_verified": sum(
                r.get("receipt_outcome") == "VERIFIED" for r in later
            ),
            "store_shows_record": sum(bool(r.get("store_shows_record")) for r in later),
            "model_calls_total": sum(r["model_calls"] for r in later),
            "cost_usd_total": round(sum(r["cost_usd"] for r in later), 6),
            "seconds_median": med([r["seconds"] for r in later]),
            "seconds_min": min((r["seconds"] for r in later), default=0.0),
            "seconds_max": max((r["seconds"] for r in later), default=0.0),
            "program_seconds_median": med([r["program_seconds"] for r in later]),
        },
        "host_load_1m": [
            min(r["host_load_1m_before"] for r in rows),
            max(r["host_load_1m_after"] for r in rows),
        ],
    }
    out = HERE / "results" / app / "summary.json"
    out.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
