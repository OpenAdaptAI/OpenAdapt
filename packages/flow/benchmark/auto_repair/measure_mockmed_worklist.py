"""Measure a paused worklist resuming after one repair, on MockMed.

    python benchmark/auto_repair/measure_mockmed_worklist.py --run-id 2026-10-19-a --items 5

For each drift mode, :class:`openadapt_flow.repair.preflight_queue.WorklistQueue`
runs the tutorial's triage program over ``--items`` items. The pre-flight on the
test record passes; then the vendor ships the drift, before item 1. Every item
that stops waits; self-repair runs once on the test record with the real
Claude repair agent from ``measure_mockmed.py``; the scripted approver answers
the one request; the waiting items re-run from the start with the repaired
program and their own record check.

After every run this reads MockMed's store itself. Every session starts from
an empty store (the recorded record check pins one record), so an item is
``correct`` when its last run left exactly the intended record, ``safe_stop``
when it left nothing, and ``wrong`` otherwise.

Outputs (committed): ``results/mockmed-worklist-<run-id>/`` with
``ledger.jsonl``, ``items.csv``, ``summary.json``, ``calls.json`` and
``meta.json``. Synthetic data only.
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

from measure_mockmed import (  # noqa: E402
    APPROVER,
    CACHE,
    QUERY,
    BridgeRepairAgent,
    _approver,
    _bundle,
    _git_head,
    _readback,
)

#: run_dir -> what MockMed's store held right after that run.
STORE: dict[str, str] = {}


def _readback_target(base: type) -> type:
    class ReadBack(base):  # type: ignore[misc, valid-type]
        """A ReplayTarget that reads MockMed's store after every run."""

        def __init__(self, base_url: str, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            self.base_url = base_url

        def execute(self, workflow, bundle_dir, session, run_dir, **kw):  # type: ignore[no-untyped-def]
            report = super().execute(workflow, bundle_dir, session, run_dir, **kw)
            outcome, count = _readback(self.base_url)
            STORE[str(run_dir)] = f"{outcome}:{count}"
            return report

    return ReadBack


def _target(base_url: str, vendor: dict, on_open: Any = None) -> Any:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.repair.self_repair import AppSession, ReplayTarget
    from openadapt_flow.runtime.effects import RestRecordVerifier
    from openadapt_flow.tutorial import TUTORIAL_NOTE, _http_json

    _ReadBack = _readback_target(ReplayTarget)

    def url(fault: Optional[str]) -> str:
        drifts = ",".join(d for d in (vendor["drift"], fault) if d)
        return f"{base_url}/{QUERY}" + (f"&drift={drifts}" if drifts else "") + "#tasks"

    def open_session(fault: Optional[str]) -> AppSession:
        if on_open is not None:
            on_open()
        _http_json(f"{base_url}/api/reset", method="POST", body={})
        backend, close = PlaywrightBackend.launch(url(fault), headless=True)
        return AppSession(backend=backend, close=close)

    return _ReadBack(
        base_url,
        open_session,
        effect_verifier=RestRecordVerifier(
            base_url,
            records_path="/api/db",
            records_key="records",
            timeout_s=2.0,
            poll_interval_s=0.05,
        ),
        policy="clinical-write",
        params={"note": TUTORIAL_NOTE},
        entry_url=url(None),
        execution_origin=base_url,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--items", type=int, default=5)
    parser.add_argument("--modes", default="relink,redesign")
    parser.add_argument("--model", default="claude-sonnet-5-5")
    args = parser.parse_args()

    from openadapt_flow.backends._browser_process import shared_browser
    from openadapt_flow.jit import load_customer_model_key
    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.repair.auto_repair import RepairLedger, summarize
    from openadapt_flow.repair.preflight_queue import QueueItem, WorklistQueue
    from openadapt_flow.repair.self_repair import ScreenAccess, SelfRepair
    from openadapt_flow.repair.standard_change import TestRecord

    out = HERE / "results" / f"mockmed-worklist-{args.run_id}"
    out.mkdir(parents=True, exist_ok=True)
    work = CACHE / f"mockmed-worklist-{args.run_id}"
    work.mkdir(parents=True, exist_ok=True)
    ledger = RepairLedger(out)
    ledger.path = out / "ledger.jsonl"
    key = load_customer_model_key("anthropic")
    agent = BridgeRepairAgent(model=args.model)
    modes = [m for m in args.modes.split(",") if m]
    (out / "meta.json").write_text(
        json.dumps(
            {
                "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "engine_git_head": _git_head(),
                "app": "MockMed, synthetic data",
                "loop": "WorklistQueue: pre-flight, items, self-repair on the test record",
                "drift_ships": "after the pre-flight, before item 1",
                "items_per_mode": args.items,
                "agent": agent.agent_id,
                "approver": APPROVER,
                "fault_probe": "drift=<mode>,missing",
                "modes": modes,
            },
            indent=2,
        )
        + "\n"
    )
    rows: list[dict[str, Any]] = []
    base_url, _db, stop = serve()
    try:
        with shared_browser(headless=True):
            bundle = _bundle(work, base_url)
            for mode in modes:
                root = work / mode
                shutil.rmtree(root, ignore_errors=True)
                vendor: dict[str, Optional[str]] = {"drift": None}

                def ship(vendor: dict = vendor, mode: str = mode) -> None:
                    vendor["drift"] = mode

                decisions: list[float] = []

                def factory(target: Any, root: Path = root, decisions=decisions) -> Any:
                    return SelfRepair(
                        agent,
                        target,
                        store=root / "repair-store",
                        screen_access=ScreenAccess(
                            enabled_by="benchmark-operator", mode="allow_unscrubbed"
                        ),
                        model_key=key,
                        fault_probes=("missing",),
                        approval_wait_s=120,
                        sleep=_approver(root / "queue", decisions),
                    )

                queue = WorklistQueue(
                    bundle,
                    TestRecord("TEST-001", _target(base_url, vendor)),
                    root / "queue",
                    self_repair=factory,
                    ledger=ledger,
                    labels={"app": "mockmed", "drift": mode, "worklist": "1"},
                )
                items = [
                    QueueItem(f"item-{i}", _target(base_url, vendor, on_open=ship))
                    for i in range(1, args.items + 1)
                ]
                t0 = time.time()
                result = queue.run(items)
                wall = time.time() - t0
                for item in result.items:
                    # The store holds what the item's last session wrote.
                    readback = STORE.get(str(item.run_dir), "not_checked").split(":")[0]
                    rows.append(
                        {
                            "mode": mode,
                            "item": item.item_id,
                            "status": item.status,
                            "runs": item.runs,
                            "readback": readback,
                            "queue_status": result.status,
                            "wall_s": round(wall, 1),
                        }
                    )
                print(
                    mode,
                    result.status,
                    [(i.item_id, i.status, i.runs) for i in result.items],
                    f"{wall:.0f}s",
                    flush=True,
                )
    finally:
        stop()
        with (out / "items.csv").open("w", newline="") as f:
            fields = [
                "mode",
                "item",
                "status",
                "runs",
                "readback",
                "queue_status",
                "wall_s",
            ]
            writer = csv.DictWriter(f, fieldnames=fields)
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
            data = ledger.rows()
            (out / "summary.json").write_text(
                json.dumps(
                    {
                        "overall": summarize(data),
                        "by_mode": {
                            m: summarize([r for r in data if r["labels"]["drift"] == m])
                            for m in modes
                        },
                    },
                    indent=2,
                )
                + "\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
