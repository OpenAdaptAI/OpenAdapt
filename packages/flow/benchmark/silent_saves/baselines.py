"""Baselines, rebuilt from rows the scoreboard already committed. No new runs.

    python -m silent_saves.baselines            # rewrite baselines.json
    python -m silent_saves.baselines --check    # fail if it is out of date

Sources, all under ``benchmark/scoreboard/raw/mockmed/`` (read, never
written):

* ``claims/2026-10-19-b``: Claude Sonnet 5.5 computer use alone, with the
  server breaking the save; ``verify-run`` judged each write afterwards. The
  same items give two baselines: the agent's own claim, and its claim gated
  on the read-back. Items 16-20 (``duplicate_keyed``) are left out: the page
  sent an idempotency key, so they are not in this fault set.
* ``agent/2026-10-18-a`` and ``program/2026-10-18-a``: scoreboard arms 1 and
  3 on the same 100-item batch. Only two items carry a fault (optimistic,
  wrong_record); the ``ui_change`` items are a button rename, not a bad save,
  so they are left out. The program arm's last attempt per item counts, as on
  the scoreboard.

Seconds come only from rows the scoreboard calls timed (1-minute host load at
or under 12); most of these rows were not, so most cells say "untimed".
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from . import judge, metrics, protocol

HERE = Path(__file__).resolve().parent
RAW = HERE.parent / "scoreboard" / "raw" / "mockmed"
OUT = HERE / "baselines.json"
TIMED_LOAD_MAX = 12.0  # scoreboard/schema.py
#: The scoreboard's read-back labels, in this benchmark's verdict terms.
_ARM_VERDICT = {"correct": "correct", "missing": "missing", "duplicate": "duplicate"}


def _claims_rows(gate_on_readback: bool) -> list[dict[str, Any]]:
    run = RAW / "claims" / "2026-10-19-b"
    rows = []
    for r in csv.DictReader((run / "items.csv").open()):
        if r["fault"] not in protocol.FAULTS:
            continue
        ledger = json.loads(
            (run / "ledgers" / f"item-{int(r['item']):03d}.json").read_text()
        )
        claimed = r["claimed_success"] == "True"
        reported = claimed and (
            not gate_on_readback or r["verify_outcome"] == "verified"
        )
        verdict = _claims_verdict(r["ground_truth"], r["fault"])
        timed = float(r["host_load_1m"]) <= TIMED_LOAD_MAX
        rows.append(
            {
                "item": int(r["item"]),
                "fault": r["fault"],
                "reported_success": reported,
                "store_verdict": verdict,
                "outcome": judge.outcome(
                    reported, verdict, store_changed=verdict != "missing"
                ),
                "item_s": round(ledger["finished_at"] - ledger["started_at"], 3)
                if timed
                else None,
                "cost_usd": float(r["cost_usd"]),
                "model_calls": int(r["model_calls"]),
            }
        )
    return rows


def _claims_verdict(ground_truth: str, fault: str) -> str:
    # claims.ground_truth says "wrong" for both a dropped note and a row on
    # another patient; the fault tells which.
    if ground_truth == "wrong":
        return "partial" if fault == "partial" else "wrong_record"
    return ground_truth


def _arm_rows(arm: str) -> list[dict[str, Any]]:
    path = RAW / arm / "2026-10-18-a" / "items.csv"
    finals: dict[int, dict[str, str]] = {}
    for r in csv.DictReader(path.open()):
        i = int(r["item"])
        if i not in finals or int(r["attempt"]) > int(finals[i]["attempt"]):
            finals[i] = r
    rows = []
    for i, r in sorted(finals.items()):
        fault = {"none": "ok"}.get(r["fault"], r["fault"])
        if fault not in protocol.FAULTS:
            continue
        reported = r["reported_success"] == "True"
        verdict = _ARM_VERDICT.get(r["readback"], "wrong_record")
        rows.append(
            {
                "item": i,
                "fault": fault,
                "reported_success": reported,
                "store_verdict": verdict,
                "outcome": judge.outcome(
                    reported, verdict, store_changed=verdict != "missing"
                ),
                "item_s": float(r["item_s"]) if r["timed"] == "True" else None,
                "cost_usd": float(r["cost_usd"]),
                "model_calls": int(r["model_calls"]),
            }
        )
    return rows


BASELINES = (
    (
        "agent_alone",
        "Agent alone (Claude Sonnet 5.5 computer use)",
        "scoreboard/raw/mockmed/claims/2026-10-19-b",
        lambda: _claims_rows(gate_on_readback=False),
    ),
    (
        "agent_with_readback",
        "Same agent, success gated on OpenAdapt's read-back (verify-run)",
        "scoreboard/raw/mockmed/claims/2026-10-19-b",
        lambda: _claims_rows(gate_on_readback=True),
    ),
    (
        "agent_alone_arm1",
        "Agent alone, scoreboard arm 1 (2 fault items)",
        "scoreboard/raw/mockmed/agent/2026-10-18-a",
        lambda: _arm_rows("agent"),
    ),
    (
        "openadapt_program",
        "OpenAdapt program, scoreboard arm 3 (2 fault items)",
        "scoreboard/raw/mockmed/program/2026-10-18-a",
        lambda: _arm_rows("program"),
    ),
)


def build() -> dict[str, Any]:
    out: dict[str, Any] = {
        "version": protocol.VERSION,
        "protocol": protocol.PROTOCOL,
        "note": "Rebuilt from committed scoreboard rows by baselines.py; no new runs.",
        "baselines": {},
    }
    for key, label, source, rows in BASELINES:
        summary = metrics.summarize(rows())
        out["baselines"][key] = {"label": label, "source": source, **summary}
    return out


def table(doc: dict[str, Any]) -> str:
    """The README's baseline table."""

    def frac(r: dict[str, Any]) -> str:
        return f"{r['k']} of {r['n']}" if r["n"] else "-"

    lines = [
        "| Executor | Bad saves reported as success | Clean saves done | "
        "Correct saves reported as failed | Seconds per item (median) | "
        "Cost per item | Source |",
        "|---|---|---|---|---|---|---|",
    ]
    for b in doc["baselines"].values():
        secs = b["seconds_per_item"]
        timed = f"{secs['median']} (n={secs['n']})" if secs["n"] else "untimed"
        lines.append(
            f"| {b['label']} | {frac(b['silent_wrong_save_rate'])} | "
            f"{frac(b['task_success'])} | {frac(b['false_alarm_rate'])} | "
            f"{timed} | ${b['cost_per_item_usd']:.4f} | `{b['source']}` |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    doc = build()
    text = json.dumps(doc, indent=2) + "\n"
    if args.check:
        if not OUT.is_file() or OUT.read_text() != text:
            print(
                "baselines.json is out of date; run python -m silent_saves.baselines",
                file=sys.stderr,
            )
            return 1
        return 0
    OUT.write_text(text)
    print(table(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
