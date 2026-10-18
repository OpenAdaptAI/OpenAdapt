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
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

sys.path.insert(0, str(HERE.parents[1] / "packages" / "flow" / "benchmark"))

from openadapt_flow.jit import TaskSignature, WebApp, compile_agent  # noqa: E402
from scoreboard.tasks import openimis as app  # noqa: E402

TASK = TaskSignature(
    "openimis.claim_entry",
    app.INSTRUCTION,
    {"insurance_no": str, "claim_no": str, "explanation": str},
)
# The bundled strict write policy. The standard gate refuses these openIMIS
# forms today; ``admit`` records why (see README).
POLICY = "clinical-write"
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
        # openIMIS lookups take ~5 s under emulation; capture after they load.
        capture_settle_timeout_s=10.0,
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


def _engine() -> dict[str, str]:
    import openadapt_flow

    root = Path(openadapt_flow.__file__).resolve().parent
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        head = "unknown"
    return {"path": str(root), "git_head": head}


def _environment() -> dict[str, Any]:
    return {
        "engine": _engine(),
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
    # The demonstration's own claim number avoids glyphs OCR confuses (0/O,
    # 1/I): a misread demonstrated value escapes parameter hygiene and gets
    # compiled into an on-screen check (engine gap, see README).
    values = {**record_values(0, args.tag), "claim_no": f"{args.tag}XKM"}
    if app.claim_rows(values["claim_no"]):
        raise SystemExit(f"claim {values['claim_no']} already exists")
    app.ui_change(False)
    if args.agent == "claude":
        from openadapt_flow.jit import load_customer_model_key
        from scoreboard.agent import ClaudeComputerUseAgent

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
    (out / "agent_run.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n"
    )
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
    (out / "admission.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n"
    )
    print(json.dumps({k: summary[k] for k in ("canary_status", "state")}, indent=2))
    return 0 if canary.verified else 1


# -- compile (outside the JIT lifecycle) -----------------------------------------


def program_dir(run_id: str) -> Path:
    return HERE / "out" / "program" / run_id


def cmd_compile(args: argparse.Namespace) -> int:
    """Compile the agent's recorded run into a plain program bundle.

    The JIT draft can't run unattended until it is admitted, and the standard
    gate refuses these openIMIS forms today (``admit`` records why). For the
    demo the same recording is compiled with the standard compiler and the
    same confirmed parameters, and the program runs under the demo profile.
    It is not admitted for production.
    """

    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.compiler.param_confirm import (
        apply_decisions,
        decisions_from_accept_list,
        load_proposals,
    )
    from openadapt_flow.ir import Workflow
    from openadapt_flow.jit.lift import lift_params

    jit = make_jit(args.cache)
    candidate = jit.candidate()
    if candidate is None:
        raise SystemExit("no JIT draft; run `record` first")
    recording = Path(candidate["compiled_recording"])
    out = program_dir(args.run_id) / "bundle"
    if out.exists():
        shutil.rmtree(out)
    # The same two passes the JIT confirm step runs: lift the agent's typed
    # inputs into parameter proposals, then recompile with them confirmed.
    values = json.loads((RESULTS / args.run_id / "agent_run.json").read_text())[
        "values"
    ]
    scratch = program_dir(args.run_id) / "proposals"
    if scratch.exists():
        shutil.rmtree(scratch)
    compile_recording(recording, scratch, name=TASK.task_id, mine_effects=True)
    lift_params(scratch, values)
    decisions = decisions_from_accept_list(load_proposals(scratch), list(TASK.params))
    apply_decisions(
        recording,
        out,
        name=TASK.task_id,
        decisions=decisions,
        mine_effects=True,
        bind_effect_params=True,
    )
    workflow = Workflow.load(out)
    steps = [
        {"id": st.id, "intent": st.intent, "risk": st.risk} for st in workflow.steps
    ]
    effects = [
        {"step": st.id, "kind": e.kind.value, "probe": e.probe}
        for st in workflow.steps
        for e in (st.effects or [])
    ]
    summary = {
        "phase": "compile",
        "recording": str(recording),
        "bundle": str(out.relative_to(HERE)),
        "params": sorted(workflow.params),
        "steps": steps,
        "record_check": effects,
        "note": "compiled from the agent's recording outside the JIT admission "
        "lifecycle; runs under the demo profile; not admitted for production",
    }
    res = RESULTS / args.run_id
    res.mkdir(parents=True, exist_ok=True)
    (res / "program.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"steps": len(steps), "params": summary["params"]}, indent=2))
    for st in steps:
        print(f"  {st['id']} {st['intent']} ({st['risk']})")
    return 0


# -- batch -----------------------------------------------------------------------


@dataclass
class Row:
    record: int
    attempt: int
    claim_no: str
    insurance_no: str
    ui_changed: bool
    outcome: str
    success: bool
    model_calls: int
    wall_s: float
    setup_s: float
    program_s: float
    readback_rows: int
    readback_correct: bool
    verifier_verdicts: str
    halted_step: str
    halt_reason: str
    video: str
    run_dir: str
    started_at: float
    host_load_1m: float


def _verdicts(report: Any) -> str:
    """The system-of-record check verdicts, e.g. ``4/4 confirmed``."""

    verdicts = [
        str(e).split(":", 2)[1].split()[0].lower()
        for r in report.results
        for e in (r.effect_results or [])
        if str(e).count(":") >= 1 and len(str(e).split(":", 2)) > 1
    ]
    if not verdicts:
        return "not reached"
    confirmed = sum(1 for v in verdicts if v == "confirmed")
    return f"{confirmed}/{len(verdicts)} confirmed"


def run_one(
    bundle: Path, values: dict[str, str], run_dir: Path, video_dir: Optional[Path]
) -> tuple[Any, float, float]:
    """Open the app, sign in (unmeasured), run the program once."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    backend, close = PlaywrightBackend.launch(
        app.claim_form_url(),
        headless=True,
        record_video_dir=str(video_dir) if video_dir else None,
    )
    try:
        s0 = time.time()
        app.setup(backend.page)
        setup_s = time.time() - s0
        # Demo profile defaults. (require_settled=True measured worse here:
        # 5 of 8 halted vs 1 of 6; see README.)
        replayer = Replayer(backend, effect_verifier=app.claims_verifier())
        p0 = time.time()
        report = replayer.run(
            Workflow.load(bundle),
            params=values,
            bundle_dir=bundle,
            run_dir=run_dir,
        )
        program_s = time.time() - p0
    finally:
        close()
    return report, setup_s, program_s


def _run_record(
    i: int,
    attempt: int,
    values: dict[str, str],
    bundle: Path,
    runs_dir: Path,
    video_dir: Path,
    vdir: Optional[Path],
    changed: bool,
) -> Row:
    before = set(video_dir.glob("*.webm"))
    run_dir = runs_dir / f"record-{i:03d}-{attempt}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    t0 = time.time()
    report, setup_s, program_s = run_one(bundle, values, run_dir, vdir)
    wall = time.time() - t0
    rows = app.claim_rows(values["claim_no"])
    halt_step, halt_reason = "", ""
    failed = [r for r in report.results if not r.ok]
    if failed:
        halt_step, halt_reason = failed[0].step_id, failed[0].error or ""
    elif report.halt is not None:
        halt_step = str(getattr(report.halt, "state_id", "") or "")
        halt_reason = str(getattr(report.halt, "reason", "") or "")
    newest = _newest(video_dir, before) if vdir else None
    return Row(
        record=i,
        attempt=attempt,
        claim_no=values["claim_no"],
        insurance_no=values["insurance_no"],
        ui_changed=changed,
        outcome=str(
            getattr(report.execution_outcome, "value", report.execution_outcome)
        ),
        success=bool(report.success),
        model_calls=int(report.model_calls or 0),
        wall_s=round(wall, 3),
        setup_s=round(setup_s, 3),
        program_s=round(program_s, 3),
        readback_rows=len(rows),
        readback_correct=_row_correct(rows, values),
        verifier_verdicts=_verdicts(report),
        halted_step=halt_step,
        halt_reason=halt_reason[:400],
        video=str(Path(newest).relative_to(HERE)) if newest else "",
        run_dir=str(run_dir.relative_to(HERE)),
        started_at=round(t0, 3),
        host_load_1m=round(os.getloadavg()[0], 1),
    )


def cmd_batch(args: argparse.Namespace) -> int:
    out = RESULTS / args.run_id
    out.mkdir(parents=True, exist_ok=True)
    bundle = program_dir(args.program_run or args.run_id) / "bundle"
    if not bundle.is_dir():
        raise SystemExit("no program bundle; run `compile` first")
    video_dir = out / "video" / "batch"
    video_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = HERE / "out" / "runs" / args.run_id
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
    from openadapt_flow.backends.playwright_backend import shared_browser

    with shared_browser(headless=True):
        _batch_loop(
            args,
            out,
            bundle,
            video_dir,
            runs_dir,
            csv_path,
            fields,
            done,
            video_records,
        )
    app.ui_change(False)
    _write_batch_meta(args, out)
    return 0


def _batch_loop(
    args: argparse.Namespace,
    out: Path,
    bundle: Path,
    video_dir: Path,
    runs_dir: Path,
    csv_path: Path,
    fields: list[str],
    done: set,
    video_records: set,
) -> None:
    changed = None
    for i in range(args.start, args.n + 1):
        want_change = bool(args.change_at) and i >= args.change_at
        if want_change != changed:
            app.ui_change(want_change)
            changed = want_change
            if want_change:
                print(f"record {i}: UI change on ({app.UI_CHANGE})")
        if i in done:
            continue
        values = record_values(i, args.tag)
        vdir = video_dir if i in video_records else None
        for attempt in range(1, args.max_attempts + 1):
            row = _run_record(
                i, attempt, values, bundle, runs_dir, video_dir, vdir, bool(changed)
            )
            with csv_path.open("a", newline="") as f:
                csv.DictWriter(f, fieldnames=fields).writerow(row.__dict__)
            print(
                f"record {i:3d}.{attempt} {row.outcome:22s} program "
                f"{row.program_s:6.1f}s {row.verifier_verdicts} {row.halted_step}",
                flush=True,
            )
            # Retry only when the halt left nothing behind: the read-back
            # finds no claim with this number. Anything else goes to a person.
            if row.success or row.readback_rows != 0:
                break
        if args.stop_after_halt and not row.success:
            print("stopping after the first record that did not succeed")
            break
        if args.pause_after_change_halt and changed and not row.success:
            print(
                f"record {i}: stopped on every attempt after the UI change; "
                "pausing the batch for a person"
            )
            (out / "paused.json").write_text(
                json.dumps(
                    {
                        "paused_at_record": i,
                        "reason": row.halt_reason,
                        "step": row.halted_step,
                        "remaining": list(range(i + 1, args.n + 1)),
                    },
                    indent=2,
                )
                + "\n"
            )
            break


def _write_batch_meta(args: argparse.Namespace, out: Path) -> None:
    (out / "batch_meta.json").write_text(
        json.dumps(
            {
                "n": args.n,
                "change_at": args.change_at,
                "ui_change": app.UI_CHANGE,
                "tag": args.tag,
                "program_run": args.program_run or args.run_id,
                "max_attempts": args.max_attempts,
                "profile": "demo (plain Replayer, SQL record check attached)",
                "environment": _environment(),
            },
            indent=2,
        )
        + "\n"
    )


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
    sub.add_parser("compile").set_defaults(func=cmd_compile)
    p = sub.add_parser("batch")
    p.add_argument("--n", type=int, default=100)
    p.add_argument(
        "--program-run",
        default=None,
        help="run id whose compiled program to use (default: --run-id)",
    )
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--change-at", type=int, default=50)
    p.add_argument("--video-records", default="1,2,3,49,50,51,100")
    p.add_argument("--stop-after-halt", action="store_true")
    p.add_argument("--pause-after-change-halt", action="store_true")
    p.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="retry a claim whose run stopped with nothing written",
    )
    p.set_defaults(func=cmd_batch)
    args = parser.parse_args()
    if len(args.tag) + 3 > 8:
        parser.error("--tag must leave room for a 3-digit record number (8 chars)")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
