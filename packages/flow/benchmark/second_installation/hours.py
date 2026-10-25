"""Engineer-hours ledger for a second installation (append-only JSONL).

Usage: python hours.py start <app> <step> [note]
       python hours.py stop  <app> <step> [note]
       python hours.py summary

Each start/stop pair is one timed span of hands-on work on one step. The
steps are setup, teach, contract, admission, soak and debugging. Time spent
waiting on a long unattended run is logged as its own step (`wait`), so it can
be counted or left out.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

LEDGER = Path(__file__).resolve().parent / "hours.jsonl"
STEPS = (
    "prep",
    "setup",
    "teach",
    "contract",
    "admission",
    "soak",
    "debugging",
    # Measurement apparatus only (the soak adapter, workload, oracle and
    # injected events). A customer installation does not pay this.
    "harness",
    "wait",
)


def _append(row: dict) -> None:
    with LEDGER.open("a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def summary() -> dict:
    open_spans: dict = {}
    minutes: dict = defaultdict(float)
    for line in LEDGER.read_text().splitlines():
        row = json.loads(line)
        key = (row["app"], row["step"])
        if row["event"] == "start":
            open_spans[key] = row["at"]
        elif key in open_spans:
            minutes[key] += (row["at"] - open_spans.pop(key)) / 60
    out: dict = defaultdict(dict)
    for (app, step), m in sorted(minutes.items()):
        out[app][step] = round(m, 1)
    for app in out:
        out[app]["total_hands_on"] = round(
            sum(v for k, v in out[app].items() if k != "wait"), 1
        )
    return {"minutes": dict(out), "open": [list(k) for k in open_spans]}


def main(argv: list[str]) -> None:
    if argv[:1] == ["summary"]:
        print(json.dumps(summary(), indent=2))
        return
    event, app, step, *note = argv
    assert event in ("start", "stop") and step in STEPS, argv
    _append({"at": time.time(), "event": event, "app": app, "step": step,
             "note": " ".join(note)})


if __name__ == "__main__":
    main(sys.argv[1:])
