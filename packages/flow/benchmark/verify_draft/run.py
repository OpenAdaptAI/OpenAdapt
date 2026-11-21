"""Run the verify-draft benchmark on one app: set up, draft, hold out.

    cd packages/flow
    ANTHROPIC_API_KEY=... python -m benchmark.verify_draft.run mockloan --out benchmark/verify_draft/results/mockloan

1. The operator setup (``apps.py``), timed on its own.
2. ``openadapt verify draft`` (``openadapt_flow.verify_draft.draft.run``)
   with the pinned model: setup minutes, rounds, person decisions, cost.
3. If a contract is admitted, a held-out check with no model: a second
   qualification run on harness-chosen values (other clean writes, the
   faults planted on other rows with other values), plus app-level faults
   the admission suite never plants (the same write sent to another record
   through the app; for a create, the write sent twice).

Person decisions are counted as the draft command counts them (answers to
its one question). The sandbox file's facts are listed as operator inputs,
not decisions, as in the Vikunja setup log the brief compares against.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from benchmark.verify_draft.apps import SETUPS, AppRun
from openadapt_flow.verify_draft import admit as A
from openadapt_flow.verify_draft import draft as D
from openadapt_flow.verify_draft import model as M
from openadapt_flow.verify_draft.sandbox import App, Store, load_sandbox, swap_map


def heldout(run: AppRun, contract_path: Path, work: Path) -> dict[str, Any]:
    sandbox = load_sandbox(str(run.sandbox))
    store, app = Store(sandbox.store), App(sandbox)
    from openadapt_flow.verify_draft.inputs import load_task, load_trace, writes

    task = load_task(run.task)
    base = store.read()
    demo = A.record_demo(
        store,
        app,
        writes(load_trace(run.trace), skip_paths=app.login_paths()),
        task.params,
        base,
    )
    q = A.Qualifier(sandbox, store, app, demo, seed=1, workdir=work)
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    contract.pop("provenance", None)
    values = {**task.params, **run.heldout_payloads[-1]}
    adm = q.qualify(contract, run.heldout_payloads, base_values=values, attribute=False)
    extra: list[A.Case] = []
    if run.other_record:
        case = A.Case(
            "fault",
            "app_sibling",
            f"the same write also sent to {run.other_record} through the app",
            "refuted",
        )

        def both(pre: Any, post: Any) -> None:
            app.send(
                demo.writes,
                "agent",
                swap_map(demo.params, {**values, **run.other_record}),
            )

        q.run(contract, case, values=values, fault=both)
        q._judge(case)
        extra.append(case)
    if demo.change == "added":
        case = A.Case(
            "fault", "app_duplicate", "the write sent twice through the app", "refuted"
        )

        def twice(pre: Any, post: Any) -> None:
            app.send(demo.writes, "agent", swap_map(demo.params, values))

        q.run(contract, case, values=values, fault=twice)
        q._judge(case)
        extra.append(case)
    cases = adm.cases + extra
    return {
        "cases": [c.as_dict() for c in cases],
        "faults_applicable": sum(
            1 for c in cases if c.group == "fault" and c.applicable
        ),
        "faults_caught": sum(
            1 for c in cases if c.group == "fault" and c.applicable and c.passed
        ),
        "faults_missed": [
            c.name
            for c in cases
            if c.group == "fault" and c.applicable and not c.passed
        ],
        "clean": sum(1 for c in cases if c.group == "clean"),
        "false_stops": [c.name for c in cases if c.group == "clean" and not c.passed],
        "problems": adm.problems,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("app", choices=sorted(SETUPS))
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--spend-ledger", default="benchmark/verify_draft/results/spend.jsonl"
    )
    ap.add_argument("--spend-cap-usd", type=float, default=30.0)
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("OPENADAPT_LOCAL_SEAL", "off")
    run = SETUPS[args.app](out / "setup")
    try:
        ledger = M.SpendLedger(Path(args.spend_ledger), args.spend_cap_usd)
        drafter = M.ClaudeDrafter(ledger, label=args.app)
        started = time.monotonic()
        outcome = D.run(
            describe=run.describe,
            trace_path=str(run.trace),
            task_path=str(run.task),
            sandbox_path=str(run.sandbox),
            out=out / "draft",
            drafter=drafter,
            ask=None,  # the benchmark never answers for a person: a question refuses
            log=lambda s: print(f"[{args.app}] {s}", file=sys.stderr, flush=True),
        )
        minutes = (time.monotonic() - started) / 60
        rounds = [
            {
                "admitted": a.admitted,
                "faults_applicable": sum(
                    1 for c in a.cases if c.group == "fault" and c.applicable
                ),
                "faults_caught": sum(
                    1
                    for c in a.cases
                    if c.group == "fault" and c.applicable and c.passed
                ),
                "clean": sum(1 for c in a.cases if c.group == "clean"),
                "false_stops": [
                    c.name for c in a.cases if c.group == "clean" and not c.passed
                ],
                "probe_writes_confirmed": sum(
                    1 for c in a.cases if c.group == "probe" and c.passed
                ),
                "attribution_problems": [
                    f"{x.table}.{x.column}: {x.problem}"
                    for x in a.attribution
                    if x.problem
                ],
                "not_applicable": {c.name: c.note for c in a.cases if not c.applicable},
                "seconds": a.seconds,
            }
            for a in outcome.admissions
        ]
        result: dict[str, Any] = {
            "app": run.name,
            "untuned": run.untuned,
            "describe": "openapi" if run.describe.endswith(".json") else run.describe,
            "operator_setup_seconds": run.setup_seconds,
            "operator_inputs": run.operator_inputs,
            "setup_minutes": round(minutes, 2),
            "person_decisions": outcome.decisions,
            "questions": outcome.questions,
            "drafts": outcome.rounds,
            "drafts_admitted": sum(1 for a in outcome.admissions if a.admitted),
            "drafts_rejected": outcome.rounds
            - sum(1 for a in outcome.admissions if a.admitted),
            "admitted": outcome.admitted,
            "reason": outcome.reason[:2000],
            "model": M.MODEL,
            "prompt_sha256": M.prompt_sha256(),
            "model_usd": round(outcome.usd, 4),
            "model_calls": [e for e in ledger.entries],
            "rounds": rounds,
            "notes": run.notes,
        }
        if outcome.admitted and outcome.contract_path is not None:
            result["heldout"] = heldout(
                run, outcome.contract_path, out / "heldout-work"
            )
        (out / "result.json").write_text(
            json.dumps(result, indent=1, default=str), encoding="utf-8"
        )
        print(
            json.dumps(
                {k: v for k, v in result.items() if k not in ("model_calls",)},
                indent=1,
                default=str,
            )
        )
        return 0
    finally:
        run.teardown()


if __name__ == "__main__":
    raise SystemExit(main())
