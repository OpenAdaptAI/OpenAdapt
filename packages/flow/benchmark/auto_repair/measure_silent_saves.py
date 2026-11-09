"""Re-run silent-saves 1.1.0's UI-change blocks under the default repair loop.

    python benchmark/auto_repair/measure_silent_saves.py --run-id 2026-10-19-a

Needs ``benchmark/silent_saves`` from #1380 (fault set 1.1.0) on the path, and
the admitted MockMed program arms 3 and 3r ran (``~/.cache/silent-saves/...``,
admitted on ``http://127.0.0.1:18765``, so the app is served on that port).

Arms 3 and 3r of #1380 stopped on every item of three blocks: display scaling
(``zoom``, items 26-35), a What's New screen (``notice``, 46-55) and a moved
New Encounter control (``relink``, 76-85). This runs those 30 items (28 with
no server fault, plus one ``wrong_record`` and one ``optimistic`` item) the way
arm 3r does, through the benchmark's own app, schedule and store judge, but
with the loop ``openadapt-flow run`` now uses by default
(:class:`openadapt_flow.repair.auto_repair.AutoRepair`) and the bridge-capable
agent from ``measure_mockmed.py``. As in arm 3r, after a repair resumes and
verifies, later items run the repaired program. The approver is scripted.

Outputs (committed): ``results/silent-saves-1.1.0-<run-id>/`` with
``items.csv`` (one row per item, scored by ``silent_saves.judge``),
``ledger.jsonl``, ``summary.json``, ``calls.json`` and ``meta.json``.
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
from typing import Any, Optional

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

BLOCKS = {"zoom": range(26, 36), "notice": range(46, 56), "relink": range(76, 86)}
PORT = 18765


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--blocks", default=",".join(BLOCKS))
    parser.add_argument("--model", default="claude-sonnet-5-5")
    args = parser.parse_args()

    from silent_saves import judge, protocol
    from silent_saves.app import MockMed
    from silent_saves.repair_arm import DEFAULT_BUNDLE, SelfRepairProgram

    from openadapt_flow.backends._browser_process import shared_browser
    from openadapt_flow.jit import load_customer_model_key
    from openadapt_flow.repair.auto_repair import AutoRepair, RepairLedger, summarize
    from openadapt_flow.repair.self_repair import (
        ReplayTarget,
        ScreenAccess,
        SelfRepair,
    )
    from openadapt_flow.runtime.effects import RestRecordVerifier

    blocks = [b for b in args.blocks.split(",") if b]
    wanted = {i for b in blocks for i in BLOCKS[b]}
    items = [
        it for it in protocol.plan_change(protocol.load("1.1.0")) if it.item in wanted
    ]
    out = HERE / "results" / f"silent-saves-1.1.0-{args.run_id}"
    out.mkdir(parents=True, exist_ok=True)
    work = CACHE / f"silent-saves-{args.run_id}"
    shutil.rmtree(work, ignore_errors=True)
    ledger = RepairLedger(out)
    ledger.path = out / "ledger.jsonl"
    ledger.path.unlink(missing_ok=True)
    key = load_customer_model_key("anthropic")
    agent = BridgeRepairAgent(model=args.model)
    (out / "meta.json").write_text(
        json.dumps(
            {
                "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "engine_git_head": _git_head(),
                "benchmark": "silent-saves 1.1.0 (#1380), UI-change blocks only",
                "blocks": {b: [BLOCKS[b].start, BLOCKS[b].stop - 1] for b in blocks},
                "program": str(DEFAULT_BUNDLE).replace(str(Path.home()), "~"),
                "loop": "AutoRepair over SelfRepair (bridge up to 3); target as arm 3r",
                "agent": agent.agent_id,
                "approver": APPROVER,
                "fault_probe": "missing (Jane Sample's referral removed)",
            },
            indent=2,
        )
        + "\n"
    )
    app = MockMed(port=PORT)
    arm = SelfRepairProgram(app)
    bundle = arm.original
    rows: list[dict[str, Any]] = []
    try:
        with shared_browser(headless=True):
            for it in items:
                app.prepare(it.fault, it.drift, it.slow_ms, it.idempotency_key)
                before = app.records()
                item_dir = work / f"item-{it.item:03d}"
                item_dir.mkdir(parents=True)
                arm._state = (app.fault, app.drift, app.slow_ms, app.idempotency_key)
                target = ReplayTarget(
                    arm._open,
                    effect_verifier=RestRecordVerifier(
                        app.store_base, records_path="/api/db", records_key="records"
                    ),
                    policy="clinical-write",
                    profile="standard",
                    params=dict(it.params),
                    entry_url=app.url,
                    execution_origin=app.base,
                )
                decisions: list[float] = []
                spent0 = agent.cost_usd
                repair = SelfRepair(
                    agent,
                    target,
                    store=work / "repair-store",
                    screen_access=ScreenAccess(
                        enabled_by="benchmark-operator (synthetic data only)",
                        mode="allow_unscrubbed",
                    ),
                    model_key=key,
                    fault_probes=("missing",),
                    approval_wait_s=120,
                    sleep=_approver(item_dir, decisions),
                )
                loop = AutoRepair(
                    repair,
                    ledger=ledger,
                    labels={
                        "app": "mockmed",
                        "benchmark": "silent-saves-1.1.0",
                        "drift": ",".join(it.drift),
                        "fault": it.fault,
                        "item": str(it.item),
                    },
                )
                t0 = time.time()
                status, reason, claimed = "harness_error", "", False
                result: Optional[Any] = None
                try:
                    result = loop.run(bundle, item_dir / "run")
                    status, reason = result.status, result.evidence.reason
                    claimed = bool(result.verified)
                except Exception as exc:  # noqa: BLE001 - a harness error is a row
                    reason = f"{type(exc).__name__}: {exc}"
                finally:
                    app.fault, app.drift, app.slow_ms, app.idempotency_key = arm._state
                wall = time.time() - t0
                time.sleep(0.5)
                after = app.records()
                verdict = judge.store_verdict(before, after, note=it.params["note"])
                outcome = judge.outcome(claimed, verdict, store_changed=before != after)
                if (
                    result is not None
                    and result.status == "resumed"
                    and result.verified
                    and result.patched_bundle
                ):
                    bundle = Path(result.patched_bundle)
                rows.append(
                    {
                        "item": it.item,
                        "drift": ",".join(it.drift),
                        "fault": it.fault,
                        "expect": it.expect,
                        "repair_status": status,
                        "claimed_success": claimed,
                        "store_verdict": verdict,
                        "outcome": outcome,
                        "ran_repaired_program": bundle != arm.original,
                        "cost_usd": round(agent.cost_usd - spent0, 6),
                        "wall_s": round(wall, 1),
                        "reason": (reason or "")[:300],
                    }
                )
                print(
                    f"{it.item:3d} {','.join(it.drift):8s} {it.fault:12s} "
                    f"{status:16s} {outcome:14s} ${agent.cost_usd - spent0:.3f} "
                    f"{wall:.0f}s {reason[:100]}",
                    flush=True,
                )
    finally:
        app.close()
        if rows:
            with (out / "items.csv").open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
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
        by_block = {}
        for b in blocks:
            mine = [r for r in rows if r["drift"] == b]
            ok_items = [r for r in mine if r["fault"] == "ok"]
            by_block[b] = {
                "items": len(mine),
                "ok_items": len(ok_items),
                "ok_items_correct": sum(r["outcome"] == "correct" for r in ok_items),
                "outcomes": {
                    o: sum(r["outcome"] == o for r in mine)
                    for o in sorted({r["outcome"] for r in mine})
                },
            }
        (out / "summary.json").write_text(
            json.dumps(
                {
                    "ok_items": sum(b["ok_items"] for b in by_block.values()),
                    "ok_items_correct": sum(
                        b["ok_items_correct"] for b in by_block.values()
                    ),
                    "by_block": by_block,
                    "funnel": summarize(ledger.rows()) if ledger.path.is_file() else {},
                    "agent_calls": len(agent.calls),
                    "agent_cost_usd": round(agent.cost_usd, 6),
                },
                indent=2,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
