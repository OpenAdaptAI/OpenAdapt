"""Re-judge recorded OpenEMR claims items with another verify-run, on rebuilt state.

    python -m scoreboard.claims_rejudge --source 2026-10-19-a --run-id <id> \\
        --verify-flow <checkout>/packages/flow --faults ok,collateral_edit

The original run (``claims_openemr.py``) kept no records: verify-run's receipt
holds none, and the fixture was reset after every item. So each item's state
is rebuilt without the agent, the way the original item left it:

1. the fixture baseline is restored and the existing chart seeded;
2. verify-run takes its baseline;
3. the item's chart is saved through OpenEMR's patient API (the values the
   agent was given; the original read-back found them 12 of 12);
4. the item's fault is applied the same way the poller did;
5. the verify-run from ``--verify-flow`` judges it.

No model is called. The agent's claim is copied from the original row. Rows
say ``rebuilt_state``; they are a re-judgement, not a new agent run.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from scoreboard import claims_openemr as c  # noqa: E402
from scoreboard import schema  # noqa: E402
from scoreboard.tasks import openemr  # noqa: E402

FIELDS = (
    "item",
    "fault",
    "claimed_success",
    "ground_truth",
    "verify_effect_verdict",
    "verify_outcome",
    "verify_exit",
    "fault_applied",
    "collateral_change_checked",
    "rebuilt_state",
    "model_calls",
    "cost_usd",
    "host_load_1m",
    "final_text",
    "verify_stderr",
)


def _save_chart(values: dict[str, str]) -> None:
    fx = openemr.fixture()
    body = {k: v for k, v in values.items() if k != "state_label"}
    body["state"] = "MA"
    session = fx.token_session("actor")
    try:
        status = session.post(
            f"{fx.api_base_url}/apis/default/api/patient", json=body
        ).status_code
    finally:
        session.close()
    if status not in (200, 201):
        raise SystemExit(f"saving the item's chart returned HTTP {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--verify-flow", required=True, type=Path)
    parser.add_argument("--faults", default="ok,collateral_edit")
    args = parser.parse_args()

    src = schema.RAW / "openemr_referral" / "claims" / args.source
    out = schema.RAW / "openemr_referral" / "claims" / args.run_id
    work = schema.CACHE / "openemr_referral" / "claims" / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    faults = args.faults.split(",")
    rows = [
        r
        for r in csv.DictReader((src / "items.csv").open())
        if r["fault"] in faults and (r["fault"] == "ok" or r["fault_applied"])
    ]
    verify_head = schema.git_head(args.verify_flow)
    schema.write_meta(
        out,
        task="openemr_referral",
        arm="claims",
        source_run=args.source,
        rebuilt_state=True,
        verify_run_flow=str(args.verify_flow),
        verify_run_git_head=verify_head,
        note="re-judgement of recorded items on rebuilt fixture state; no model calls",
        faults=faults,
    )
    fx = openemr.fixture()
    verify_yaml = work / "verify.yaml"
    verify_yaml.write_text(
        c.VERIFY_YAML.format(api=fx.ui_base_url, token_env=c.TOKEN_ENV)
    )
    env_flow = {"PYTHONPATH": str(args.verify_flow)}
    out_rows = out / "items.csv"
    out_rows.unlink(missing_ok=True)
    try:
        for r in rows:
            i, fault = int(r["item"]), r["fault"]
            c.reset_and_seed()
            values = openemr.item_values(i)
            before = c._rows()
            item_dir = work / f"item-{i:03d}"
            # A fresh directory per attempt: verify-run marks a baseline used
            # (baseline.json.used), and an old receipt must never be re-read.
            shutil.rmtree(item_dir, ignore_errors=True)
            item_dir.mkdir(parents=True)
            params = item_dir / "params.json"
            params.write_text(json.dumps(values))
            baseline = item_dir / "baseline.json"
            baseline.unlink(missing_ok=True)
            env = {**c._token_env(), **env_flow}
            cap = c._verify(
                env, "--verify", str(verify_yaml), "--params", str(params),
                "--capture-baseline", str(baseline),
            )  # fmt: skip
            if cap.returncode != 0:
                raise SystemExit(f"baseline failed: {cap.stderr[-500:]}")
            _save_chart(values)
            applied = ""
            if fault == "collateral_edit":
                victim = min(int(k) for k in before)
                fx._db(
                    "UPDATE openemr.patient_data SET phone_home = '555-0199' "
                    f"WHERE id = {victim}"
                )
                applied = "collateral_edit (rebuilt)"
            truth = c.ground_truth(before, values)
            record = item_dir / "agent-run.json"
            record.write_text(
                json.dumps(
                    {
                        "executor_kind": "agent",
                        "executor_id": "claude-sonnet-5-5",
                        "claimed_success": r["claimed_success"] == "True",
                        "rebuilt_from": f"{args.source} item {i}",
                    }
                )
            )
            share = item_dir / "share"
            # A receipt left by an earlier run of this item must never be read
            # as this run's (verify-run refuses an existing --out).
            shutil.rmtree(share, ignore_errors=True)
            ver = c._verify(
                {**c._token_env(), **env_flow},
                "--verify", str(verify_yaml), "--params", str(params),
                "--run-record", str(record),
                "--authorization-ref", f"SCOREBOARD-REJUDGE-{args.run_id}-{i:03d}",
                "--baseline", str(baseline), "--out", str(share),
            )  # fmt: skip
            receipt_file = share / "effect-receipt.json"
            doc = (
                json.loads(receipt_file.read_text())
                if receipt_file.is_file() and ver.returncode in (0, 1)
                else {}
            )
            receipt = doc.get("receipt", {})
            checked = [
                e.get("collateral_change_checked")
                for e in doc.get("evidence", {}).get("readback", {}).get("effects", [])
            ]
            row = {
                "item": i,
                "fault": fault,
                "claimed_success": r["claimed_success"],
                "ground_truth": truth,
                "verify_effect_verdict": receipt.get("effect_verdict", "none"),
                "verify_outcome": receipt.get("outcome", "refused"),
                "verify_exit": ver.returncode,
                "fault_applied": applied if fault != "ok" else "",
                "collateral_change_checked": any(bool(x) for x in checked),
                "rebuilt_state": True,
                "model_calls": 0,
                "cost_usd": 0.0,
                "host_load_1m": schema.load_1m(),
                "final_text": r["final_text"],
                "verify_stderr": ver.stderr.strip()[-300:] if not receipt else "",
            }
            new = not out_rows.is_file()
            with out_rows.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
            print(
                f"rejudge {i} {fault:16s} truth={truth:16s} "
                f"verify={row['verify_outcome']} "
                f"checked={row['collateral_change_checked']}",
                flush=True,
            )
    finally:
        fx.reset()
    meta = json.loads((out / "meta.json").read_text())
    meta["complete"] = True
    (out / "meta.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
