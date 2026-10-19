"""Run one executor over the fault set and score every item from the store."""

from __future__ import annotations

import csv
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import executors, judge, metrics, protocol
from .app import MockMed
from .readback import ReadBack

HERE = Path(__file__).resolve().parent
FIELDS = (
    "item",
    "fault",
    "executor",
    "claimed_success",
    "readback_confirmed",
    "reported_success",
    "store_verdict",
    "store_changed",
    "outcome",
    "model_calls",
    "cost_usd",
    "item_s",
    "readback_s",
    "host_load_1m",
    "final_text",
    "error",
)
#: A double-sent save lands after the first answer; wait before reading.
SETTLE_S = 1.0


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def run(
    agent: str,
    *,
    out: Path,
    per_fault: int = protocol.FAULT_SET["per_fault_default"],
    faults: tuple[str, ...] = protocol.FAULTS,
    readback: bool = False,
    port: int = 0,
    headed: bool = False,
    max_cost_usd: float = 0.5,
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / "items.csv"
    rows: list[dict[str, Any]] = (
        list(csv.DictReader(rows_path.open())) if rows_path.is_file() else []
    )
    done = {int(r["item"]) for r in rows}
    items = protocol.plan(per_fault, faults)
    app = MockMed(port=port)
    executor: Any = None
    try:
        executor = executors.load(
            agent, app=app, headed=headed, max_cost_usd=max_cost_usd
        )
        (out / "meta.json").write_text(
            json.dumps(
                {
                    "benchmark": protocol.FAULT_SET["name"],
                    "version": protocol.VERSION,
                    "protocol": protocol.PROTOCOL,
                    "app": protocol.FAULT_SET["app"],
                    "executor": executor.name,
                    "agent_spec": agent,
                    "readback": readback,
                    "per_fault": per_fault,
                    "faults": list(faults),
                    "git_head": _git_head(),
                    "machine": f"{platform.machine()} {platform.system()} {platform.release()}",
                    "python": platform.python_version(),
                    "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                },
                indent=2,
            )
            + "\n"
        )
        check = ReadBack(app.store_base, out) if readback else None
        for it in items:
            if it.item in done:
                continue
            item_dir = out / "work" / f"item-{it.item:03d}"
            item_dir.mkdir(parents=True, exist_ok=True)
            app.prepare(it.fault)
            before = app.records()
            if check is not None:
                check.before(item_dir, it.params)
            task = executors.Task(
                item=it.item,
                url=app.url,
                instruction=it.instruction,
                params=dict(it.params),
                work_dir=item_dir,
            )
            load0 = round(os.getloadavg()[0], 2)
            t0 = time.monotonic()
            report: executors.Report = executor.run(task)
            item_s = time.monotonic() - t0
            time.sleep(SETTLE_S)
            after = app.records()
            verdict = judge.store_verdict(before, after, note=it.params["note"])
            row: dict[str, Any] = {
                "item": it.item,
                "fault": it.fault,
                "executor": executor.name,
                "claimed_success": report.claimed_success,
                "readback_confirmed": "",
                "reported_success": report.claimed_success,
                "store_verdict": verdict,
                "store_changed": before != after,
                "model_calls": report.model_calls,
                "cost_usd": round(report.cost_usd, 6),
                "item_s": round(item_s, 3),
                "readback_s": "",
                "host_load_1m": load0,
                "final_text": report.final_text[:600],
                "error": report.error or "",
            }
            if check is not None:
                r0 = time.monotonic()
                rb = check.after(
                    item_dir,
                    executor.name,
                    report.claimed_success,
                    report.final_text,
                    f"SILENT-SAVES-{out.name}-{it.item:03d}",
                )
                row["readback_s"] = round(time.monotonic() - r0, 3)
                row["readback_confirmed"] = rb["readback_confirmed"]
                row["reported_success"] = (
                    report.claimed_success and rb["readback_confirmed"]
                )
                if rb["readback_stderr"]:
                    row["error"] = (row["error"] + " " + rb["readback_stderr"]).strip()
            row["outcome"] = judge.outcome(
                row["reported_success"], verdict, store_changed=row["store_changed"]
            )
            new = not rows_path.is_file()
            with rows_path.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
            rows.append(row)
            print(
                f"silent-saves {it.item:3d} {it.fault:12s} "
                f"claimed={report.claimed_success!s:5s} store={verdict:16s} "
                f"-> {row['outcome']}  {item_s:5.1f}s",
                flush=True,
            )
    finally:
        if executor is not None and hasattr(executor, "close"):
            executor.close()
        app.close()
    summary = {
        "version": protocol.VERSION,
        "protocol": protocol.PROTOCOL,
        "executor": rows[0]["executor"] if rows else agent,
        "readback": readback,
        **metrics.summarize(rows),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def print_summary(summary: dict[str, Any], file: Any = sys.stdout) -> None:
    sw = summary["silent_wrong_save_rate"]
    ts = summary["task_success"]
    fa = summary["false_alarm_rate"]
    secs = summary["seconds_per_item"]

    def pct(r: dict[str, Any]) -> str:
        if not r["n"]:
            return "n/a"
        lo, hi = r["ci95"]
        return f"{r['k']} of {r['n']} ({100 * r['rate']:.0f}%, 95% CI {100 * lo:.0f}-{100 * hi:.0f}%)"

    lines = [
        f"executor: {summary['executor']}"
        + ("  + OpenAdapt read-back" if summary.get("readback") else ""),
        f"silent wrong-save rate: {pct(sw)} bad saves reported as success",
        f"task success:           {pct(ts)} clean items saved and reported",
        f"false alarms:           {pct(fa)} correct saves reported as failed",
        f"seconds per item:       median {secs['median']}, p90 {secs['p90']}",
        f"cost per item:          ${summary['cost_per_item_usd']}",
    ]
    print("\n".join(lines), file=file)


def default_out(agent: str, readback: bool, run_id: Optional[str]) -> Path:
    name = agent.split(":", 1)[0] + ("+readback" if readback else "")
    return HERE / "results" / name / (run_id or time.strftime("%Y-%m-%d-%H%M%S"))
