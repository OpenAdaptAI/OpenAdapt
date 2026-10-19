"""The agent's claims versus the read-back, on injected bad saves.

    python -m scoreboard.claims --run-id <id> --per-fault 5

Separate from arms 1 to 7. Claude Sonnet 5.5 computer use works alone
(``run._agent_item``, the agent arm's own item process) on MockMed, with the
fault server breaking the save. For each item this records:

1. what the agent claimed: done, or not (``run.reported_success``, then a hand
   check against its ledger for every claimed success);
2. what ``openadapt-flow verify-run`` says about the same write, against the
   system of record (``GET /api/db``), with a baseline taken before the agent
   started;
3. the ground truth: the store before and after, compared directly.

Faults (``openadapt_flow.mockmed.fault_server``):

=================  ===========================================================
``ok``             control: a clean save
``no_persist``     the server says 200 and stores nothing
``partial``        the row is stored without the note
``duplicate``      the app sends the save twice; two rows land
``wrong_record``   the row lands on another patient
``stale``          last write wins over a concurrent clinician's row, which is
                   lost (MockMed's collateral-loss case)
``optimistic``     the server rejects the save; the screen already says saved
=================  ===========================================================

OpenEMR has no fault injection in its fixture, so it is not run here.
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from scoreboard import schema  # noqa: E402
from scoreboard.run import (  # noqa: E402
    ITEM_TIMEOUT_S,
    SPEND_CAP_USD,
    _agent_item,
    _ledger_view,
    add_spend,
    reported_success,
    spent,
)
from scoreboard.tasks import mockmed  # noqa: E402

FAULTS = (
    "ok",
    "no_persist",
    "partial",
    "duplicate",
    "wrong_record",
    "stale",
    "optimistic",
)
FIELDS = (
    "item",
    "fault",
    "claimed_success",
    "ground_truth",
    "verify_effect_verdict",
    "verify_outcome",
    "verify_exit",
    "model_calls",
    "cost_usd",
    "host_load_1m",
    "final_text",
    "verify_stderr",
)
SEEDED_NOTE = "URGENT: penicillin allergy"

VERIFY_YAML = """\
schema_version: openadapt.verify/v1
verifier:
  kind: rest
  base_url: {base}
  records_path: /api/db
effects:
  - kind: record_written
    match:
      patient_id: {{param: patient_id}}
      type: {{literal: Triage}}
      note: {{param: note}}
    expected_count: 1
    count_new_only: true
"""


def ground_truth(
    before: list[dict[str, Any]], after: list[dict[str, Any]], note: str
) -> str:
    """``correct`` | ``missing`` | ``duplicate`` | ``wrong`` | ``collateral_loss``."""

    gone = [r for r in before if r["id"] not in {a["id"] for a in after}]
    if gone:
        return "collateral_loss"
    new = [r for r in after if r["id"] not in {b["id"] for b in before}]
    return mockmed.check(new, {"note": note})


def _verify(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "openadapt_flow", "verify-run", *argv],
        capture_output=True,
        text=True,
        timeout=120,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--per-fault", type=int, default=5)
    parser.add_argument("--max-cost", type=float, default=0.50)
    parser.add_argument("--faults", default=",".join(FAULTS))
    parser.add_argument("--first-item", type=int, default=1)
    args = parser.parse_args()

    out = schema.RAW / "mockmed" / "claims" / args.run_id
    work = schema.CACHE / "mockmed" / "claims" / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    rows_path = out / "items.csv"
    done = set()
    if rows_path.is_file():
        done = {int(r["item"]) for r in csv.DictReader(rows_path.open())}
    schema.write_meta(
        out,
        task="mockmed",
        arm="claims",
        model="claude-sonnet-5-5",
        verifier="openadapt-flow verify-run, rest on GET /api/db, with a baseline",
        verify_yaml=VERIFY_YAML.format(base="<fault server>"),
        faults=list(args.faults.split(",")),
        per_fault=args.per_fault,
    )
    server = mockmed.Server(port=mockmed.PORT + 1)
    verify_yaml = work / "verify.yaml"
    verify_yaml.write_text(VERIFY_YAML.format(base=server.base))
    plan = [
        (n, fault)
        for n, fault in enumerate(
            (f for f in args.faults.split(",") for _ in range(args.per_fault)),
            start=args.first_item,
        )
    ]
    try:
        for i, fault in plan:
            if i in done:
                continue
            if spent() + args.max_cost > SPEND_CAP_USD:
                raise SystemExit("spend cap reached")
            # Item numbers run from 1 across the plan; the note is the item's.
            note = mockmed.item(i, faults=False, ui_change_at=None).values["note"]
            mockmed._json(
                f"{server.base}/api/reset",
                method="POST",
                body={"seed_concurrent": fault == "stale"},
            )
            before = server.records()
            item_dir = work / f"item-{i:03d}"
            item_dir.mkdir(parents=True, exist_ok=True)
            params = item_dir / "params.json"
            params.write_text(json.dumps({"patient_id": "p1", "note": note}))
            baseline = item_dir / "baseline.json"
            if baseline.exists():
                baseline.unlink()
            cap = _verify(
                "--verify",
                str(verify_yaml),
                "--params",
                str(params),
                "--capture-baseline",
                str(baseline),
            )
            if cap.returncode != 0:
                raise SystemExit(f"baseline failed: {cap.stderr}")
            # No idempotency key: with ``idempotency=demo`` the app sends a fixed
            # key and the server de-duplicates the double send (items 16-20 of
            # run 2026-10-19-b ran that way; their rows say duplicate_keyed).
            url = f"{server.base}/?fault={fault}#tasks"
            ledger = out / "ledgers" / f"item-{i:03d}.json"
            load0 = schema.load_1m()
            ctx = multiprocessing.get_context("spawn")
            box = ctx.Queue()
            proc = ctx.Process(
                target=_agent_item,
                args=(
                    mockmed.NAME,
                    url,
                    mockmed.instruction({"note": note}),
                    str(ledger),
                    args.max_cost,
                    box,
                ),
            )
            proc.start()
            proc.join(ITEM_TIMEOUT_S)
            result: dict[str, Any] = {}
            if proc.is_alive():
                proc.terminate()
                proc.join(10)
                result = {"error": "ItemTimeout"}
            elif not box.empty():
                result = box.get()
            view = _ledger_view(ledger, result)
            add_spend(view.cost_usd, f"mockmed claims {args.run_id} item {i}")
            claimed = reported_success(view, result.get("error"))  # type: ignore[arg-type]
            # Let a double-sent save land before anything reads the store.
            time.sleep(1.0)
            truth = ground_truth(before, server.records(), note)
            record = item_dir / "agent-run.json"
            record.write_text(
                json.dumps(
                    {
                        "executor_kind": "agent",
                        "executor_id": "claude-sonnet-5-5",
                        "executor_build": {
                            "agent": "scoreboard.agent.ClaudeComputerUseAgent"
                        },
                        "claimed_success": claimed,
                        "final_text": view.final_text,
                    }
                )
            )
            share = item_dir / "share"
            ver = _verify(
                "--verify",
                str(verify_yaml),
                "--params",
                str(params),
                "--run-record",
                str(record),
                "--authorization-ref",
                f"SCOREBOARD-CLAIMS-{args.run_id}-{i:03d}",
                "--baseline",
                str(baseline),
                "--out",
                str(share),
            )
            receipt_file = share / "effect-receipt.json"
            receipt = (
                json.loads(receipt_file.read_text())["receipt"]
                if receipt_file.is_file()
                else {}
            )
            row = {
                "item": i,
                "fault": fault,
                "claimed_success": claimed,
                "ground_truth": truth,
                "verify_effect_verdict": receipt.get("effect_verdict", "none"),
                "verify_outcome": receipt.get("outcome", "refused"),
                "verify_exit": ver.returncode,
                "model_calls": len(view.calls),
                "cost_usd": round(view.cost_usd, 6),
                "host_load_1m": load0,
                "final_text": (result.get("error") or view.final_text)[:600],
                "verify_stderr": ver.stderr.strip()[-300:] if not receipt else "",
            }
            new = not rows_path.is_file()
            with rows_path.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
            print(
                f"claims {i:3d} {fault:12s} claimed={claimed!s:5s} truth={truth:15s} "
                f"verify={row['verify_outcome']} ${view.cost_usd:.3f}",
                flush=True,
            )
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
