"""One command for the whole economic comparison: every arm, one seeded stream, resumable.

    cd packages/flow/benchmark
    python -m lifecycle.arms.compare --out lifecycle/results/economics-<date> [--seed 7 --items 40 --days 10]

- Every arm runs the same seeded worklist and event ledger through the soak
  harness's ``run_arm`` (same reset, seeded charts, drift days, injected
  faults, oracle, scripted operator and cost model).
- Each arm writes its own ``<out>/<arm>/ledger.jsonl``. Rerunning the same
  command skips arms whose ledger ends with ``arm_end``. An arm that stopped
  part-way (the spend cap, a crash, Ctrl-C) is moved to
  ``<arm>.partial-<n>`` and run again from its start, because the app is reset
  per arm; the partial ledger is kept and its model spend stays counted.
- Model spend is capped across all arms at $30 (``LIFECYCLE_SPEND_CAP_USD``
  raises it only with the coordinator's go). The cap is checked before every
  model call; reaching it stops the run with a message to resume.
- The API key comes from ``ANTHROPIC_API_KEY`` or the keychain item named in
  ``LIFECYCLE_KEYCHAIN_SERVICE``; it is never printed.
- At the end, ``economics.json`` and ``REPORT.md`` are written from the
  ledgers alone (``python -m lifecycle.arms.economics <out>`` recomputes them).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from . import budget, economics, report

DEFAULT_ARMS = [
    # (name in the ledger, module:factory, options)
    # The soak's OpenAdapt arm: the certified referral program under the production
    # configuration. Its API tier is refused on this form (results/api_tier/).
    ("openadapt", "lifecycle.openadapt_arm:make", {}),
    ("cua_alone", "lifecycle.arms.cua:factory", {"model": "claude-opus-5-5", "item_budget_usd": 1.00}),
    ("agent_tools", "lifecycle.arms.agent_tools:factory", {"model": "claude-opus-5-5", "item_budget_usd": 0.60}),
    ("agent_script", "lifecycle.arms.agent_script:factory", {"model": "claude-opus-5-5"}),
    ("human_script", "lifecycle.arms.human_script:factory", {}),
]

#: Local Ollama stand-ins for the frontier agent, used while no key is
#: available. Their names say so, and every row records the model. The
#: screenshot stand-in runs only the first 10 items: an 8B vision model on this
#: host takes about a minute a call and does not act reliably, so a full stream
#: would hold the shared fixture for hours to show the same thing.
LOCAL_ARMS = [
    ("openadapt", "lifecycle.openadapt_arm:make", {}),
    ("cua_local", "lifecycle.arms.cua:factory",
     {"name": "cua_local", "model": "ollama:qwen3-vl:8b", "max_turns": 6, "stop_after": 10}),
    ("agent_tools_local", "lifecycle.arms.agent_tools:factory",
     {"name": "agent_tools_local", "model": "ollama:llama3.1:8b", "max_turns": 25}),
    ("agent_script_local", "lifecycle.arms.agent_script:factory",
     {"name": "agent_script_local", "model": "ollama:llama3.1:8b", "max_turns": 20}),
    ("human_script", "lifecycle.arms.human_script:factory", {}),
]


def _complete(ledger: Path) -> bool:
    if not ledger.is_file():
        return False
    lines = [ln for ln in ledger.read_text().splitlines() if ln.strip()]
    return bool(lines) and json.loads(lines[-1]).get("type") in ("arm_end", "run_end") and any(
        json.loads(ln).get("type") == "arm_end" for ln in lines[-3:]
    )


def _move_partial(d: Path) -> Optional[Path]:
    if not d.exists():
        return None
    n = 1
    while (d.parent / f"{d.name}.partial-{n}").exists():
        n += 1
    dest = d.parent / f"{d.name}.partial-{n}"
    d.rename(dest)
    return dest


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--items", type=int, default=40)
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--existing", type=int, default=12)
    p.add_argument("--only", action="append", help="run only these arms (by name)")
    p.add_argument("--arms-json", type=Path, help="a JSON list of [name, module:factory, options]")
    p.add_argument("--models", choices=("frontier", "local"), default="frontier",
                   help="local: Ollama stand-ins for the agent arms (labelled *_local)")
    p.add_argument("--app", default="openemr")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args(argv)

    from ..cost_model import V1
    from ..run import PROTOCOL_VERSION, Ledger, engine_commit, run_arm
    from ..workload import WORKLOAD_VERSION, generate

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    arms = (json.loads(args.arms_json.read_text()) if args.arms_json
            else LOCAL_ARMS if args.models == "local" else DEFAULT_ARMS)
    if args.only:
        arms = [a for a in arms if a[0] in args.only]

    plan = {"seed": args.seed, "items": args.items, "days": args.days, "existing": args.existing,
            "app": args.app, "arms": arms}
    plan_path = out / "plan.json"
    if plan_path.is_file():
        old = json.loads(plan_path.read_text())
        same = {k: old.get(k) for k in ("seed", "items", "days", "existing", "app")}
        if same != {k: plan[k] for k in same}:
            raise SystemExit(f"{out} was started with a different stream {same}; use a new --out")
    else:
        plan_path.write_text(json.dumps(plan, indent=1) + "\n")

    if not args.report_only:
        workload = generate(args.seed, args.items, args.days, args.existing)
        wl = out / "workload.jsonl"
        text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in workload.ledger_rows())
        if wl.is_file() and wl.read_text() != text:
            raise SystemExit("the seeded workload differs from the one this run started with")
        wl.write_text(text)
        from ..apps import make_app

        app = None
        for name, spec, options in arms:
            d = out / name
            if _complete(d / "ledger.jsonl"):
                print(f"[{name}] already complete; skipping")
                continue
            moved = _move_partial(d)
            if moved:
                print(f"[{name}] earlier attempt stopped part-way; kept as {moved.name}")
            d.mkdir(parents=True)
            if app is None:
                app = make_app(args.app)
            ledger = Ledger(d / "ledger.jsonl")
            ledger.write({
                "type": "run_start", "protocol": PROTOCOL_VERSION, "workload": WORKLOAD_VERSION,
                "seed": args.seed, "items": args.items, "days": args.days,
                "cost_model": V1.to_dict(), "engine_commit": engine_commit(),
                "prices_as_of": budget.prices()["as_of"], "spend_before_usd": budget.spent(),
                "spend_cap_usd": budget.cap_usd(), "app": app.describe(),
            })
            t0 = time.time()
            try:
                run_arm(arm_spec=spec, arm_options=options, app=app, workload=workload,
                        ledger=ledger, out=d, cost_name=V1.version,
                        stop_after=options.get("stop_after"))
                ledger.write({"type": "run_end", "wall_s": round(time.time() - t0, 1),
                              "spend_after_usd": budget.spent()})
            except budget.SpendCapReached as exc:
                ledger.write({"type": "stopped", "why": "spend_cap", "detail": str(exc)})
                ledger.close()
                print(f"[{name}] stopped: {exc}\nResume with the same command once the cap is raised.")
                return 3
            finally:
                ledger.close()

    rows = economics.read_rows(sorted(out.glob("*/ledger.jsonl")))
    result = economics.compute(rows)
    (out / "economics.json").write_text(json.dumps(result, indent=1, default=str) + "\n")
    (out / "REPORT.md").write_text(report.render(result, out, plan))
    print(economics.render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
