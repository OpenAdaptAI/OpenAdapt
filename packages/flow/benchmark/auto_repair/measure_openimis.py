"""Measure the default repair loop on openIMIS's relabelled Save (#1331).

    python benchmark/auto_repair/measure_openimis.py --run-id 2026-10-19-a \\
        --bundle BUNDLE --label reviewed --healthy 2 --changed 3

Each item runs the compiled claim-entry program (``launch-4``) once through
:class:`openadapt_flow.repair.auto_repair.AutoRepair`, the loop ``run`` uses
by default when a repair agent is set. ``--healthy`` items run on the
unchanged app; then the vendor change is switched on (the icon-only Save
becomes "Save claim", ``scoreboard.tasks.openimis.ui_change``) and
``--changed`` items run on it.

The program runs as the scoreboard's arm 3 runs it: the demo profile's plain
Replayer with the read-only SQL record check (``DemoTarget`` from
``scoreboard.repair_arm``). The repair agent is ``BridgeRepairAgent`` from
``measure_mockmed.py`` (one Claude call per proposal, the customer's key);
the approver is scripted and approves everything that reaches it. No fault
probe is configured, as in the scoreboard's repair arm.

``--bundle`` is the program to run. ``--label`` names it in the results: the
bundle as compiled (``unreviewed``), or the same bundle after a scripted
reviewer classified its two unclassified steps with ``openadapt-flow qualify
set-risk`` (``reviewed``, the step #1337 points to).

After every item this reads the claim table itself (``app.check``):
``correct``, ``missing``, ``duplicate`` or ``wrong``.

Outputs (committed): ``<out>/ledger.jsonl``, ``items.csv``, ``summary.json``,
``calls.json`` and ``meta.json``. Synthetic data only.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from measure_mockmed import (  # noqa: E402
    APPROVER,
    CACHE,
    BridgeRepairAgent,
    _approver,
    _git_head,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--healthy", type=int, default=2)
    parser.add_argument("--changed", type=int, default=3)
    parser.add_argument("--start", type=int, default=200)
    parser.add_argument("--tag", default="ar")
    parser.add_argument("--out", default=None)
    parser.add_argument("--model", default="claude-sonnet-5-5")
    args = parser.parse_args()

    from scoreboard.repair_arm import DemoTarget
    from scoreboard.tasks import openimis as app

    from openadapt_flow.jit import load_customer_model_key
    from openadapt_flow.repair.auto_repair import AutoRepair, RepairLedger, summarize
    from openadapt_flow.repair.self_repair import ScreenAccess, SelfRepair

    name = f"openimis-{args.label}-{args.run_id}"
    out = Path(args.out) if args.out else HERE / "results" / name
    out.mkdir(parents=True, exist_ok=True)
    work = CACHE / name
    work.mkdir(parents=True, exist_ok=True)
    ledger = RepairLedger(out)
    ledger.path = out / "ledger.jsonl"
    key = load_customer_model_key("anthropic")
    agent = BridgeRepairAgent(model=args.model)
    (out / "meta.json").write_text(
        json.dumps(
            {
                "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "engine_git_head": _git_head(),
                "app": "openIMIS reference stack (pinned), synthetic data",
                "program": "launch-4 claim-entry bundle (demo/cua_to_program)",
                "bundle_label": args.label,
                "risk_review": (
                    "step_002 and step_012 classified state_changing by a scripted "
                    "reviewer (qualify set-risk), standing in for a person"
                    if args.label == "reviewed"
                    else "none"
                ),
                "ui_change": "icon-only Save relabelled 'Save claim' (#1331)",
                "loop": "AutoRepair over SelfRepair; no fault probe; no policy",
                "agent": agent.agent_id,
                "approver": APPROVER,
                "items": {"healthy": args.healthy, "changed": args.changed},
            },
            indent=2,
        )
        + "\n"
    )
    from openadapt_flow.backends._browser_process import shared_browser

    rows = []
    app.ui_change(False)
    # The halted session stays open while a probe opens another; both need
    # one shared browser process on this thread, as `run` shares one.
    browser = shared_browser(headless=True)
    browser.__enter__()
    try:
        total = args.healthy + args.changed
        for n in range(total):
            i = args.start + n
            changed = n >= args.healthy
            if changed != app.ui_change_is_on():
                app.ui_change(changed)
            values = app.item_values(i, args.tag)
            item_dir = work / f"item-{i:03d}"
            shutil.rmtree(item_dir, ignore_errors=True)
            decisions: list[float] = []
            before = agent.cost_usd
            repair = SelfRepair(
                agent,
                DemoTarget(app, values),
                store=item_dir / "repair-store",
                screen_access=ScreenAccess(
                    enabled_by="benchmark-operator (synthetic data only)",
                    mode="allow_unscrubbed",
                ),
                model_key=key,
                approval_wait_s=120,
                sleep=_approver(item_dir, decisions),
            )
            loop = AutoRepair(
                repair,
                ledger=ledger,
                labels={
                    "app": "openimis",
                    "bundle": args.label,
                    "ui_change": str(changed).lower(),
                    "item": str(i),
                },
            )
            t0 = time.time()
            try:
                result = loop.run(Path(args.bundle), item_dir / "run")
                status, reason = result.status, result.evidence.reason
            except Exception as exc:  # noqa: BLE001 - a harness error is a row
                status, reason = "harness_error", f"{type(exc).__name__}: {exc}"
            wall = time.time() - t0
            readback = app.check(values)
            rows.append(
                {
                    "item": i,
                    "ui_change": changed,
                    "repair_status": status,
                    "readback": readback,
                    "cost_usd": round(agent.cost_usd - before, 6),
                    "wall_s": round(wall, 1),
                    "reason": (reason or "")[:300],
                }
            )
            print(
                f"{i} change={changed} {status:18s} {readback:9s} "
                f"${agent.cost_usd - before:.3f} {wall:.0f}s {reason[:120]}",
                flush=True,
            )
    finally:
        browser.__exit__(None, None, None)
        app.ui_change(False)
        with (out / "items.csv").open("w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "item",
                    "ui_change",
                    "repair_status",
                    "readback",
                    "cost_usd",
                    "wall_s",
                    "reason",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)
        (out / "calls.json").write_text(
            json.dumps(
                {
                    "model": agent.model,
                    "cost_usd": round(agent.cost_usd, 6),
                    "calls": [asdict(c) for c in agent.calls],
                },
                indent=2,
            )
            + "\n"
        )
        if ledger.path.is_file():
            ledger.path.write_text(
                ledger.path.read_text().replace(
                    str(CACHE), "~/.cache/openadapt-auto-repair"
                )
            )
        (out / "summary.json").write_text(
            json.dumps(
                {
                    "funnel": summarize(ledger.rows()),
                    "readback": {
                        k: sum(r["readback"] == k for r in rows)
                        for k in sorted({r["readback"] for r in rows})
                    },
                    "ui_change_off_after_run": not app.ui_change_is_on(),
                },
                indent=2,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
