"""Code as action against the screenshot loop on MockMed, live, on the operator's key.

Each trial runs every arm once, in rotating order, on the same task against a
freshly reset MockMed and its system of record:

* ``code``: one ``run`` tool that takes a script (code as action).
* ``every_turn``: Claude's computer toolset, a screenshot after every action
  (the screenshot loop).
* ``selective``: the structural listing after every action, a screenshot on
  request (compile-agent's default).

Correct means run 1 passed its record check: the system of record, read back
after the run, shows exactly one new encounter on the right patient with the
note typed. Then one passing code-arm program is admitted and replayed, with
no model calls, to show the trace compiled into a working program.

    PYTHONPATH=. python benchmark/code_as_action/measure_mockmed.py \\
        --env-file /path/to/.env --trials 5

All data is synthetic (MockMed). Spend is capped across every script here
(results/spend.json).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402

PROMPT = (
    "Open the referral for Jane Sample, start a Triage encounter, and save "
    "this note: {note}"
)
NOTE = "Synthetic follow-up in two weeks"
REPLAY_NOTES = ["Recheck knee in ten days", "Ice and rest, review in a week"]
ARMS = ["code", "every_turn", "selective"]
KEYS = [
    "model_calls",
    "prompt_tokens",
    "output_tokens",
    "cost_usd",
    "run_seconds",
    "model_seconds",
    "actions",
    "screenshots_sent",
]


def _compiler(work: Path, base: str, task_id: str) -> Any:
    from openadapt_flow.compile_agent import FirstRunCompiler
    from openadapt_flow.jit.keys import load_customer_model_key
    from openadapt_flow.tutorial import _http_json

    verify = work / "verify.yaml"
    verify.write_text(
        "verifier:\n"
        "  kind: rest\n"
        f"  base_url: {base}\n"
        "  records_path: /api/db\n"
        "  records_key: records\n"
        "  timeout_s: 3.0\n"
        "  poll_interval_s: 0.1\n"
    )
    return FirstRunCompiler(
        PROMPT,
        params=["note"],
        url=f"{base}/?fault=ok&idempotency=demo#tasks",
        verify=verify,
        expect={"patient_id": "p1", "type": "Triage"},
        on_screen=["Jane Sample", "MRN P1"],
        task_id=task_id,
        cache_dir=work / "cache",
        key_dir=work / "keys",
        reset=lambda: _http_json(f"{base}/api/reset", method="POST", body={}),
        on_warning=lambda text: None,
        model_key=load_customer_model_key("anthropic"),
    )


def _admitter(work: Path) -> Any:
    from openadapt_flow.jit import authority

    os.environ.setdefault(authority.KEY_PASSPHRASE_ENV, "measurement-only")
    pem = work / "keyfiles" / "admitter.pem"
    authority.register_principal(
        work / "keys",
        role="admitter_key",
        principal_id="ci:measurement",
        public_key_hex=authority.generate_encrypted_key(pem),
    )
    return authority.principal_from_key(
        authority.load_encrypted_key(pem), work / "keys"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--arms", default=",".join(ARMS))
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--max-run-usd", type=float, default=0.75)
    parser.add_argument("--replays", type=int, default=2)
    parser.add_argument("--out", type=Path, default=common.RESULTS / "mockmed")
    parser.add_argument("--label", default="")
    args = parser.parse_args()
    common.load_key(args.env_file)
    arms = [a for a in args.arms.split(",") if a]

    from openadapt_flow.compile_agent import ClaudeAgent
    from openadapt_flow.mockmed.fault_server import serve

    runs_path = args.out / "runs.jsonl"
    rows: list[dict[str, Any]] = []
    work_root = Path(tempfile.mkdtemp(prefix="code-action-mockmed-"))
    base, _db, stop = serve()
    base = base.rstrip("/")
    passing: list[Any] = []
    stopped = None
    try:
        for trial in range(1, args.trials + 1):
            shift = (trial - 1) % len(arms)
            order = arms[shift:] + arms[:shift]
            for arm in order:
                stopped = common.may_start(args.max_run_usd)
                if stopped:
                    break
                work = work_root / f"t{trial}-{arm}"
                work.mkdir(parents=True)
                compiler = _compiler(work, base, f"mockmed.code-action-{arm}-t{trial}")
                agent = ClaudeAgent(
                    vision=arm,
                    model=args.model,
                    effort=args.effort,
                    max_cost_usd=args.max_run_usd,
                )
                note = f"{NOTE}, trial {trial}"
                load_before = common.load_1m()
                started = time.time()
                try:
                    result = compiler.record(agent, {"note": note})
                    status, detail, run1 = result.status, result.detail, result.run1
                except Exception as exc:  # noqa: BLE001 - a failed run is a row
                    status, detail = "error", f"{type(exc).__name__}: {exc}"
                    ledger = getattr(agent, "last_ledger", None)
                    run1 = {"agent": ledger.to_dict() if ledger else {}}
                wall = time.time() - started
                load_after = common.load_1m()
                agent_row = run1.get("agent") or {}
                metrics = common.agent_metrics(agent_row, run1.get("harness") or {})
                cost = float(metrics.get("cost_usd") or 0.0)
                total = common.add_spend(f"mockmed {arm} t{trial}", cost)
                row = {
                    "app": "mockmed",
                    "label": args.label or None,
                    "trial": trial,
                    "arm": arm,
                    "model": args.model,
                    "order": order.index(arm),
                    "started_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)
                    ),
                    "status": status,
                    "correct": status == "draft",
                    "record_check": run1.get("record_check"),
                    "detail": detail if status != "draft" else None,
                    "run_seconds": round(float(run1.get("seconds") or wall), 3),
                    "host_load_1m": [load_before, load_after],
                    "under_load": max(load_before, load_after) >= common.LOAD_LIMIT,
                    **metrics,
                    "code_action": run1.get("code_action"),
                    "gates": [
                        {
                            k: v
                            for k, v in g.items()
                            if k not in ("screenshot", "url", "at")
                        }
                        for g in run1.get("gates") or []
                    ],
                    "agent_summary": agent_row.get("summary"),
                    "calls": agent_row.get("calls"),
                }
                rows.append(row)
                common.append(runs_path, row)
                print(
                    f"t{trial} {arm}: {status}; {metrics['model_calls']} calls, "
                    f"{metrics['actions']} actions, {metrics['prompt_tokens']} prompt "
                    f"tokens, ${cost:.4f}, {row['run_seconds']} s, load "
                    f"{load_before}->{load_after} (total ${total:.2f})",
                    flush=True,
                )
                if status == "draft" and arm == "code":
                    passing.append(compiler)
            if stopped:
                print(f"stopping: {stopped}", flush=True)
                break
        replays: list[dict[str, Any]] = []
        if passing and args.replays:
            compiler = passing[0]
            compiler.confirm_checks(
                principal_id="measurement.operator", drop_fields=["key", "source"]
            )
            admitted = compiler.admit(
                admitter=_admitter(compiler.dir.parent.parent),
                canary_params=[{"note": "Canary test note 1"}],
            )
            print(f"admit code: {admitted.status} {admitted.detail or ''}", flush=True)
            if admitted.admitted:
                for i, note in enumerate(REPLAY_NOTES[: args.replays], start=1):
                    replay = compiler.run(note=note)
                    row = {
                        "app": "mockmed",
                        "program_from": "code",
                        "replay": i,
                        "status": replay.status,
                        "verified": replay.verified,
                        "model_calls": replay.model_calls,
                        "seconds": replay.seconds,
                        "receipt_outcome": replay.receipt_outcome,
                        "host_load_1m": common.load_1m(),
                    }
                    replays.append(row)
                    common.append(args.out / "replays.jsonl", row)
                    print(f"replay {i}: {row}", flush=True)
    finally:
        stop()
    every = [json.loads(line) for line in runs_path.read_text().splitlines() if line]
    summary = {
        "arms": common.summarize(every, arms, KEYS),
        "environment": {
            "model": args.model,
            "effort": args.effort,
            "app": "MockMed fault server, ?fault=ok&idempotency=demo, 1280x800",
            "engine_commit": common.commit(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "measured_at": common.now(),
            "load_limit_1m": common.LOAD_LIMIT,
            "spend_total_usd": common.spent(),
        },
    }
    replay_path = args.out / "replays.jsonl"
    if replay_path.is_file():
        rep = [
            json.loads(line) for line in replay_path.read_text().splitlines() if line
        ]
        summary["replays"] = {
            "runs": len(rep),
            "verified": sum(1 for r in rep if r["verified"]),
            "model_calls_total": sum(r["model_calls"] or 0 for r in rep),
        }
    (args.out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary["arms"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
