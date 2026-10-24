"""Run the lifecycle soak: one seeded worklist, every arm, one results ledger.

    cd packages/flow/benchmark
    python -m lifecycle.run --app openemr --seed 7 --items 300 --days 10 \\
        --arm lifecycle.arms.openadapt:make --out lifecycle/results/<run id>

Each arm gets the app reset to the same baseline with the same existing
charts, then the same stream of items, UI changes and faults. The harness
applies every event, asks the oracle after every item, and writes every row
to ``ledger.jsonl``; ``results.json`` and ``REPORT.md`` are computed from that
file alone (``python -m lifecycle.report <run dir>`` recomputes them).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .arm import INTERFACE_VERSION, Env, ItemResult, OperatorRequest, load_arm
from .cost_model import MODELS, V1
from .metrics import classify
from .operator import ScriptedOperator
from .oracle import Oracle
from .workload import WORKLOAD_VERSION, Truth, Workload, generate

HERE = Path(__file__).resolve().parent
PROTOCOL_VERSION = "lifecycle/2026-10-20.1"


def load_1m() -> Optional[float]:
    try:
        return round(os.getloadavg()[0], 1)
    except OSError:
        return None


class Ledger:
    """Append-only JSON lines; every row is flushed as it is written."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._fh = path.open("a", encoding="utf-8")
        self.arm = ""

    def write(self, row: dict[str, Any]) -> None:
        row = {"t": round(time.time(), 3), "arm": self.arm, **row}
        with self._lock:
            self._fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class _CountingOperator:
    """Wraps the operator to know which items already reached a person."""

    def __init__(self, inner: ScriptedOperator) -> None:
        self.inner = inner
        self.asked: dict[str, int] = {}

    def ask(self, req: OperatorRequest) -> Any:
        if req.item_id:
            self.asked[req.item_id] = self.asked.get(req.item_id, 0) + 1
        return self.inner.ask(req)


def engine_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=HERE,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_arm(
    *,
    arm_spec: str,
    arm_options: dict[str, Any],
    app: Any,
    workload: Workload,
    ledger: Ledger,
    out: Path,
    cost_name: str,
    stop_after: Optional[int] = None,
    say: Callable[[str], None] = print,
) -> None:
    arm = load_arm(arm_spec, arm_options)
    ledger.arm = arm.name
    truth = workload.truth
    cost = MODELS[cost_name]

    say(f"[{arm.name}] resetting the app to its baseline")
    app.reset()
    app.clear_all_events()
    existing_ids = app.seed_existing(workload.existing)
    ledger.write(
        {
            "type": "arm_start",
            "arm_spec": arm_spec,
            "options": arm_options,
            "describe": arm.describe(),
            "existing_ids": existing_ids,
            "interface": INTERFACE_VERSION,
        }
    )

    oracle = Oracle(app.rows)
    harness_ids: set[str] = set()
    harness_touched: set[str] = set()
    other_clerk_ids: set[str] = set()
    active_drift: list[str] = []
    current: dict[str, Any] = {"before": None, "seq": 0}

    def do_by_hand(t: Truth) -> dict[str, Any]:
        """The operator enters the item in the app themself."""
        if t.intended is None:
            return {}
        new_id = app.create_chart(t.intended, who="operator")
        harness_ids.add(new_id)
        return {"chart_id": new_id}

    def record(row: dict[str, Any]) -> None:
        ledger.write({**row, "seq": current["seq"]})

    scripted = ScriptedOperator(
        cost,
        truth,
        look=lambda t: oracle.look(t, current["before"]),
        do_by_hand=do_by_hand,
        drift_active=lambda: list(active_drift),
        record=record,
    )
    operator = _CountingOperator(scripted)
    env = Env(
        app=workload.items[0].app,
        ui_url=app.ui_url,
        api_url=app.api_url,
        accounts=tuple(app.accounts()),
        workdir=out / "arms" / arm.name,
        seed=workload.seed,
        extra=app.extra(),
    )
    env.workdir.mkdir(parents=True, exist_ok=True)

    say(f"[{arm.name}] setup")
    for phase in arm.setup(env, operator):
        ledger.write({"type": "phase", "stage": "setup", **phase})

    drift_by_seq = {d["first_seq"]: d for d in workload.drift}
    items = workload.items[: stop_after or len(workload.items)]
    for item in items:
        current["seq"] = item.seq
        t = truth[item.item_id]
        if item.seq in drift_by_seq:
            d = drift_by_seq[item.seq]
            applied = app.apply_drift(d["kind"])
            active_drift.append(d["kind"])
            ledger.write({"type": "drift_applied", **d, "applied": applied})
            say(
                f"[{arm.name}] day {d['day']}: UI change '{d['kind']}' from item {item.seq}"
            )
            for phase in arm.after_drift(d, env, operator):
                ledger.write(
                    {
                        "type": "phase",
                        "stage": "maintenance",
                        "drift": d["kind"],
                        **phase,
                    }
                )

        before = oracle.snapshot()
        current["before"] = before
        injected: list[dict[str, Any]] = []
        for inj in t.inject:
            if inj["when"] == "before_item":
                injected.append(app.inject(inj, t, harness=harness_ids))
        for inj in injected:
            if inj.get("chart_id"):
                other_clerk_ids.add(inj["chart_id"])
                harness_ids.add(inj["chart_id"])
        stop_during = threading.Event()
        during: list[threading.Thread] = []
        during_out: list[dict[str, Any]] = []
        for inj in t.inject:
            if inj["when"] == "during_item":
                th = threading.Thread(
                    target=lambda i=inj: during_out.append(
                        app.inject(i, t, harness=harness_ids, stop=stop_during)
                    ),
                    daemon=True,
                )
                th.start()
                during.append(th)

        load_start = load_1m()
        started = time.time()
        try:
            result = arm.run_item(item, env, operator)
        except Exception as exc:  # noqa: BLE001 - an arm crash is a result
            if type(exc).__name__ == "SpendCapReached":
                stop_during.set()
                app.clear_item_events()
                ledger.write(
                    {
                        "type": "arm_paused",
                        "seq": item.seq,
                        "reason": str(exc)[:300],
                        "paused_items": len(items) - item.seq + 1,
                    }
                )
                say(f"[{arm.name}] spend cap reached at item {item.seq}; arm paused")
                break
            result = ItemResult(
                item_id=item.item_id,
                claimed="error",
                product_status=f"{type(exc).__name__}: {exc}"[:500],
                wall_s=time.time() - started,
            )
        stop_during.set()
        for th in during:
            th.join(timeout=60)
        cleared = app.clear_item_events()
        for phase in [*result.phases, *result.detail.get("maintenance_phases", [])]:
            ledger.write(
                {
                    "type": "phase",
                    "stage": "maintenance",
                    "seq": item.seq,
                    "within_item": item.item_id,
                    **phase,
                }
            )
        for d in during_out:
            harness_touched.update(d.get("touched_ids", []))

        after = oracle.snapshot()
        verdict = oracle.judge_arm(
            t, before, after, harness_ids=harness_ids, harness_touched=harness_touched
        )
        outcome = classify(
            claimed=result.claimed,
            oracle=verdict["verdict"],
            chart_wanted=t.intended is not None,
            exception=t.exception,
        )
        # A stop the arm never put in front of a person still needs one.
        if result.claimed != "done" and not operator.asked.get(item.item_id):
            operator.ask(
                OperatorRequest(
                    kind="stop",
                    item_id=item.item_id,
                    reason=result.product_status[:300],
                    surface="harness:stopped-item queue",
                    detail={"resumable": False},
                )
            )
        final = oracle.judge_final(
            t, before, oracle.snapshot(), other_clerk_ids=other_clerk_ids
        )
        # Context for duplicate-check stops: which identity values this item
        # shares with a chart that existed when it started (ground truth, for
        # the report; never shown to the arm).
        shares = {
            key: sum(
                1
                for r in before
                if str(item.params.get(key, "")).strip().casefold()
                and r.get(key, "").strip().casefold()
                == str(item.params.get(key, "")).strip().casefold()
            )
            for key in ("fname", "lname", "DOB")
        }
        row = {
            "type": "item_result",
            "shares_with_existing": shares,
            "seq": item.seq,
            "item_id": item.item_id,
            "day": item.day,
            "exception": t.exception,
            "chart_wanted": t.intended is not None,
            "drift_active": list(active_drift),
            "claimed": result.claimed,
            "product_status": result.product_status,
            "oracle": verdict,
            "outcome": outcome,
            "final_oracle": final["verdict"],
            "final": final,
            "wall_s": round(result.wall_s, 2),
            "harness_wall_s": round(time.time() - started, 2),
            "model_calls": result.model_calls,
            "cost_usd": result.cost_usd,
            "evidence": result.evidence,
            "detail": result.detail,
            "load_1m": load_start,
            "load_1m_end": load_1m(),
            "injected": injected + during_out,
            "cleared": cleared,
        }
        ledger.write(row)
        say(
            f"[{arm.name}] {item.seq:4d} {item.item_id} {t.exception:22s} "
            f"claimed={result.claimed:7s} oracle={verdict['verdict']:9s} -> {outcome:13s} "
            f"final={final['verdict']:8s} {result.wall_s:6.1f}s load={load_start}"
        )
    arm.close()
    app.clear_all_events()
    ledger.write({"type": "arm_end"})


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--app", default="openemr", choices=("openemr", "fake"))
    p.add_argument(
        "--arm",
        action="append",
        required=True,
        help="module:factory; repeat for several arms",
    )
    p.add_argument("--arm-options", default="{}", help="JSON options for every arm")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--items", type=int, default=300)
    p.add_argument("--days", type=int, default=10)
    p.add_argument("--existing", type=int, default=12)
    p.add_argument(
        "--stop-after",
        type=int,
        default=None,
        help="run only the first N items (a smoke run)",
    )
    p.add_argument(
        "--drift",
        action="append",
        default=None,
        metavar="KIND@SEQ",
        help="replace the drift schedule: apply UI change KIND before item SEQ "
        "(repeatable), e.g. relabel_save@26",
    )
    p.add_argument("--cost-model", default=V1.version, choices=sorted(MODELS))
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=False)
    workload = generate(args.seed, args.items, args.days, args.existing)
    if args.drift is not None:
        by_seq = {i.seq: i for i in workload.items}
        workload.drift = []
        for spec in args.drift:
            kind, _, seq = spec.partition("@")
            workload.drift.append(
                {"day": by_seq[int(seq)].day, "kind": kind, "first_seq": int(seq)}
            )
    (out / "workload.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in workload.ledger_rows())
    )
    from .apps import make_app

    app = make_app(args.app)
    ledger = Ledger(out / "ledger.jsonl")
    ledger.write(
        {
            "type": "run_start",
            "protocol": PROTOCOL_VERSION,
            "workload": WORKLOAD_VERSION,
            "seed": args.seed,
            "items": args.items,
            "days": args.days,
            "stop_after": args.stop_after,
            "cost_model": MODELS[args.cost_model].to_dict(),
            "engine_commit": engine_commit(),
            "host": platform.node().split(".")[0],
            "platform": platform.platform(),
            "cpus": os.cpu_count(),
            "app": app.describe(),
            "argv": argv if argv is not None else None,
        }
    )
    try:
        for spec in args.arm:
            run_arm(
                arm_spec=spec,
                arm_options=json.loads(args.arm_options),
                app=app,
                workload=workload,
                ledger=ledger,
                out=out,
                cost_name=args.cost_model,
                stop_after=args.stop_after,
            )
    finally:
        ledger.write({"type": "run_end"})
        ledger.close()
    from .report import write_report

    write_report(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
