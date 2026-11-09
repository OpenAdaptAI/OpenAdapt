"""Route recorded stops through the default repair loop's rules, offline.

    python benchmark/auto_repair/route_recorded_stops.py \\
        --bundle BUNDLE --runs-root ROOT --out results/openemr-1347 --app openemr

For every ``report.json`` under ``ROOT`` that halted, this applies what
:class:`openadapt_flow.repair.auto_repair.AutoRepair` would do with it:
:func:`~openadapt_flow.repair.auto_repair.route_stop` (repair, reconciliation
or a person) and, for a stop routed to repair, self-repair's own check of
whether it handles that halt. It calls no agent and opens no app: a stop that
is never handed to repair needs neither, and the answer for one that is
depends only on the halted report and the bundle.

It also records what self-repair's check would say if the stop had been
routed to repair (``if_routed``), so a reader can see why the stop is or is
not repairable, not only where it went.

Outputs: ``ledger.jsonl`` (the loop's own row per stop), ``stops.csv`` and
``summary.json``. Rows name the run and item directory relative to
``ROOT``; no record values are copied.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--runs-root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    from openadapt_flow.ir import RunReport, Workflow
    from openadapt_flow.repair.auto_repair import (
        RepairLedger,
        ledger_row,
        route_stop,
        summarize,
    )
    from openadapt_flow.repair.self_repair import _repairable

    workflow = Workflow.load(Path(args.bundle))
    root = Path(args.runs_root)
    out = Path(args.out)
    if not out.is_absolute():
        out = HERE / out
    out.mkdir(parents=True, exist_ok=True)
    ledger = RepairLedger(out)
    ledger.path = out / "ledger.jsonl"
    ledger.path.unlink(missing_ok=True)
    rows = []
    reports = sorted(root.rglob("report.json"))
    for path in reports:
        report = RunReport.model_validate_json(path.read_text())
        if report.halt is None or report.success:
            continue
        name = str(path.parent.relative_to(root))
        route, why = route_stop(report)
        verdict = _repairable(report, workflow)
        if_routed = (
            verdict
            if isinstance(verdict, str)
            else f"repairable: {verdict[0].id} ({verdict[1]})"
        )
        if route == "repair" and isinstance(verdict, str):
            route, why = "person", verdict
        row = ledger_row(
            report,
            name,
            route=route,
            route_reason=why,
            labels={"app": args.app, "run": name.split("/")[0]},
        )
        ledger.append(row)
        halted = next(
            (r for r in reversed(report.results) if not r.ok and not r.skipped), None
        )
        rows.append(
            {
                "run": name,
                "outcome": report.transaction_outcome,
                "halted_step": report.halt.state_id,
                "halt_kind": (report.halt.reason or "").split(" for step")[0][:60],
                "identity": (
                    halted.identity.status
                    if halted is not None and halted.identity is not None
                    else ""
                ),
                "route": route,
                "if_routed_to_repair": if_routed[:200],
            }
        )
    with (out / "stops.csv").open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "run",
                "outcome",
                "halted_step",
                "halt_kind",
                "identity",
                "route",
                "if_routed_to_repair",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    (out / "summary.json").write_text(
        json.dumps(
            {
                "app": args.app,
                "note": args.note,
                "bundle_name": workflow.name,
                "reports_read": len(reports),
                "funnel": summarize(ledger.rows()),
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps(summarize(ledger.rows()), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
