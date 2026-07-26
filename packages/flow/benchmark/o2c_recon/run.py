"""O2C reconciliation benchmark harness: two systems + spreadsheet write-back.

Runs the reconciliation workflow program end-to-end through the REAL
:class:`~openadapt_flow.runtime.replayer.Replayer` (api actuation tier),
against TWO separate fixture applications (billing = system A, ledger =
system B) plus the shared-folder spreadsheets (exported worklist in, results
sheet written back and re-read), under two arms:

- ``naive``    -- demo profile; effect contracts read only the applications'
  own painted acknowledgement banners.
- ``governed`` -- sealed bundle admitted by the real Standard-profile gate,
  a single-use exact-input authorization, exact API identity contracts, and
  separate persisted-state verification per surface
  (read-only SQL over the ledger file, and the results CSV re-read from
  disk).

Every run is judged by the direct persisted-state adjudicator
(:mod:`benchmark.o2c_recon.ground_truth`).

Zero model calls; localhost only; all data synthetic.

Usage::

    python -m benchmark.o2c_recon.run            # write results.json
    python -m benchmark.o2c_recon.run --n 3      # trials per scenario/arm
    python -m benchmark.o2c_recon.run --print    # print, do not write
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from benchmark.multiapp_common import (
    CsvRecordVerifier,
    NullBackend,
    NullVision,
    SurfaceRoutedVerifier,
    standard_authorization,
)
from benchmark.o2c_recon import ground_truth
from benchmark.o2c_recon.fixtures import (
    BillingHandle,
    LedgerHandle,
    serve_billing,
    serve_ledger,
)
from benchmark.o2c_recon.workflow import (
    build_workflow,
    build_worklist,
    scenario_faults,
    scenario_orders,
)
from openadapt_flow.ir import Workflow
from openadapt_flow.runtime.actuators import ApiActuator
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.runtime.effects.sql import SqlRecordVerifier
from openadapt_flow.runtime.replayer import Replayer

HERE = Path(__file__).resolve().parent

ARMS = ("naive", "governed")
SCENARIOS = (
    "healthy",
    "missing_in_ledger",
    "ambiguous_duplicate",
    "stale_snapshot",
    "phantom_writeback",
)
DEFAULT_N = 3
EFFECT_TIMEOUT_S = 0.25


def _build_verifier(
    arm: str, billing: BillingHandle, ledger: LedgerHandle
) -> SurfaceRoutedVerifier:
    if arm == "naive":
        return SurfaceRoutedVerifier(
            {
                "ledger_banner": RestRecordVerifier(
                    ledger.base_url,
                    records_path="/api/ui/banner",
                    records_key="records",
                    timeout_s=EFFECT_TIMEOUT_S,
                    poll_interval_s=0.02,
                ),
                "billing_banner": RestRecordVerifier(
                    billing.base_url,
                    records_path="/api/ui/banner",
                    records_key="records",
                    timeout_s=EFFECT_TIMEOUT_S,
                    poll_interval_s=0.02,
                ),
            },
            default_surface="ledger_banner",
        )

    def _connect_ro() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{ledger.db_path}?mode=ro", uri=True, timeout=5.0)

    def sql(query: str) -> SqlRecordVerifier:
        return SqlRecordVerifier(
            _connect_ro, query, timeout_s=EFFECT_TIMEOUT_S, poll_interval_s=0.02
        )

    return SurfaceRoutedVerifier(
        {
            "ledger": sql(
                "SELECT id, order_id, customer, amount_posted, status "
                "FROM ledger_entries"
            ),
            "adjustments": sql(
                "SELECT id, order_id, delta, reason, status FROM adjustments"
            ),
            "results": CsvRecordVerifier(
                billing.results_path,
                timeout_s=EFFECT_TIMEOUT_S,
                poll_interval_s=0.02,
            ),
        },
        default_surface="ledger",
    )


def run_one(scenario: str, arm: str, index: int, work_root: Path) -> dict[str, Any]:
    work_dir = work_root / f"{scenario}-{arm}-{index}"
    work_dir.mkdir(parents=True, exist_ok=True)
    billing = serve_billing(
        work_dir / "billing.db", work_dir / "export", work_dir / "workbook"
    )
    ledger = serve_ledger(work_dir / "ledger.db")
    try:
        orders = scenario_orders(scenario)
        faults = scenario_faults(scenario)
        requests.post(
            f"{billing.base_url}/api/reset",
            json={"order_ids": orders, "faults": faults["billing"]},
            timeout=5.0,
        )
        requests.post(
            f"{ledger.base_url}/api/reset",
            json={"order_ids": orders, "faults": faults["ledger"]},
            timeout=5.0,
        )
        rows = build_worklist(billing.export_path, ledger.base_url)
        worklists = {"orders": rows}
        workflow_src = build_workflow(
            arm, billing_base=billing.base_url, processed=len(rows)
        )
        bundle_dir = work_dir / "bundle"
        (bundle_dir / "templates").mkdir(parents=True, exist_ok=True)
        workflow_src.save(bundle_dir)
        workflow = Workflow.load(bundle_dir)

        verifier = _build_verifier(arm, billing, ledger)
        api_actuator = ApiActuator(ledger.base_url, timeout_s=5.0)
        authorization = None
        if arm == "governed":
            authorization = standard_authorization(
                workflow,
                bundle_dir=bundle_dir,
                effect_verifier=verifier,
                api_actuator=api_actuator,
                params=None,
                worklists=worklists,
            )
        replayer = Replayer(
            NullBackend(),
            vision=NullVision(),
            effect_verifier=verifier,
            api_actuator=api_actuator,
            governed_authorization=authorization,
            durable=(arm == "governed"),
            require_settled=(arm == "governed"),
            poll_interval_s=0.01,
        )
        before = ground_truth.capture(
            billing.db_path, ledger.db_path, billing.results_path, billing.export_path
        )
        report = replayer.run(
            workflow,
            worklists=worklists,
            bundle_dir=bundle_dir,
            run_dir=work_dir / "out",
        )
        after = ground_truth.capture(
            billing.db_path, ledger.db_path, billing.results_path, billing.export_path
        )
        verdict = ground_truth.judge(
            scenario,
            before,
            after,
            completed=bool(report.success or report.execution_completed),
        )
        reported_success = bool(report.success)
        halted = not reported_success
        executed = [
            r for r in report.results if not r.skipped and not r.step_id.startswith("<")
        ]
        return {
            "scenario": scenario,
            "arm": arm,
            "i": index,
            "worklist_rows": len(rows),
            "executed_action_steps": len(executed),
            "actuation_kinds": sorted({str(r.actuation) for r in executed}),
            "reported_success": reported_success,
            "halted": halted,
            "execution_profile": report.execution_profile,
            "governed_policy_name": (
                Path(report.governed_policy_name).name
                if report.governed_policy_name
                else None
            ),
            "governed_approval_source": report.governed_approval_source,
            "execution_outcome": report.execution_outcome,
            "transaction_outcome": report.transaction_outcome,
            "transaction_billable": report.transaction_billable,
            "terminal_outcome": report.terminal_outcome,
            "model_calls": report.model_calls,
            "gt_violations": verdict.violations,
            "gt_correct": verdict.correct,
            "table_deltas": verdict.table_deltas,
            "silent_wrong": (not verdict.correct) and reported_success,
            "caught": (not verdict.correct) and halted,
            "safe_halt": verdict.correct and halted,
            "clean_success": verdict.correct and reported_success,
            "over_halt": scenario == "healthy" and halted and verdict.correct,
        }
    finally:
        billing.stop()
        ledger.stop()


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    per_arm: dict[str, Any] = {}
    for arm in ARMS:
        arm_rows = [r for r in rows if r["arm"] == arm]
        per_scenario: dict[str, Any] = {}
        for scenario in SCENARIOS:
            srows = [r for r in arm_rows if r["scenario"] == scenario]
            if not srows:
                continue
            per_scenario[scenario] = {
                "n": len(srows),
                "reported_success": sum(1 for r in srows if r["reported_success"]),
                "transaction_outcomes": sorted(
                    {str(r["transaction_outcome"]) for r in srows}
                ),
                "silent_wrong": sum(1 for r in srows if r["silent_wrong"]),
                "caught": sum(1 for r in srows if r["caught"]),
                "safe_halt": sum(1 for r in srows if r["safe_halt"]),
            }
        per_arm[arm] = {
            "n_runs": len(arm_rows),
            "verified": sum(
                1 for r in arm_rows if r["transaction_outcome"] == "VERIFIED"
            ),
            "completed_unverified": sum(
                1
                for r in arm_rows
                if r["transaction_outcome"] == "COMPLETED_UNVERIFIED"
            ),
            "halts": sum(1 for r in arm_rows if r["halted"]),
            "safe_halts": sum(1 for r in arm_rows if r["safe_halt"]),
            "caught": sum(1 for r in arm_rows if r["caught"]),
            "silent_wrong": sum(1 for r in arm_rows if r["silent_wrong"]),
            "over_halts": sum(1 for r in arm_rows if r["over_halt"]),
            "model_calls": sum(int(r["model_calls"] or 0) for r in arm_rows),
            "per_scenario": per_scenario,
        }
    return {"per_arm": per_arm}


def run_benchmark(
    n: int = DEFAULT_N, *, log: Callable[[str], None] = print
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="o2c-recon-") as tmp:
        work_root = Path(tmp)
        for arm in ARMS:
            for scenario in SCENARIOS:
                for i in range(n):
                    rows.append(run_one(scenario, arm, i, work_root))
                last = rows[-1]
                log(
                    f"{arm:9s} {scenario:22s} "
                    f"outcome={last['transaction_outcome']:24s} "
                    f"gt_ok={last['gt_correct']!s:5s} "
                    f"silent_wrong={last['silent_wrong']}"
                )
    metrics = aggregate(rows)
    governed = metrics["per_arm"]["governed"]
    naive = metrics["per_arm"]["naive"]
    healthy = [r for r in rows if r["scenario"] == "healthy" and r["arm"] == "governed"]
    return {
        "benchmark": "o2c_recon",
        "instrument": (
            "order-to-cash billing reconciliation across two systems: "
            "exported spreadsheet -> compare -> ledger adjustments (UI-only "
            "gateway) -> reconcile marks (API) -> results spreadsheet "
            "write-back, end-to-end through the real replayer's api tier"
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "n_per_scenario": n,
        "arms": list(ARMS),
        "scenarios": list(SCENARIOS),
        "workflow_shape": {
            "applications": ["billing (system A)", "ledger (system B)"],
            "modalities": [
                "csv spreadsheet in (exported worklist)",
                "csv spreadsheet out (written-back results, re-read from disk)",
                "rest api",
                "ui gateway (no adjustment api)",
                "two sqlite systems of record",
            ],
            "healthy_worklist_rows": healthy[0]["worklist_rows"] if healthy else None,
            "healthy_executed_action_steps": (
                healthy[0]["executed_action_steps"] if healthy else None
            ),
            "branches": ["disposition (match vs adjust vs missing)"],
            "exception_paths": [
                "order missing in the ledger (explicit halt terminal)",
                "ambiguous duplicate ledger entries (halt at UI gateway)",
                "stale reconciliation snapshot (optimistic-concurrency halt)",
                "phantom results-sheet write (governed: caught; naive: silent)",
            ],
        },
        "headline": {
            "governed_verified": governed["verified"],
            "governed_silent_wrong": governed["silent_wrong"],
            "governed_over_halts": governed["over_halts"],
            "naive_silent_wrong": naive["silent_wrong"],
            "model_calls_total": governed["model_calls"] + naive["model_calls"],
        },
        "metrics": metrics,
        "runs": rows,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument("--out", default=str(HERE))
    parser.add_argument("--print", action="store_true", dest="print_only")
    args = parser.parse_args(argv)
    results = run_benchmark(n=args.n)
    if args.print_only:
        json.dump(results, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    h = results["headline"]
    print(
        f"\no2c_recon  governed: verified={h['governed_verified']} "
        f"silent_wrong={h['governed_silent_wrong']} "
        f"over_halts={h['governed_over_halts']}  "
        f"naive: silent_wrong={h['naive_silent_wrong']}  "
        f"model_calls={h['model_calls_total']}"
    )
    print(f"Wrote results.json under {out_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
