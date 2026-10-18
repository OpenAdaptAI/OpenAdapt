"""Bring runs made under earlier protocols into the scoreboard's row format.

    python -m scoreboard.importers openimis-demo --run-id launch-4
    python -m scoreboard.importers openemr

* ``openimis-demo``: the launch demo's openIMIS runs
  (``demo/cua_to_program/results/<run>``): the agent's first run under the
  JIT wrapper (arm 2) and the compiled program's batch (arm 3).
* ``openemr``: the OpenEMR referral timing study
  (``benchmark/openemr_referral_timing/runs.csv``, its own PROTOCOL.md and a
  start gate at load 12): exactly its published set (engine 0c642bc0, warm
  runs, blocks 2 to 10): the Claude Sonnet agent (arm 1) and the
  DOM-identity program (arm 3). The older bundle, the cold starts and the
  later blocks stay in that study.

The originals are left where they are. Each import writes
``raw/<task>/<arm>/<run id>/items.csv`` and a ``meta.json`` that names its
source file and its SHA-256.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from scoreboard import schema  # noqa: E402

REPO = HERE.parents[3]
DEMO = REPO / "demo" / "cua_to_program" / "results"
OPENEMR = HERE.parent / "openemr_referral_timing" / "runs.csv"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fresh(path: Path) -> Path:
    items = path / "items.csv"
    if items.is_file():
        items.unlink()
    return items


def openimis_demo(run_id: str, program_only: bool = False) -> None:
    src = DEMO / run_id
    if not program_only:
        _openimis_first_run(src, run_id)
    _openimis_program(src, run_id)


def _openimis_first_run(src: Path, run_id: str) -> None:
    ledger = json.loads((src / "agent_ledger.json").read_text())
    agent = json.loads((src / "agent_run.json").read_text())
    admission = json.loads((src / "admission.json").read_text())

    # Arm 2: the first run under the JIT wrapper.
    out = schema.run_dir("openimis", "first_run", run_id)
    items = _fresh(out)
    calls = ledger["calls"]
    item_s = ledger["finished_at"] - ledger["started_at"]
    readback = "correct" if agent["record_correct"] else "wrong"
    schema.append(
        items,
        schema.Row(
            task="openimis",
            arm="first_run",
            run_id=run_id,
            item=0,
            attempt=1,
            fault="none",
            reported_success=True,
            readback=readback,
            outcome=schema.classify(True, readback, "none"),
            model_calls=len(calls),
            input_tokens=sum(c["input_tokens"] for c in calls),
            output_tokens=sum(c["output_tokens"] for c in calls),
            cache_read_tokens=sum(c["cache_read_input_tokens"] for c in calls),
            cache_write_tokens=sum(c["cache_creation_input_tokens"] for c in calls),
            cost_usd=round(sum(c["cost_usd"] for c in calls), 6),
            item_s=round(item_s, 3),
            wall_s=float(agent["wall_s_including_setup_and_compile"]),
            approvals_asked=0,
            host_load_1m=-1.0,
            timed=False,
            detail=(
                f"model {ledger['model']}; compiled; not admitted: standard gate "
                f"{admission['canary_status']} ({admission['canary_detail'].splitlines()[1][:120]})"
            ),
        ),
    )
    schema.write_meta(
        out,
        task="openimis",
        arm="first_run",
        imported_from=str(src.relative_to(REPO)),
        source_sha256={
            p.name: _sha(p)
            for p in (
                src / "agent_ledger.json",
                src / "agent_run.json",
                src / "admission.json",
            )
        },
        note="host load not sampled for this run; untimed",
    )


def _openimis_program(src: Path, run_id: str) -> None:
    out = schema.run_dir("openimis", "program", run_id)
    items = _fresh(out)
    batch = src / "batch.csv"
    for r in csv.DictReader(batch.open()):
        load = float(r.get("host_load_1m") or -1)
        success = r["success"] == "True"
        rows = int(r["readback_rows"])
        readback = (
            "correct"
            if r["readback_correct"] == "True"
            else "missing"
            if rows == 0
            else "duplicate"
            if rows > 1
            else "wrong"
        )
        fault = "ui_change" if r["ui_changed"] == "True" else "none"
        schema.append(
            items,
            schema.Row(
                task="openimis",
                arm="program",
                run_id=run_id,
                item=int(r["record"]),
                attempt=int(r["attempt"]),
                fault=fault,
                reported_success=success,
                readback=readback,
                outcome=schema.classify(success, readback, fault),
                model_calls=int(r["model_calls"]),
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_usd=0.0,
                item_s=float(r["program_s"]),
                wall_s=float(r["wall_s"]),
                approvals_asked=0,
                host_load_1m=load,
                timed=0 <= load <= schema.TIMED_LOAD_MAX,
                detail=(r["halt_reason"] or r["verifier_verdicts"])[:300],
            ),
        )
    paused = src / "paused.json"
    if paused.is_file():
        p = json.loads(paused.read_text())
        for i in p["remaining"]:
            schema.append(
                items,
                schema.Row(
                    task="openimis",
                    arm="program",
                    run_id=run_id,
                    item=i,
                    attempt=0,
                    fault="ui_change",
                    reported_success=False,
                    readback="not_checked",
                    outcome="paused",
                    model_calls=0,
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=0.0,
                    item_s=0.0,
                    wall_s=0.0,
                    approvals_asked=0,
                    host_load_1m=-1.0,
                    timed=False,
                    detail=f"batch paused for a person at item {p['paused_at_record']}",
                ),
            )
    meta = (
        json.loads((src / "batch_meta.json").read_text())
        if (src / "batch_meta.json").is_file()
        else {}
    )
    schema.write_meta(
        out,
        task="openimis",
        arm="program",
        imported_from=str(src.relative_to(REPO)),
        source_sha256={"batch.csv": _sha(batch)},
        batch_meta=meta,
        profile="demo (plain Replayer, SQL record check attached); not admitted",
    )


def openemr() -> None:
    arms = {"agent-sonnet": "agent", "replay-dom": "program"}
    outs = {}
    for r in csv.DictReader(OPENEMR.open()):
        arm = arms.get(r["arm"])
        # Exactly that study's published set (compute_figures.py SETS
        # "published"): engine 0c642bc0, warm, blocks 2 to 10, valid runs.
        if (
            arm is None
            or r["engine_tag"] != "main-0c642bc0"
            or r["mode"] != "warm"
            or r["valid"] != "True"
            or not 2 <= int(r["block"]) <= 10
        ):
            continue
        run_id = f"study-{r['engine_tag']}"
        out = schema.run_dir("openemr_referral", arm, run_id)
        if out not in outs:
            outs[out] = _fresh(out)
        mismatch = r["mismatch"] == "True"
        correct = r["correct"] == "True"
        new = int(r["new_charts"] or 0)
        fault = "duplicate_check" if mismatch else "none"
        if correct and not mismatch:
            readback, reported = "correct", True
        elif correct and mismatch:
            readback, reported = "missing", False  # stopped; no chart created
        else:
            readback = "missing" if new == 0 else "wrong"
            reported = new > 0
        load = float(r["load1_max"] or -1)
        item_no = int(r["block"]) * 100 + int(r["pos"])
        schema.append(
            outs[out],
            schema.Row(
                task="openemr_referral",
                arm=arm,
                run_id=run_id,
                item=item_no,
                attempt=1,
                fault=fault,
                reported_success=reported,
                readback=readback,
                outcome=(
                    "correct"
                    if correct and not mismatch
                    else "safe_stop"
                    if correct and mismatch
                    else schema.classify(reported, readback, fault)
                ),
                # That study recorded calls and cost but not tokens: -1 = unknown.
                model_calls=int(float(r["model_calls"] or 0)),
                input_tokens=-1 if arm == "agent" else 0,
                output_tokens=-1 if arm == "agent" else 0,
                cache_read_tokens=-1 if arm == "agent" else 0,
                cache_write_tokens=-1 if arm == "agent" else 0,
                cost_usd=float(r["cost_usd"] or 0),
                item_s=float(r["task"] or 0),
                wall_s=float(r["wall"] or 0),
                approvals_asked=0,
                host_load_1m=load,
                # That study gated each run's start on 1-minute load < 12.
                timed=r["gate_gave_up"] != "True",
                detail=f"{r['ref']} block {r['block']} {r['outcome']} {r['error']}"[
                    :300
                ],
            ),
        )
    for out in outs:
        schema.write_meta(
            out,
            task="openemr_referral",
            arm=out.parent.name,
            imported_from=str(OPENEMR.relative_to(REPO)),
            source_sha256=_sha(OPENEMR),
            note=(
                "the study's published set: engine 0c642bc0, warm, blocks 2-10, "
                "valid runs; timed per its start gate (1-minute load < 12)"
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("openimis-demo")
    p.add_argument("--run-id", required=True)
    p.add_argument("--program-only", action="store_true")
    sub.add_parser("openemr")
    args = parser.parse_args()
    if args.cmd == "openimis-demo":
        openimis_demo(args.run_id, args.program_only)
    else:
        openemr()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
