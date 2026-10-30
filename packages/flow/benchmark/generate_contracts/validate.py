"""Judge generated contracts on a first-study app: correct writes and faults.

    FCG_INSTANCE=1 python benchmark/generate_contracts/validate.py dolibarr WORKDIR [--only T01 F05]

For every task of the study, the pack's reference correct write; for every
planted fault, the fault. Each case: reset the app, pick the contract with
``verify_contract.choose`` (from the task's values only), ``begin_write``,
write, ``verify_write``, and the study's oracle labels what happened. Results
append to ``WORKDIR/validate.jsonl``; a case already there is skipped.

The tasks, faults and oracle are read here only, never by the generator.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

import yaml

from demo_lib import FCG, fixture, load

from openadapt_flow.generate import verify_contract as gen
from openadapt_flow.verifier import Actor, begin_write, verify_write

LATE_WAIT_S = 150


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("app")
    ap.add_argument("work", type=Path)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--actor-id", required=True, help="the bot's id as the contracts bind it")
    ap.add_argument("--actor-param", default="bot_user_id")
    ap.add_argument("--no-faults", action="store_true")
    ap.add_argument("--no-correct", action="store_true")
    args = ap.parse_args()
    os.environ.setdefault("OPENADAPT_LOCAL_SEAL", "off")
    app_dir = FCG / "apps" / args.app
    fx = fixture(args.app)
    oracle = load(f"fcg_oracle_{args.app}", app_dir / "oracle.py")
    faults = load(f"fcg_faults_{args.app}", app_dir / "faults" / "faults.py")
    tasks = {t["id"]: t for t in yaml.safe_load((app_dir / "tasks.yaml").read_text())}
    manifest = args.work / "out" / "manifest.yaml"
    out = args.work / "validate.jsonl"
    done = set()
    if out.exists():
        done = {json.loads(x)["case"] for x in out.read_text().splitlines() if x.strip()}
    cases: list[tuple[str, dict, Any, float]] = []
    if not args.no_correct:
        for tid, task in tasks.items():
            cases.append((f"correct-{tid}", task, lambda t=task: faults.correct_write(t, fx.bot()), 0.0))
    if not args.no_faults:
        for f in yaml.safe_load((app_dir / "faults" / "faults.yaml").read_text()):
            task = tasks[f["task"]]
            fn = getattr(faults, f["id"])
            late = 0.0
            if f.get("class") == "late_write" or f.get("after_snapshot_delay_s"):
                late = max(LATE_WAIT_S, float(f.get("after_snapshot_delay_s") or 0))
            cases.append(
                (f"fault-{f['id']}", task,
                 lambda fn=fn, t=task: fn(t, fx.bot(), fx.other_user(), fx.read_access()), late)
            )  # fmt: skip
    for case, task, act, late in cases:
        short = case.split("-", 1)[1]
        if case in done or (args.only and short not in args.only and task["id"] not in args.only):
            continue
        while os.getloadavg()[0] >= 35:
            time.sleep(30)
        rec: dict[str, Any] = {"case": case, "task": task["id"], "kind": task["kind"]}
        fx.reset()
        before = oracle.snapshot()
        pending = None
        try:
            contract, params = gen.choose(manifest, task["kind"], task["values"], {args.actor_param: args.actor_id})
            rec["contract"] = contract.name
            tmp = tempfile.mkdtemp()
            pending = begin_write(contract, params, actor=Actor("agent", "generated-check"),
                                  ledger=None, write_key=f"{case}-{time.time_ns()}", workdir=tmp)  # fmt: skip
        except Exception as exc:  # noqa: BLE001 - a stop before the write
            rec["verdict"] = {"outcome": "BLOCKED_AT_BEGIN", "reasons": [f"{type(exc).__name__}: {exc}"]}
        try:
            rec["claim"] = act()
        except Exception as exc:  # noqa: BLE001
            rec["claim"] = {"status": "not_done", "note": f"{type(exc).__name__}: {exc}"}
        if pending is not None:
            try:
                r = verify_write(pending)
                rec["verdict"] = {
                    "outcome": getattr(r.outcome, "value", str(r.outcome)),
                    "reasons": list(r.reasons),
                    "recheck_due_at": getattr(r, "recheck_due_at", None),
                }
            except Exception as exc:  # noqa: BLE001
                rec["verdict"] = {"outcome": "VERIFIER_CRASH", "reasons": [f"{type(exc).__name__}: {exc}"],
                                  "trace": traceback.format_exc()[-2000:]}  # fmt: skip
        if late:
            time.sleep(late)
            if hasattr(fx, "wait_late_writes"):
                fx.wait_late_writes()
        after = oracle.snapshot()
        try:
            rec["oracle"] = oracle.judge(task, before, after)
        except Exception as exc:  # noqa: BLE001
            rec["oracle"] = {"label": "oracle_error", "reasons": [str(exc)]}
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
        v = rec["verdict"]
        print(f"{case} [{task['kind']}]: {v['outcome']} | oracle {rec['oracle'].get('label')} | "
              f"{'; '.join(v.get('reasons', []))[:200]}", flush=True)  # fmt: skip
    fx.reset()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
