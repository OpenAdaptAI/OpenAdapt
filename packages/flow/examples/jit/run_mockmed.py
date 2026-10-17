"""Explore a task once with a computer-use agent, then run it as a program.

Uses a recorded Anthropic computer-use trajectory as the agent, so it makes no
model calls. It serves the bundled MockMed app with its system of record:

    python examples/jit/run_mockmed.py

Run 1 is a cache miss. The agent's actions are recorded and compiled into a
DRAFT, which waits for you. You review its steps, it runs once as an attended
canary against MockMed, and you admit it under your name. Run 2
is a cache hit with a new note: the compiled program runs with no model calls
and is checked against the system of record. Run 3 uses an app that shows
"saved" and drops the note; the program stops and the run goes to the inbox.
The agent is not called again after run 1.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from openadapt_flow.jit import AdmissionError, TaskSignature, WebApp, compile_agent
from openadapt_flow.jit.recorded import RecordedTrajectoryAgent
from openadapt_flow.mockmed.fault_server import serve
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.tutorial import _http_json, _records

ROOT = Path(__file__).resolve().parents[2]
TRAJECTORY = ROOT / "tests" / "fixtures" / "jit" / "anthropic_add_note.json"
TASK = TaskSignature(
    "mockmed.add_note",
    "Open the first referral, start a Triage encounter, and save this note: {note}",
    {"note": str},
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", default=None, help="cache directory (default: temp)")
    parser.add_argument(
        "--yes", action="store_true", help="admit without the prompt (you still decide)"
    )
    parser.add_argument(
        "--principal", default="example-reviewer", help="your name, for the review"
    )
    args = parser.parse_args()
    cache = Path(args.cache or tempfile.mkdtemp(prefix="openadapt-jit-"))
    agent = RecordedTrajectoryAgent.from_file(TRAJECTORY)
    base, _db, stop = serve()
    base = base.rstrip("/")
    try:

        def app(fault: str) -> WebApp:
            return WebApp(
                url=f"{base}/?fault={fault}&idempotency=demo#tasks",
                record_reader=lambda: _records(base),
                reset=lambda: _http_json(f"{base}/api/reset", method="POST", body={}),
            )

        verifier = RestRecordVerifier(
            base, records_path="/api/db", records_key="records"
        )

        def wrapped(fault: str = "ok"):
            return compile_agent(
                agent,
                task=TASK,
                app=app(fault),
                verifier=verifier,
                policy="clinical-write",
                cache_dir=cache,
                key_dir=cache / "admission-keys",
            )

        jit = wrapped()
        print("Run 1: the agent does the task while Flow records it")
        first = jit.run(note=agent.inputs["note"])
        print(f"  {first.status}; agent called: {first.agent_called}")
        if first.candidate is None:
            print(f"  no candidate: {first.detail}")
            return 1
        print(
            json.dumps(
                {k: first.candidate[k] for k in ("distilled", "params", "effects")},
                indent=2,
            )
        )
        answer = "ok" if args.yes else input("Admit this program? Type ok to admit: ")
        if answer.strip().lower() not in ("ok", "yes", "y"):
            print("Not admitted. The candidate stays in", jit.program_dir)
            return 0
        steps = [s["id"] for s in jit.status()["steps"]]
        try:
            jit.review(
                principal_id=args.principal,
                reviewed_steps=steps,
                accept_params=["note"],
                confirm="ok",
            )
        except AdmissionError:
            # Confirming the parameter recompiled the program; review its steps.
            steps = [s["id"] for s in jit.status()["steps"]]
            jit.review(
                principal_id=args.principal,
                reviewed_steps=steps,
                accept_params=["note"],
                confirm="ok",
            )
        canary = jit.canary(principal_id=args.principal, note="Canary test note")
        print(f"Canary: {canary.status}; model calls: {canary.model_calls}")
        if not canary.verified:
            print(f"  {canary.halt_reason or canary.detail}")
            return 1
        manifest = jit.admit(principal_id=args.principal, confirm="ok")
        print(f"  ADMITTED by {manifest['admitted_by_principal_id']}")

        print("Run 2: the admitted program, with a new note")
        second = jit.run(note="Recheck knee in ten days")
        print(
            f"  {second.status}; model calls: {second.model_calls}; "
            f"agent called: {second.agent_called}; records: {_records(base)}"
        )

        print("Run 3: the app shows 'saved' and drops the note")
        third = wrapped("optimistic").run(note="Recheck knee in ten days")
        print(
            f"  {third.status}; agent called: {third.agent_called}; "
            f"inbox: {third.inbox_item}"
        )
        print(f"  {third.halt_reason}")
        return 0 if second.verified and third.status == "HALTED" else 1
    finally:
        stop()


if __name__ == "__main__":
    raise SystemExit(main())
