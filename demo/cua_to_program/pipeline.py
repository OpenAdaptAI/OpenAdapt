"""CUA to program on openIMIS claim entry: record, admit, run a batch.

    python pipeline.py bootstrap
    python pipeline.py record --agent claude   # one real Claude computer-use run
    python pipeline.py admit                   # params, review, canary, admit
    python pipeline.py batch --n 100 --change-at 50

Every number this writes is measured: wall clocks around each phase, token
usage from the API, and a read of the claim table after every record. The
results go to ``results/<run id>/`` as CSV and JSON.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import openimis_app as app  # noqa: E402

from openadapt_flow.jit import TaskSignature, WebApp, compile_agent  # noqa: E402

TASK = TaskSignature(
    "openimis.claim_entry",
    (
        "Enter one health-facility claim on the open Health Facility Claim "
        "form and save it. The keyboard focus is already in the Insurance No. "
        "field. Enter insurance number {insurance_no} and wait until the "
        "insuree's name and an active policy appear. Then set Claim No. to "
        "{claim_no}; set the main diagnosis to 'Scarlet fever, uncomplicated' "
        "(type Scarlet in the main diagnosis search box and pick it); set "
        "Explanation to: {explanation}; and in the Services table add the "
        "service 'General Consultation' (type General in its search box and "
        "pick it). Leave every other field as it is. Save the claim with the "
        "round Save button at the bottom right of the page (a floppy-disk "
        "icon), then stop."
    ),
    {"insurance_no": str, "claim_no": str, "explanation": str},
)
# clinical-write minus the icon-only-Save rules; see the file.
POLICY = str(HERE / "claims-demo-policy.yaml")
DEMO_ADMITTER = "ci:launch-demo"
REVIEWER = "demo-reviewer@example.invalid"

DEFAULT_CACHE = HERE / "out" / "jit-cache"
RESULTS = HERE / "results"

EXPLANATIONS = [
    "Fever and sore throat, seen same day",
    "Follow-up after antibiotic course",
    "Rash on trunk, swab taken",
    "Child with fever, parent present",
    "Second visit, symptoms improving",
]


def record_values(i: int, tag: str) -> dict[str, str]:
    """The synthetic inputs for record ``i`` (1-based) of a batch."""

    chf = app.POLICYHOLDERS[(i - 1) % len(app.POLICYHOLDERS)][0]
    return {
        "insurance_no": chf,
        "claim_no": f"{tag}{i:03d}",  # openIMIS accepts at most 8 characters
        "explanation": f"{EXPLANATIONS[(i - 1) % len(EXPLANATIONS)]} (#{i})",
    }


def make_jit(
    cache: Path,
    *,
    agent: Any = None,
    video_dir: Optional[Path] = None,
    model_key: Any = None,
    setup: Any = None,
) -> Any:
    webapp = WebApp(
        url=app.claim_form_url(),
        record_reader=app.read_claims,
        setup=setup or app.setup,
        headless=True,
        new_record_form=True,
        record_video_dir=str(video_dir) if video_dir else None,
    )
    return compile_agent(
        agent,
        task=TASK,
        app=webapp,
        verifier=app.claims_verifier(),
        policy=POLICY,
        cache_dir=cache,
        key_dir=cache / "admission-keys",
        model_key=model_key,
        dialect=getattr(agent, "dialect", "generic"),
    )


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(HERE), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _environment() -> dict[str, Any]:
    return {
        "machine": platform.machine(),
        "system": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "git_head": _git_head(),
        "app": "openIMIS 25.10 reference stack (synthetic demo data), linux/amd64 "
        "images under Rosetta in a colima VM",
    }


def _newest(directory: Path, before: set[Path]) -> Optional[str]:
    if not directory.is_dir():
        return None
    new = [p for p in directory.glob("*.webm") if p not in before]
    return str(max(new, key=lambda p: p.stat().st_mtime)) if new else None


# -- bootstrap -----------------------------------------------------------------


def cmd_bootstrap(args: argparse.Namespace) -> int:
    print(json.dumps(app.bootstrap(), indent=2))
    app.ui_change(False)
    print("rows readable by the reader role:", len(app.read_claims()))
    return 0


# -- record --------------------------------------------------------------------


def cmd_record(args: argparse.Namespace) -> int:
    out = RESULTS / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    video_dir = out / "video" / "agent"
    video_dir.mkdir(parents=True, exist_ok=True)
    values = record_values(0, args.tag)
    if app.claim_rows(values["claim_no"]):
        raise SystemExit(f"claim {values['claim_no']} already exists")
    app.ui_change(False)
    if args.agent == "claude":
        from claude_agent import ClaudeComputerUseAgent

        from openadapt_flow.jit import load_customer_model_key

        agent: Any = ClaudeComputerUseAgent(
            ledger_path=out / "agent_ledger.json",
            frames_dir=out / "agent_frames",
            max_cost_usd=args.max_cost,
        )
        model_key = load_customer_model_key("anthropic")
    else:
        from scripted_agent import ScriptedClaimAgent

        agent = ScriptedClaimAgent()
        model_key = None
    jit = make_jit(args.cache, agent=agent, video_dir=video_dir, model_key=model_key)
    before = set(video_dir.glob("*.webm"))
    t0 = time.time()
    result = jit.run(**values)
    t1 = time.time()
    rows = app.claim_rows(values["claim_no"])
    summary: dict[str, Any] = {
        "phase": "agent_run",
        "agent": args.agent,
        "status": result.status,
        "detail": result.detail,
        "values": values,
        "wall_s_including_setup_and_compile": round(t1 - t0, 3),
        "claim_rows_after": rows,
        "record_correct": _row_correct(rows, values),
        "video": _newest(video_dir, before),
        "environment": _environment(),
        "candidate_effects": (result.candidate or {}).get("effects"),
        "candidate_params": (result.candidate or {}).get("params"),
        "agent_actions": (result.candidate or {}).get("agent_actions"),
    }
    if args.agent == "claude":
        summary["model"] = agent.model
        summary["model_calls"] = len(agent.calls)
        summary["cost_usd"] = round(agent.cost_usd, 6)
        if agent.started_at and agent.finished_at:
            summary["agent_wall_s"] = round(agent.finished_at - agent.started_at, 3)
        summary["refused_actions"] = agent.refused_actions
    (out / "agent_run.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(json.dumps({k: summary[k] for k in ("status", "record_correct")}, indent=2))
    print("details:", out / "agent_run.json")
    return 0 if result.status == "needs_human_admit" else 1


def _row_correct(rows: list[dict[str, Any]], values: dict[str, str]) -> bool:
    if len(rows) != 1:
        return False
    row = rows[0]
    return (
        row["insurance_no"] == values["insurance_no"]
        and row["explanation"] == values["explanation"]
        and int(row["claim_status"]) == 2
        and row["hf_code"] == app.HEALTH_FACILITY_CODE
        and row["diagnosis"] == "A389"
    )


# -- admit ---------------------------------------------------------------------


def _demo_admitter(key_dir: Path) -> Any:
    """A demo CI admitter key registered in the cache's own key directory.

    Stands in for a key a person provisioned (JIT_ADMISSION.md, CI mode).
    """

    from openadapt_flow.jit import authority

    os.environ.setdefault(authority.KEY_PASSPHRASE_ENV, "launch-demo-only")
    pem = key_dir / "demo-ci.pem"
    if not pem.is_file():
        key_dir.mkdir(parents=True, exist_ok=True)
        pub = authority.generate_encrypted_key(pem)
        authority.register_principal(
            key_dir, role="admitter_key", principal_id=DEMO_ADMITTER, public_key_hex=pub
        )
    return authority.principal_from_key(authority.load_encrypted_key(pem), key_dir)


def cmd_admit(args: argparse.Namespace) -> int:
    out = RESULTS / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    video_dir = out / "video" / "canary"
    video_dir.mkdir(parents=True, exist_ok=True)
    app.ui_change(False)
    jit = make_jit(args.cache, video_dir=video_dir)
    t0 = time.time()
    confirmed = jit.confirm_params(
        principal_id=REVIEWER, accept_params=list(TASK.params), confirm="ok"
    )
    jit.review(principal_id=REVIEWER, reviewed_steps=confirmed["steps"], confirm="ok")
    t_review = time.time()
    canary_values = record_values(999, args.tag)
    before = set(video_dir.glob("*.webm"))
    canary = jit.canary(principal_id=REVIEWER, **canary_values)
    t_canary = time.time()
    summary: dict[str, Any] = {
        "phase": "admission",
        "reviewer": REVIEWER,
        "steps_reviewed": len(confirmed["steps"]),
        "canary_values": canary_values,
        "canary_status": canary.status,
        "canary_model_calls": canary.model_calls,
        "canary_detail": canary.detail,
        "canary_halt_reason": canary.halt_reason,
        "canary_wall_s": round(t_canary - t_review, 3),
        "canary_rows": app.claim_rows(canary_values["claim_no"]),
        "canary_video": _newest(video_dir, before),
    }
    if canary.verified:
        manifest = jit.admit_with_key(_demo_admitter(args.cache / "admission-keys"))
        summary["admitted_by"] = DEMO_ADMITTER
        summary["admission"] = manifest.get("manifest", manifest)
    summary["admission_wall_s"] = round(time.time() - t0, 3)
    summary["state"] = str(jit.state())
    (out / "admission.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(json.dumps({k: summary[k] for k in ("canary_status", "state")}, indent=2))
    return 0 if canary.verified else 1


# -- batch ---------------------------------------------------------------------


@dataclass
class Row:
    record: int
    claim_no: str
    insurance_no: str
    ui_changed: bool
    status: str
    model_calls: Optional[int]
    wall_s: float
    setup_s: float
    program_s: Optional[float]
    readback_rows: int
    readback_correct: bool
    halt_reason: str
    repaired: bool
    video: str
    run_dir: str
    started_at: float


def cmd_batch(args: argparse.Namespace) -> int:
    out = RESULTS / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    video_dir = out / "video" / "batch"
    video_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out / "batch.csv"
    fields = list(Row.__dataclass_fields__)
    done = set()
    if csv_path.is_file():
        with csv_path.open() as f:
            done = {int(r["record"]) for r in csv.DictReader(f)}
    else:
        with csv_path.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()
    video_records = {int(x) for x in args.video_records.split(",") if x}
    app.ui_change(False)
    changed = False
    setup_clock: dict[str, float] = {}

    def timed_setup(page: Any) -> None:
        s0 = time.time()
        app.setup(page)
        setup_clock["s"] = time.time() - s0

    for i in range(1, args.n + 1):
        if args.change_at and i >= args.change_at and not changed:
            app.ui_change(True)
            changed = True
            print(f"record {i}: UI change applied ({app.UI_CHANGE})")
        if i in done:
            continue
        values = record_values(i, args.tag)
        vdir = video_dir if i in video_records else None
        jit = make_jit(args.cache, video_dir=vdir, setup=timed_setup)
        before = set(video_dir.glob("*.webm"))
        setup_clock.clear()
        t0 = time.time()
        result = jit.run(**values)
        wall = time.time() - t0
        rows = app.claim_rows(values["claim_no"])
        report = result.report
        total_ms = None
        if report is not None:
            total_ms = getattr(report, "total_ms", None)
            if total_ms is None and isinstance(report, dict):
                total_ms = report.get("total_ms")
        row = Row(
            record=i,
            claim_no=values["claim_no"],
            insurance_no=values["insurance_no"],
            ui_changed=changed,
            status=result.status,
            model_calls=result.model_calls,
            wall_s=round(wall, 3),
            setup_s=round(setup_clock.get("s", 0.0), 3),
            program_s=round(total_ms / 1000, 3) if total_ms else None,
            readback_rows=len(rows),
            readback_correct=_row_correct(rows, values),
            halt_reason=(result.halt_reason or result.detail or "")[:300],
            repaired=False,
            video=_newest(video_dir, before) or "",
            run_dir=str(result.run_dir or ""),
            started_at=round(t0, 3),
        )
        with csv_path.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writerow(row.__dict__)
        print(
            f"record {i:3d} {row.status:10s} wall {row.wall_s:6.1f}s "
            f"program {row.program_s}s read back {row.readback_correct}"
        )
        if args.stop_on_halt and not result.verified:
            print("stopping on the first record that did not verify")
            break
    (out / "batch_meta.json").write_text(
        json.dumps(
            {
                "n": args.n,
                "change_at": args.change_at,
                "ui_change": app.UI_CHANGE,
                "tag": args.tag,
                "environment": _environment(),
            },
            indent=2,
        )
        + "\n"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--run-id", default=time.strftime("%Y%m%d"))
    parser.add_argument(
        "--tag", default="CL", help="claim number prefix, 2-4 letters (unique per run)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap").set_defaults(func=cmd_bootstrap)
    p = sub.add_parser("record")
    p.add_argument("--agent", choices=["claude", "scripted"], default="claude")
    p.add_argument("--max-cost", type=float, default=4.0)
    p.set_defaults(func=cmd_record)
    sub.add_parser("admit").set_defaults(func=cmd_admit)
    p = sub.add_parser("batch")
    p.add_argument("--n", type=int, default=100)
    p.add_argument("--change-at", type=int, default=50)
    p.add_argument("--video-records", default="1,2,3,49,50,51,52,100")
    p.add_argument("--stop-on-halt", action="store_true")
    p.set_defaults(func=cmd_batch)
    args = parser.parse_args()
    if len(args.tag) + 3 > 8:
        parser.error("--tag must leave room for a 3-digit record number (8 chars)")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
