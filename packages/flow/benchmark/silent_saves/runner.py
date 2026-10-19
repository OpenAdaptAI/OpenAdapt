"""Run one executor over the fault set and score every item from the store."""

from __future__ import annotations

import csv
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import executors, judge, metrics, protocol, spend
from .app import MockMed
from .readback import ReadBack

HERE = Path(__file__).resolve().parent
FIELDS = (
    "item",
    "fault",
    "kind",
    "expect",
    "release",
    "drift",
    "executor",
    "claimed_success",
    "readback_confirmed",
    "reported_success",
    "store_verdict",
    "store_changed",
    "outcome",
    "outcome_alone",
    "model_calls",
    "cost_usd",
    "item_s",
    "readback_s",
    "host_load_1m",
    "human_events",
    "human_minutes",
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
    fault_set: str = protocol.DEFAULT,
    per_fault: int = protocol.FAULT_SET["per_fault_default"],
    faults: tuple[str, ...] = protocol.FAULTS,
    limit: Optional[int] = None,
    readback: bool = False,
    port: int = 0,
    headed: bool = False,
    max_cost_usd: float = 0.25,
    arm: str = "",
    arm_cap_usd: float = 0.0,
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / "items.csv"
    stopped = ""
    rows: list[dict[str, Any]] = (
        list(csv.DictReader(rows_path.open())) if rows_path.is_file() else []
    )
    done = {int(r["item"]) for r in rows}
    fs = protocol.load(fault_set)
    change = fs.get("kind") == "change"
    items = (
        protocol.plan_change(fs, limit) if change else protocol.plan(per_fault, faults)
    )
    app = MockMed(port=port)
    executor: Any = None
    try:
        executor = executors.load(
            agent,
            app=app,
            headed=headed,
            max_cost_usd=max_cost_usd,
            arm=arm,
            arm_cap_usd=arm_cap_usd,
        )
        (out / "meta.json").write_text(
            json.dumps(
                {
                    "benchmark": fs["name"],
                    "version": fs["version"],
                    "protocol": fs["protocol"],
                    "app": fs["app"],
                    "executor": executor.name,
                    "agent_spec": agent,
                    "readback": readback,
                    "arm": arm or None,
                    "arm_cap_usd": arm_cap_usd or None,
                    "items": len(items),
                    "per_fault": None if change else per_fault,
                    "faults": None if change else list(faults),
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
            app.prepare(it.fault, it.drift, it.slow_ms, it.idempotency_key)
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
            events = list(report.extra.get("human_events", []))
            row: dict[str, Any] = {
                "item": it.item,
                "fault": it.fault,
                "kind": it.kind,
                "expect": it.expect,
                "release": it.release,
                "drift": ",".join(it.drift),
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
                "human_events": json.dumps(events) if events else "",
                "human_minutes": round(
                    sum(float(e.get("minutes", 0) or 0) for e in events), 2
                ),
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
            # The executor's own claim, scored without the read-back gate.
            row["outcome_alone"] = judge.outcome(
                report.claimed_success, verdict, store_changed=row["store_changed"]
            )
            for name in ("ledger.json", "repair-ledger.json"):
                src = item_dir / name
                if src.is_file():
                    dest = out / "ledgers" / f"item-{it.item:03d}-{name}"
                    dest.parent.mkdir(exist_ok=True)
                    shutil.copyfile(src, dest)
            new = not rows_path.is_file()
            with rows_path.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
            rows.append(row)
            if hasattr(executor, "after_score"):
                # Work that must not touch the scored store, such as arm 2's
                # admission canary, runs here; the next item resets the store.
                executor.after_score(task)
            print(
                f"silent-saves {it.item:3d} {it.fault:12s} {it.release[:18]:18s} "
                f"claimed={report.claimed_success!s:5s} store={verdict:16s} "
                f"-> {row['outcome']}  {item_s:5.1f}s",
                flush=True,
            )
    except spend.OverBudget as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        stopped = str(exc)
    finally:
        if executor is not None and hasattr(executor, "close"):
            executor.close()
        app.close()
    summary = {
        "version": fs["version"],
        "protocol": fs["protocol"],
        "executor": rows[0]["executor"] if rows else agent,
        "readback": readback,
        **(metrics.summarize_change(rows) if change else metrics.summarize(rows)),
    }
    if stopped:
        summary["stopped"] = stopped
    if readback and rows:
        alone = [
            {
                **r,
                "outcome": r["outcome_alone"],
                "reported_success": r["claimed_success"],
            }
            for r in rows
        ]
        summary["without_readback"] = (
            metrics.summarize_change(alone) if change else metrics.summarize(alone)
        )
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def _frac(r: dict[str, Any]) -> str:
    if not r["n"]:
        return "n/a"
    lo, hi = r["ci95"]
    return f"{r['k']} of {r['n']} ({100 * r['rate']:.0f}%, 95% CI {100 * lo:.0f}-{100 * hi:.0f}%)"


def print_change_summary(summary: dict[str, Any], file: Any = sys.stdout) -> None:
    h = summary["human"]
    secs = summary["seconds_per_item"]
    cpv = summary["cost_per_verified_item_usd"]
    lines = [
        f"executor: {summary['executor']}"
        + ("  + OpenAdapt read-back" if summary.get("readback") else "")
        + f"  (fault set {summary['version']}, {summary['items']} items)",
        f"verified saves:        {_frac(summary['verified_saves'])} of items with a correct save to make",
        f"silent wrong saves:    {_frac(summary['silent_wrong_saves'])} of all items",
        f"false stops:           {_frac(summary['false_stops'])} of items with a correct save to make",
        f"detection:             {_frac(summary['detection']['silent_wrong'])} server-fault bad saves reported as success",
        f"prevention:            {_frac(summary['prevention']['wrong_saves_landed'])} trap items where a wrong record landed",
        f"                       {_frac(summary['prevention']['stopped_when_no_correct_save'])} stopped when no correct save existed",
        f"human:                 {h['minutes_per_100_items']} logged minutes per 100 items "
        f"({h['events']} events, {h['scripted_events']} scripted); "
        f"{h['items_left_for_a_person']} items left for a person",
        f"cost per verified item: {'n/a' if cpv is None else f'${cpv}'}",
        f"seconds per item:      median {secs['median']}, p90 {secs['p90']}",
    ]
    print("\n".join(lines), file=file)


def print_summary(summary: dict[str, Any], file: Any = sys.stdout) -> None:
    if "verified_saves" in summary:
        print_change_summary(summary, file)
        return
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


def default_out(
    agent: str,
    readback: bool,
    run_id: Optional[str],
    fault_set: str = protocol.DEFAULT,
) -> Path:
    name = agent.split(":", 1)[0] + ("+readback" if readback else "")
    root = (
        HERE / "results"
        if fault_set == protocol.DEFAULT
        else HERE / "results" / fault_set
    )
    return root / name / (run_id or time.strftime("%Y-%m-%d-%H%M%S"))
