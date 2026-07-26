"""AP invoice benchmark harness: email + PDF + two apps + 3-way match.

Runs the complete AP intake workflow program end-to-end through the REAL
:class:`~openadapt_flow.runtime.replayer.Replayer` (api actuation tier), under
two arms:

- ``naive``    -- demo profile, no governed authorization, and effect
  contracts that read only the applications' OWN painted acknowledgement
  banners (what a screen-echo automation trusts).
- ``governed`` -- sealed bundle admitted by the real Standard-profile gate,
  a single-use exact-input authorization, exact API identity contracts on
  every consequential write, and separate persisted-state
  effect verification routed per record surface (read-only SQL over the ERP
  file, the payments REST oracle, and the OUTBOX maildir file oracle), plus
  the adjacent-record collateral guard and at-most-once idempotency.

Every run is judged by the direct persisted-state adjudicator
(:mod:`benchmark.ap_invoice.ground_truth`): direct SQLite file reads plus a
direct maildir read, with its own invariant logic and per-table delta audit.

Zero model calls; localhost only; all data synthetic.

Usage::

    python -m benchmark.ap_invoice.run            # write results.json
    python -m benchmark.ap_invoice.run --n 3      # trials per scenario/arm
    python -m benchmark.ap_invoice.run --print    # print, do not write
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

from benchmark.ap_invoice import ground_truth
from benchmark.ap_invoice.fixtures import (
    ADJACENT_INVOICE,
    ErpHandle,
    MailerHandle,
    serve_erp,
    serve_mailer,
)
from benchmark.ap_invoice.workflow import (
    build_workflow,
    build_worklist,
    scenario_faults,
    scenario_invoices,
    scenario_seed_duplicate,
    seed_inbox,
)
from benchmark.multiapp_common import (
    NullBackend,
    NullVision,
    SurfaceRoutedVerifier,
    standard_authorization,
)
from openadapt_flow.ir import Workflow
from openadapt_flow.runtime.actuators import ApiActuator
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.runtime.effects.file_arrival import FileArrivalVerifier
from openadapt_flow.runtime.effects.sql import SqlRecordVerifier
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.transaction import IdempotencyLedger

HERE = Path(__file__).resolve().parent

ARMS = ("naive", "governed")
SCENARIOS = (
    "healthy",
    "missing_po",
    "duplicate_invoice",
    "collateral_approve",
    "payment_confirm_outage",
)
DEFAULT_N = 3
EFFECT_TIMEOUT_S = 0.25


def _build_verifier(
    arm: str, erp: ErpHandle, mailer: MailerHandle
) -> SurfaceRoutedVerifier:
    if arm == "naive":
        return SurfaceRoutedVerifier(
            {
                "banner": RestRecordVerifier(
                    erp.base_url,
                    records_path="/api/ui/banner",
                    records_key="records",
                    timeout_s=EFFECT_TIMEOUT_S,
                    poll_interval_s=0.02,
                ),
                "mail_banner": RestRecordVerifier(
                    mailer.base_url,
                    records_path="/api/ui/banner",
                    records_key="records",
                    timeout_s=EFFECT_TIMEOUT_S,
                    poll_interval_s=0.02,
                ),
            },
            default_surface="banner",
        )

    def _connect_ro() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{erp.db_path}?mode=ro", uri=True, timeout=5.0)

    def sql(query: str) -> SqlRecordVerifier:
        return SqlRecordVerifier(
            _connect_ro,
            query,
            timeout_s=EFFECT_TIMEOUT_S,
            poll_interval_s=0.02,
        )

    return SurfaceRoutedVerifier(
        {
            "invoices": sql(
                "SELECT id, invoice_id, vendor_id, po_number, amount, "
                "doc_sha256, status, discount_applied, amount_payable "
                "FROM invoices"
            ),
            "exceptions": sql("SELECT id, invoice_id, reason FROM ap_exceptions"),
            "batches": sql("SELECT id, batch_id, processed FROM batches"),
            "payments": RestRecordVerifier(
                erp.base_url,
                records_path="/api/payments",
                records_key="records",
                timeout_s=EFFECT_TIMEOUT_S,
                poll_interval_s=0.02,
            ),
            "outbox": FileArrivalVerifier(
                mailer.outbox / "new",
                pattern="*.eml",
                content_probe=r"X-OpenAdapt-Invoice: ",
            ),
        },
        default_surface="invoices",
    )


def _one_replay(
    workflow_src: Workflow,
    *,
    arm: str,
    erp: ErpHandle,
    mailer: MailerHandle,
    worklists: dict[str, list[dict[str, str]]],
    work_dir: Path,
    tag: str,
    ledger: Optional[IdempotencyLedger],
    idempotency_key: Optional[str],
) -> Any:
    """Seal, authorize (governed arm), and run once through the real replayer."""
    bundle_dir = work_dir / f"bundle-{tag}"
    (bundle_dir / "templates").mkdir(parents=True, exist_ok=True)
    workflow_src.save(bundle_dir)
    workflow = Workflow.load(bundle_dir)

    verifier = _build_verifier(arm, erp, mailer)
    api_actuator = ApiActuator(erp.base_url, timeout_s=5.0)
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
        idempotency_ledger=ledger,
        poll_interval_s=0.01,
    )
    return replayer.run(
        workflow,
        worklists=worklists,
        bundle_dir=bundle_dir,
        run_dir=work_dir / f"out-{tag}",
        idempotency_key=idempotency_key,
    )


def run_one(scenario: str, arm: str, index: int, work_root: Path) -> dict[str, Any]:
    """One end-to-end (scenario x arm) trial with fresh fixture applications."""
    work_dir = work_root / f"{scenario}-{arm}-{index}"
    work_dir.mkdir(parents=True, exist_ok=True)
    erp = serve_erp(work_dir / "erp.db")
    mailer = serve_mailer(work_dir / "mail")
    try:
        requests.post(
            f"{erp.base_url}/api/reset",
            json={
                "faults": scenario_faults(scenario),
                "seed_duplicate": scenario_seed_duplicate(scenario),
            },
            timeout=5.0,
        )
        seed_inbox(mailer.inbox, scenario_invoices(scenario))
        rows = build_worklist(mailer.inbox, erp.base_url)
        worklists = {"invoices": rows}
        workflow_src = build_workflow(
            arm,
            mailer_base=mailer.base_url,
            adjacent_invoice=ADJACENT_INVOICE,
            processed=len(rows),
        )

        before = ground_truth.capture(erp.db_path, mailer.outbox)
        ledger: Optional[IdempotencyLedger] = None
        idempotency_key: Optional[str] = None
        if scenario == "payment_confirm_outage":
            ledger = IdempotencyLedger(work_dir / "idempotency.json")
            idempotency_key = f"ap-batch-{scenario}-{index}"

        report = _one_replay(
            workflow_src,
            arm=arm,
            erp=erp,
            mailer=mailer,
            worklists=worklists,
            work_dir=work_dir,
            tag="run1",
            ledger=ledger,
            idempotency_key=idempotency_key,
        )

        retry_outcome: Optional[str] = None
        retry_suppressed: Optional[bool] = None
        if scenario == "payment_confirm_outage":
            # The no-blind-retry contract: re-running under the SAME
            # idempotency key must be SUPPRESSED, never re-actuated.
            retry = _one_replay(
                workflow_src,
                arm=arm,
                erp=erp,
                mailer=mailer,
                worklists=worklists,
                work_dir=work_dir,
                tag="run2",
                ledger=IdempotencyLedger(work_dir / "idempotency.json"),
                idempotency_key=idempotency_key,
            )
            retry_outcome = retry.transaction_outcome
            retry_suppressed = bool(retry.idempotent_replay)

        after = ground_truth.capture(erp.db_path, mailer.outbox)
        verdict = ground_truth.judge(
            scenario,
            before,
            after,
            outbox_dir=mailer.outbox,
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
            "retry_transaction_outcome": retry_outcome,
            "retry_suppressed": retry_suppressed,
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
        erp.stop()
        mailer.stop()


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
            "reconciliation_required": sum(
                1
                for r in arm_rows
                if r["transaction_outcome"] == "RECONCILIATION_REQUIRED"
            ),
            "suppressed_retries": sum(1 for r in arm_rows if r.get("retry_suppressed")),
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
    with tempfile.TemporaryDirectory(prefix="ap-invoice-") as tmp:
        work_root = Path(tmp)
        for arm in ARMS:
            for scenario in SCENARIOS:
                for i in range(n):
                    rows.append(run_one(scenario, arm, i, work_root))
                last = rows[-1]
                log(
                    f"{arm:9s} {scenario:24s} "
                    f"outcome={last['transaction_outcome']:24s} "
                    f"gt_ok={last['gt_correct']!s:5s} "
                    f"silent_wrong={last['silent_wrong']}"
                )
    metrics = aggregate(rows)
    governed = metrics["per_arm"]["governed"]
    naive = metrics["per_arm"]["naive"]
    healthy = [r for r in rows if r["scenario"] == "healthy" and r["arm"] == "governed"]
    return {
        "benchmark": "ap_invoice",
        "instrument": (
            "multi-application AP invoice intake: email + PDF + ERP 3-way "
            "match + payments + exception queue, end-to-end through the real "
            "replayer's api tier"
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "n_per_scenario": n,
        "arms": list(ARMS),
        "scenarios": list(SCENARIOS),
        "workflow_shape": {
            "applications": ["erp", "mail_gateway"],
            "modalities": [
                "maildir email in/out",
                "pdf document",
                "rest api",
                "ui gateway (no entry api)",
                "sqlite system of record",
            ],
            "healthy_worklist_rows": healthy[0]["worklist_rows"] if healthy else None,
            "healthy_executed_action_steps": (
                healthy[0]["executed_action_steps"] if healthy else None
            ),
            "branches": [
                "match route (ok vs mismatch)",
                "discount (eligible vs expired)",
            ],
            "exception_paths": [
                "missing purchase order (halt at entry)",
                "ambiguous duplicate invoice (halt at entry)",
                "collateral adjacent-record overwrite (governed: caught)",
                "payment confirmation outage (governed: RECONCILIATION_REQUIRED "
                "+ suppressed retry)",
            ],
        },
        "headline": {
            "governed_verified": governed["verified"],
            "governed_silent_wrong": governed["silent_wrong"],
            "governed_over_halts": governed["over_halts"],
            "governed_reconciliation_required": governed["reconciliation_required"],
            "governed_suppressed_retries": governed["suppressed_retries"],
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
        f"\nap_invoice  governed: verified={h['governed_verified']} "
        f"silent_wrong={h['governed_silent_wrong']} "
        f"over_halts={h['governed_over_halts']} "
        f"reconciliation={h['governed_reconciliation_required']}  "
        f"naive: silent_wrong={h['naive_silent_wrong']}  "
        f"model_calls={h['model_calls_total']}"
    )
    print(f"Wrote results.json under {out_dir}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
