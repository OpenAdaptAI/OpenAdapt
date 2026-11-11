"""Run the grader-soundness study on one benchmark target, or summarize.

    # wrong solutions (the kit's harmful families) and valid alternatives
    python run.py run --target openadapt_flow.verifier_audit.targets.agentdojo:make_target \\
        --target-arg suite=banking --out results/agentdojo/banking

    python run.py summarize --results results --out results.json

The benchmark itself is imported from the caller's own install (AgentDojo in
process, AppWorld through the kit's worker). No benchmark file lives here.
See PROTOCOL.md for what is counted.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from alternatives import (  # noqa: E402
    ALTERNATIVES,
    AltContext,
    audit_alternative,
    read_only_pool,
)
from openadapt_flow.verifier_audit.families import (  # noqa: E402
    DEFAULT_FAMILIES,
    Polarity,
)
from openadapt_flow.verifier_audit.report import wilson_interval  # noqa: E402
from openadapt_flow.verifier_audit.runner import (  # noqa: E402
    AuditConfig,
    _redact,
    run_audit,
)

HARMFUL = tuple(f for f in DEFAULT_FAMILIES if f.polarity is Polarity.HARMFUL)
REQUESTED_NAME = {
    "wrong_record": "wrong record",
    "wrong_value": "wrong field value",
    "unauthorized_side_effect": "collateral write",
    "partial_write": "missing write (some)",
    "optimistic_success": "missing write (all)",
    "duplicate": "duplicate write",
    "late_write": "write after the success claim",
    "repeated_reads": "repeated reads",
    "foreign_reads": "unrelated reads",
    "reordered_writes": "writes reordered",
    "refused_attempt": "refused attempt, then the right write",
    "self_corrected_write": "wrong write, then corrected",
}
RETRY = ("HARNESS_ERROR", "SCORER_ERROR")


def _factory(spec: str):
    module, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module), attr)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def cmd_run(args: argparse.Namespace) -> int:
    kwargs = dict(kv.split("=", 1) for kv in args.target_arg)
    target = _factory(args.target)(**kwargs)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    tasks = args.tasks.split(",") if args.tasks else None
    started = time.time()

    # 1. Wrong solutions: the kit, unchanged, harmful families only.
    report = run_audit(
        target,
        AuditConfig(
            families=HARMFUL,
            tasks=tasks,
            max_candidates=6,
            redact=args.redact,
            out_dir=out,
            log=_log,
        ),
    )
    excluded = {row["task"] for row in report["excluded"]}
    all_tasks = list(tasks or target.tasks())
    kept = [t for t in all_tasks if t not in excluded]

    # 2. Valid alternatives, same tasks, census on the end state.
    ledger = out / "alternatives.jsonl"
    done: dict[tuple[str, str], dict[str, Any]] = {}
    if ledger.is_file():
        for line in ledger.read_text().splitlines():
            if line.strip():
                cell = json.loads(line)
                if cell["status"] not in RETRY:
                    done[(cell["task"], cell["family"])] = cell
    families = [f for f in ALTERNATIVES if not args.only or f in args.only.split(",")]
    foreign = (
        read_only_pool(target, all_tasks) if args.foreign_reads else {t: [] for t in kept}
    )
    for n, task in enumerate(kept, 1):
        pending = [f for f in families if (task, f) not in done]
        if not pending:
            continue
        t0 = time.monotonic()
        ref = target.reference(task)
        control = target.run(task, ref, score=False)
        ctx = AltContext(task=task, reference=ref, foreign_reads=foreign.get(task, []))
        for family in pending:
            try:
                cell = audit_alternative(target, task, family, ctx, control)
            except Exception as exc:  # noqa: BLE001 - one cell must not end the run
                cell = {
                    "task": task,
                    "family": family,
                    "polarity": "legitimate",
                    "status": "HARNESS_ERROR",
                    "error": repr(exc)[:500],
                }
            if args.redact:
                cell = _redact(cell)
            with ledger.open("a") as fh:
                fh.write(json.dumps(cell, sort_keys=True, default=str) + "\n")
        _log(f"[alt {n}/{len(kept)}] {task}: {time.monotonic() - t0:.1f}s")
    meta_path = out / "run_meta.json"
    prior = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    # Invocations on one ledger may cover different task subsets; keep the union.
    all_tasks = sorted(set(prior.get("tasks", [])) | set(all_tasks))
    excluded |= set(prior.get("excluded", []))
    meta_path.write_text(
        json.dumps(
            {
                "target": target.name,
                "tasks": all_tasks,
                "excluded": sorted(excluded),
                "redact": args.redact,
                "wall_seconds_this_invocation": round(time.time() - started, 1),
            },
            indent=2,
        )
        + "\n"
    )
    close = getattr(target, "close", None)
    if close:
        close()
    return 0


# ---------------------------------------------------------------- summary


def _rate(hits: int, n: int) -> dict[str, Any]:
    lo, hi = wilson_interval(hits, n)
    return {
        "hits": hits,
        "n": n,
        "rate": round(hits / n, 4) if n else None,
        "wilson95": [round(lo, 4), round(hi, 4)],
    }


def _load_cells(run_dir: Path) -> tuple[list[dict], list[dict], list[dict]]:
    """Latest cell per (task, family) from both ledgers, plus exclusions."""
    harmful: dict[tuple[str, str], dict] = {}
    excluded: dict[str, dict] = {}
    for line in (run_dir / "cells.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        cell = json.loads(line)
        if cell["family"] == "__control__":
            excluded[cell["task"]] = cell
        else:
            harmful[(cell["task"], cell["family"])] = cell
    alts: dict[tuple[str, str], dict] = {}
    alt_path = run_dir / "alternatives.jsonl"
    if alt_path.is_file():
        for line in alt_path.read_text().splitlines():
            if line.strip():
                cell = json.loads(line)
                alts[(cell["task"], cell["family"])] = cell
    return list(harmful.values()), list(alts.values()), list(excluded.values())


def _value_class(cell: dict) -> str:
    named = cell.get("original_value_in_instruction")
    return {True: "named", False: "unnamed"}.get(named, "unknown")


def summarize_benchmark(run_dirs: list[Path]) -> dict[str, Any]:
    harmful: list[dict] = []
    alts: list[dict] = []
    excluded: list[dict] = []
    tasks: set[str] = set()
    for d in run_dirs:
        h, a, e = _load_cells(d)
        prefix = d.name + ":" if len(run_dirs) > 1 and d.parent.name == "agentdojo" else ""
        for group in (h, a, e):
            for cell in group:
                cell["task"] = prefix + cell["task"]
        harmful += h
        alts += a
        excluded += e
        meta = json.loads((d / "run_meta.json").read_text())
        tasks |= {prefix + t for t in meta["tasks"]}

    rows = []
    pooled_fa = pooled_n = 0
    fa_tasks: set[str] = set()
    scored_tasks: set[str] = set()
    for fam in [f.id for f in HARMFUL]:
        group = [c for c in harmful if c["family"] == fam]
        statuses: dict[str, int] = {}
        for c in group:
            statuses[c["status"]] = statuses.get(c["status"], 0) + 1
        counted = [c for c in group if c["status"] in ("FALSE_ACCEPT", "CAUGHT")]
        extra_row = None
        if fam == "wrong_value":
            primary = [c for c in counted if _value_class(c) == "named"]
            unspecified = [c for c in counted if _value_class(c) != "named"]
            passed = sum(c["status"] == "FALSE_ACCEPT" for c in unspecified)
            extra_row = {
                "family": "wrong_value (unspecified value)",
                "requested_name": "wrong field value, value not named in the task",
                "class": "unspecified",
                "metric": "pass_rate",
                **_rate(passed, len(unspecified)),
                "statuses": {"PASS": passed, "FAIL": len(unspecified) - passed},
                "note": "not counted as false accept or caught (PROTOCOL.md)",
            }
            counted = primary
        hits = sum(c["status"] == "FALSE_ACCEPT" for c in counted)
        pooled_fa += hits
        pooled_n += len(counted)
        for c in counted:
            scored_tasks.add(c["task"])
            if c["status"] == "FALSE_ACCEPT":
                fa_tasks.add(c["task"])
        rows.append(
            {
                "family": fam,
                "requested_name": REQUESTED_NAME[fam],
                "class": "wrong",
                "metric": "false_accept_rate",
                **_rate(hits, len(counted)),
                "statuses": dict(sorted(statuses.items())),
            }
        )
        if extra_row:
            rows.append(extra_row)
    pooled_fr = pooled_alt_n = 0
    for fam in ALTERNATIVES:
        group = [c for c in alts if c["family"] == fam]
        statuses = {}
        for c in group:
            statuses[c["status"]] = statuses.get(c["status"], 0) + 1
        counted = [c for c in group if c["status"] in ("FALSE_REJECT", "ACCEPTED")]
        hits = sum(c["status"] == "FALSE_REJECT" for c in counted)
        pooled_fr += hits
        pooled_alt_n += len(counted)
        rows.append(
            {
                "family": fam,
                "requested_name": REQUESTED_NAME[fam],
                "class": "valid_alternative",
                "metric": "false_reject_rate",
                **_rate(hits, len(counted)),
                "statuses": dict(sorted(statuses.items())),
            }
        )
    exclusion_reasons: dict[str, int] = {}
    for e in excluded:
        r = str(e.get("reason"))
        exclusion_reasons[r] = exclusion_reasons.get(r, 0) + 1
    return {
        "tasks_in_sample": len(tasks),
        "tasks_excluded": len(excluded),
        "exclusion_reasons": exclusion_reasons,
        "tasks_analyzed": len(tasks) - len(excluded),
        "headline": {
            "false_accept_pooled": _rate(pooled_fa, pooled_n),
            "false_reject_pooled": _rate(pooled_fr, pooled_alt_n),
            "tasks_with_any_false_accept": _rate(len(fa_tasks), len(scored_tasks)),
            "note": "pooled over cells; cells from one task are not independent, "
            "so the pooled intervals are descriptive",
        },
        "families": rows,
        "counterexamples": [
            {
                k: c.get(k)
                for k in (
                    "task",
                    "family",
                    "status",
                    "edit",
                    "effect_diff",
                    "verdict",
                    "original_value_in_instruction",
                    "scorer_detail",
                )
                if k in c
            }
            for c in harmful + alts
            if c["status"] in ("FALSE_ACCEPT", "FALSE_REJECT")
        ],
    }


def cmd_summarize(args: argparse.Namespace) -> int:
    root: Path = args.results
    out: dict[str, Any] = {"format": "openadapt.grader-soundness/v1", "benchmarks": {}}
    for bench in sorted(p for p in root.iterdir() if p.is_dir()):
        runs = sorted(p for p in bench.iterdir() if (p / "cells.jsonl").is_file())
        if runs:
            out["benchmarks"][bench.name] = summarize_benchmark(runs)
    args.out.write_text(json.dumps(out, indent=2, sort_keys=True, default=str) + "\n")
    for name, b in out["benchmarks"].items():
        h = b["headline"]
        print(
            name,
            "FA",
            h["false_accept_pooled"],
            "FR",
            h["false_reject_pooled"],
            "tasks",
            b["tasks_analyzed"],
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(allow_abbrev=False)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", allow_abbrev=False)
    r.add_argument("--target", required=True)
    r.add_argument("--target-arg", action="append", default=[])
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--tasks")
    r.add_argument("--only", help="comma-separated alternative families")
    r.add_argument("--redact", action="store_true")
    r.add_argument(
        "--foreign-reads",
        action="store_true",
        help="build foreign_reads from other tasks' reads (AgentDojo)",
    )
    s = sub.add_parser("summarize", allow_abbrev=False)
    s.add_argument("--results", type=Path, default=HERE / "results")
    s.add_argument("--out", type=Path, default=HERE / "results.json")
    args = p.parse_args(argv)
    return cmd_run(args) if args.cmd == "run" else cmd_summarize(args)


if __name__ == "__main__":
    raise SystemExit(main())
