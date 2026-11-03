"""Validate a task pack: every task's oracle must tell right from wrong.

    python -m openadapt_flow.taskgen.validate packs/mockmed --profile mockmed \\
        --pg "host=127.0.0.1 port=9770 user=wb_admin" --workers 2

For each task, on a fresh copy of the app:

1. **positive**: reset to the task's world, read the baseline with the
   task's contract, run the scripted solution through the screens, judge
   with ``verify_run``. It must be ``CONFIRMED``.
2. **planted**: for each planted variant (the family's trap taken, an app
   fault), the same, with the variant's setup and solution. None may be
   ``CONFIRMED``: each must be ``REFUTED`` or ``INDETERMINATE``.

The solution reaching ``done`` through the UI alone is the "solvable from
the UI" check: no action reads or writes the store.

A task is **valid** when both hold. ``results.jsonl`` holds one line per
episode, ``validity.json`` the rates per app, kind and family and the list of
rejected tasks with the reason. The judge is the one the OpenEnv
environment's reward uses (``capture_baseline`` and ``verify_run``).
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

import yaml

from openadapt_flow.taskgen.contracts import READER_PASSWORD_ENV

CONFIRMED = "CONFIRMED"
NOT_CONFIRMED = ("REFUTED", "INDETERMINATE")


def load_contract(path: Path, dbname: Optional[str]) -> Any:
    """The task's contract; ``dbname`` points it at a worker's copy of the
    database (the contract is otherwise unchanged)."""

    from openadapt_flow.verifier import Contract

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if dbname:
        text = json.dumps(doc)
        text = text.replace(
            '"dbname": "' + _dbname(doc) + '"', '"dbname": "' + dbname + '"'
        )
        doc = json.loads(text)
    return Contract.from_dict(doc, name=path.stem)


def _dbname(doc: dict[str, Any]) -> str:
    return str(doc["frame"]["database"]["read"]["sql_connect_args"]["dbname"])


def judge_episode(
    wb: Any,
    browser: Any,
    url: str,
    cfg: Any,
    params: dict[str, str],
    setup: dict[str, Any],
    solution: list[dict[str, Any]],
) -> dict[str, Any]:
    from openadapt_flow.verify_run import capture_baseline, verify_run

    started = time.monotonic()
    wb.reset(setup)
    out: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="taskgen-") as tmp:
        base = Path(tmp) / "baseline.json"
        try:
            capture_baseline(cfg, params, base)
        except Exception as exc:  # noqa: BLE001 - fails closed, reported
            return {
                "verdict": "INDETERMINATE",
                "error": f"baseline: {type(exc).__name__}: {exc}",
                "seconds": 0.0,
            }
        claim = None
        try:
            browser.open(url + "/login")
            claim = browser.run(solution)
            out["ui"] = "reached_done" if claim else "no_done"
        except Exception as exc:  # noqa: BLE001 - a UI failure is a result
            out["ui"] = "action_failed"
            out["ui_error"] = str(exc)[:300]
        try:
            result = verify_run(
                cfg,
                params=params,
                run_record={
                    "executor_kind": "script",
                    "executor_id": "taskgen-validate",
                    "claim": claim,
                },
                authorization_ref="taskgen-validate",
                baseline_path=base,
            )
            receipt = result["receipt"]
            out["verdict"] = str(receipt["effect_verdict"])
            reasons = result.get("reasons") or receipt.get("reasons") or []
            if out["verdict"] != CONFIRMED:
                out["reasons"] = [str(r)[:240] for r in reasons][:4] or [
                    str(receipt.get("outcome"))
                ]
        except Exception as exc:  # noqa: BLE001 - fails closed, reported
            out["verdict"] = "INDETERMINATE"
            out["error"] = f"verify: {type(exc).__name__}: {str(exc)[:300]}"
    out["seconds"] = round(time.monotonic() - started, 2)
    return out


def _worker(args: tuple[int, str, str, str, list[str], str]) -> list[dict[str, Any]]:
    index, profile_name, pg, pack, ids, reader = args
    import psycopg2

    from openadapt_flow.taskgen.profile import load_profile
    from openadapt_flow.taskgen.runner import Browser
    from openadapt_flow.taskgen.workbench import serve

    prof = load_profile(profile_name)
    db = f"wb_{prof.app}_v{prof.version}_val{index}"
    admin = psycopg2.connect(pg + " dbname=postgres")
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute("DROP DATABASE IF EXISTS " + db + " WITH (FORCE)")
    cur.execute("CREATE DATABASE " + db)
    admin.close()
    url, wb, stop = serve(prof, f"{pg} dbname={db}", reader=reader)
    browser = Browser()
    pack_dir = Path(pack)
    tasks = {
        json.loads(line)["id"]: json.loads(line)
        for line in (pack_dir / "tasks.jsonl").read_text().splitlines()
        if line.strip()
    }
    rows: list[dict[str, Any]] = []
    try:
        for tid in ids:
            task = tasks[tid]
            plan = json.loads((pack_dir / "validation" / f"{tid}.json").read_text())
            try:
                cfg = load_contract(pack_dir / task["verify"], db).config
                params = dict(task["params"])
                r = judge_episode(
                    wb, browser, url, cfg, params, task["setup"], task["solution"]
                )
                rows.append({"task_id": tid, "episode": "positive", **r})
                for p in plan["planted"]:
                    setup = {**task["setup"], **p["setup"]}
                    sol = p["solution"] or task["solution"]
                    r = judge_episode(wb, browser, url, cfg, params, setup, sol)
                    rows.append({"task_id": tid, "episode": p["name"], **r})
            except Exception as exc:  # noqa: BLE001 - the task is rejected, the run goes on
                rows.append(
                    {
                        "task_id": tid,
                        "episode": "positive",
                        "verdict": "INDETERMINATE",
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
            print(
                f"[{index}] {tid}: "
                + " ".join(
                    f"{x['episode']}={x['verdict']}"
                    for x in rows
                    if x["task_id"] == tid
                ),
                flush=True,
            )
    finally:
        browser.close()
        stop()
    return rows


def _flow_worker(args: tuple[int, str, str, list[str], str]) -> list[dict[str, Any]]:
    """Validate multi-app tasks: one workbench per app, one contract each."""

    index, pg, pack, ids, reader = args
    import psycopg2

    from openadapt_flow.taskgen.profile import load_profile
    from openadapt_flow.taskgen.runner import Browser
    from openadapt_flow.taskgen.workbench import serve
    from openadapt_flow.verify_run import capture_baseline, verify_run

    pack_dir = Path(pack)
    tasks = {
        json.loads(line)["id"]: json.loads(line)
        for line in (pack_dir / "tasks.jsonl").read_text().splitlines()
        if line.strip()
    }
    names = {
        a: spec["profile"] for t in tasks.values() for a, spec in t["apps"].items()
    }
    servers: dict[str, Any] = {}
    admin = psycopg2.connect(pg + " dbname=postgres")
    admin.autocommit = True
    for name, source in sorted(names.items()):
        prof = load_profile(source)
        db = f"wb_{prof.app}_v{prof.version}_flow{index}"
        admin.cursor().execute("DROP DATABASE IF EXISTS " + db + " WITH (FORCE)")
        admin.cursor().execute("CREATE DATABASE " + db)
        url, wb, stop = serve(prof, f"{pg} dbname={db}", reader=reader)
        servers[name] = (url, wb, stop, db)
    admin.close()
    browser = Browser()
    browser.apps = {n: s[0] for n, s in servers.items()}
    rows: list[dict[str, Any]] = []

    def episode(
        task: dict[str, Any],
        solution: list[dict[str, Any]],
        fault_app: Optional[str],
        fault_setup: dict[str, Any],
    ) -> dict[str, Any]:
        cfgs, bases = {}, {}
        tmp = tempfile.TemporaryDirectory(prefix="taskgen-flow-")
        try:
            for name, spec in task["apps"].items():
                url, wb, _stop, db = servers[name]
                wb.reset(
                    {**spec["setup"], **(fault_setup if name == fault_app else {})}
                )
                cfgs[name] = load_contract(pack_dir / spec["verify"], db).config
                bases[name] = Path(tmp.name) / f"{name}.json"
                capture_baseline(cfgs[name], spec["params"], bases[name])
            first = next(iter(task["apps"]))
            browser.open(servers[first][0] + "/login")
            claim = None
            ui = "reached_done"
            try:
                claim = browser.run(solution)
            except Exception as exc:  # noqa: BLE001
                ui = f"action_failed: {str(exc)[:200]}"
            verdicts = {}
            for name, spec in task["apps"].items():
                r = verify_run(
                    cfgs[name],
                    params=spec["params"],
                    run_record={
                        "executor_kind": "script",
                        "executor_id": "taskgen-validate",
                        "claim": claim,
                    },
                    authorization_ref="taskgen-validate",
                    baseline_path=bases[name],
                )
                verdicts[name] = str(r["receipt"]["effect_verdict"])
            both = all(v == CONFIRMED for v in verdicts.values())
            return {
                "verdict": CONFIRMED
                if both
                else ("REFUTED" if "REFUTED" in verdicts.values() else "INDETERMINATE"),
                "per_app": verdicts,
                "ui": ui,
            }
        finally:
            tmp.cleanup()

    try:
        for tid in ids:
            task = tasks[tid]
            plan = json.loads((pack_dir / "validation" / f"{tid}.json").read_text())
            try:
                rows.append(
                    {
                        "task_id": tid,
                        "episode": "positive",
                        **episode(task, task["solution"], None, {}),
                    }
                )
                for p in plan["planted"]:
                    rows.append(
                        {
                            "task_id": tid,
                            "episode": p["name"],
                            **episode(
                                task,
                                p.get("solution") or task["solution"],
                                p.get("app"),
                                p.get("setup") or {},
                            ),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - the task is rejected
                rows.append(
                    {
                        "task_id": tid,
                        "episode": "positive",
                        "verdict": "INDETERMINATE",
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
            print(
                f"[{index}] {tid}: "
                + " ".join(
                    f"{x['episode']}={x['verdict']}"
                    for x in rows
                    if x["task_id"] == tid
                ),
                flush=True,
            )
    finally:
        browser.close()
        for s in servers.values():
            s[2]()
    return rows


def summarize(pack: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    tasks = [
        json.loads(line)
        for line in (pack / "tasks.jsonl").read_text().splitlines()
        if line.strip()
    ]
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_task[r["task_id"]].append(r)
    verdicts: dict[str, dict[str, Any]] = {}
    for t in tasks:
        eps = by_task.get(t["id"], [])
        pos = [e for e in eps if e["episode"] == "positive"]
        planted = [e for e in eps if e["episode"] != "positive"]
        why = []
        if not pos:
            why.append("not run")
        elif pos[0]["verdict"] != CONFIRMED:
            why.append(
                f"positive {pos[0]['verdict']}: {(pos[0].get('reasons') or [pos[0].get('error', '')])[0]}"
            )
        elif pos[0].get("ui") != "reached_done":
            why.append(
                f"solution did not reach done through the UI: {pos[0].get('ui_error', '')}"
            )
        if not planted:
            why.append("no planted fault")
        for e in planted:
            if e["verdict"] == CONFIRMED:
                why.append(f"planted {e['episode']} was CONFIRMED")
        verdicts[t["id"]] = {"valid": not why, "why": why}

    def rate(keys: list[str]) -> dict[str, Any]:
        n = len(keys)
        v = sum(verdicts[k]["valid"] for k in keys)
        return {"tasks": n, "valid": v, "rate": round(v / n, 4) if n else None}

    groups: dict[str, dict[str, list[str]]] = {
        "app": defaultdict(list),
        "kind": defaultdict(list),
        "family": defaultdict(list),
    }
    for t in tasks:
        tags = dict(tag.split(":", 1) for tag in t["tags"] if ":" in tag)
        groups["app"][tags["app"]].append(t["id"])
        groups["kind"][tags["kind"]].append(t["id"])
        groups["family"][tags["family"]].append(t["id"])
    planted_counts = Counter(
        e["episode"].split(":")[0] + ":" + e["verdict"]
        for e in rows
        if e["episode"] != "positive"
    )
    return {
        "pack": pack.name,
        "overall": rate([t["id"] for t in tasks]),
        "by_app": {k: rate(v) for k, v in sorted(groups["app"].items())},
        "by_kind": {k: rate(v) for k, v in sorted(groups["kind"].items())},
        "by_family": {k: rate(v) for k, v in sorted(groups["family"].items())},
        "planted_verdicts": dict(sorted(planted_counts.items())),
        "episodes": len(rows),
        "rejected": {k: v["why"] for k, v in verdicts.items() if not v["valid"]},
        "valid_ids": sorted(k for k, v in verdicts.items() if v["valid"]),
        "model_calls": 0,
    }


def validate(
    pack: Path,
    profile: str,
    pg: str,
    *,
    workers: int = 1,
    reader: str = "wb_reader",
    only: Optional[list[str]] = None,
) -> dict[str, Any]:
    os.environ.setdefault(READER_PASSWORD_ENV, "synthetic-reader")
    ids = [
        json.loads(line)["id"]
        for line in (pack / "tasks.jsonl").read_text().splitlines()
        if line.strip()
    ]
    if only:
        ids = [i for i in ids if i in only]
    chunks = [ids[i::workers] for i in range(workers)]
    jobs = [(i, profile, pg, str(pack), c, reader) for i, c in enumerate(chunks) if c]
    started = time.time()
    flow = profile == "flow"
    if flow:
        jobs = [(i, pg, str(pack), c, reader) for i, c in enumerate(chunks) if c]  # type: ignore[misc]
    fn = _flow_worker if flow else _worker
    if workers == 1:
        results = [fn(jobs[0])]  # type: ignore[arg-type]
    else:
        with mp.get_context("spawn").Pool(len(jobs)) as pool:
            results = pool.map(fn, jobs)  # type: ignore[arg-type]
    rows = [r for part in results for r in part]
    with (pack / "results.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    summary = summarize(pack, rows) if not only else {"episodes": len(rows)}
    summary["wall_s"] = round(time.time() - started, 1)
    (pack / "validity.json").write_text(
        json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pack", type=Path)
    ap.add_argument(
        "--profile",
        required=True,
        help="the pack's profile, or 'flow' for a multi-app pack",
    )
    ap.add_argument(
        "--pg", required=True, help="libpq conninfo of an admin account, without dbname"
    )
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--reader", default="wb_reader")
    ap.add_argument("--only", action="append")
    args = ap.parse_args(argv)
    s = validate(
        args.pack,
        args.profile,
        args.pg,
        workers=args.workers,
        reader=args.reader,
        only=args.only,
    )
    print(
        json.dumps(
            {k: v for k, v in s.items() if k not in ("rejected", "valid_ids")}, indent=1
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
