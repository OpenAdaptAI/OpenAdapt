"""Measure the opt-in fallback on MockMed drifts, headless, synthetic data.

    python benchmark/wrap_fallback/measure.py --out benchmark/wrap_fallback/results.json

For each drift mode: a scripted agent's run 1 is compiled and admitted on the
undrifted app, MockMed drifts, and the task runs once through
``FallbackPolicy`` with a scripted fallback agent that knows the drifted
labels and a gate that approves every save that reaches it (it stands in for
the person). One more trial per mode has the gate refuse. After every trial
the script reads MockMed's own store and classifies the outcome: ``correct``
(exactly one record, for the task's patient, with the typed note), ``none``
(nothing saved) or ``wrong`` (anything else).

The compiled program runs in process on a thread (the MCP worker-thread fix
is #2406). No model is called.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]
sys.path.insert(0, str(FLOW))

from tests.e2e.test_wrap_fallback_e2e import (  # noqa: E402
    NOTE,
    PROMPT,
    DriftableMockMed,
    Gate,
    admitted,
    saved,
    steps,
)

#: Drift modes, and the steps the scripted fallback agent takes on each.
MODES: dict[str, list] = {
    "rename": steps(save="Submit Encounter", new="New Encounter"),
    "redesign": steps(save="Submit to chart"),
    "relink": steps(new="Start visit"),
}


class Env:
    """The two monkeypatch calls the shared helpers make, outside pytest."""

    def __init__(self) -> None:
        self.undo: list = []

    def setenv(self, name: str, value: str) -> None:
        import os

        self.undo.append(("env", name, os.environ.get(name)))
        os.environ[name] = value

    def setattr(self, target: Any, name: str, value: Any) -> None:
        self.undo.append(("attr", target, name, getattr(target, name)))
        setattr(target, name, value)

    def close(self) -> None:
        import os

        for item in reversed(self.undo):
            if item[0] == "env":
                if item[2] is None:
                    os.environ.pop(item[1], None)
                else:
                    os.environ[item[1]] = item[2]
            else:
                setattr(item[1], item[2], item[3])


def classify(rows: list) -> str:
    if not rows:
        return "none"
    return "correct" if rows == [("p1", "Triage", NOTE)] else "wrong"


def trial(mode: str, approve: bool) -> dict[str, Any]:
    from openadapt_flow.agent.repair import RepairAccessPolicy, RepairDesk
    from openadapt_flow.compile_agent import ScriptedAgent
    from openadapt_flow.wrap.fallback import FallbackPolicy

    env = Env()
    with tempfile.TemporaryDirectory(prefix="wrap-fallback-") as raw:
        tmp = Path(raw)
        app = DriftableMockMed(tmp / "app")
        try:
            cache = admitted(tmp, app, env)
            app.drift(mode)
            gate = Gate(approve=approve)
            policy = FallbackPolicy(
                cache,
                max_cost_usd=0.5,
                gate=gate,
                repair_desk=RepairDesk(tmp / "repairs", RepairAccessPolicy(None)),
            )
            started = time.monotonic()
            run = policy.run(PROMPT, {"note": NOTE}, ScriptedAgent(steps=MODES[mode]))
            seconds = round(time.monotonic() - started, 2)
            rows = saved(app)
        finally:
            app.stop()
            env.close()
    last = policy.last or {}
    program = run.replay if run.path == "skill" else run.replay.get("program")
    return {
        "mode": mode,
        "gate": "approve" if approve else "refuse",
        "program_status": program.get("status") if isinstance(program, dict) else None,
        "path": run.path,
        "status": run.status,
        "verified": run.verified,
        "fallback_reason": last.get("reason"),
        "approvals_needed": len(gate.requests),
        "model_calls": run.model_calls,
        "seconds": seconds,
        "saved": classify(rows),
        "repair_opened": bool((last.get("repair") or {}).get("opened")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--out", required=True)
    parser.add_argument("--modes", nargs="*", default=sorted(MODES))
    args = parser.parse_args()
    trials = [trial(m, approve) for m in args.modes for approve in (True, False)]
    fell_back = [t for t in trials if t["path"] == "agent_fallback"]
    summary = {
        "drifts": len(args.modes),
        "trials": len(trials),
        "program_verified": sum(t["path"] == "skill" and t["verified"] for t in trials),
        "fallbacks": len(fell_back),
        "fallback_verified": sum(t["verified"] for t in fell_back),
        "wrong_saves": sum(t["saved"] == "wrong" for t in trials),
        "saves_after_refusal": sum(
            t["gate"] == "refuse" and t["saved"] != "none" and t["path"] != "skill"
            for t in trials
        ),
        "approvals_needed": sum(t["approvals_needed"] for t in trials),
        "model_calls": sum(t["model_calls"] or 0 for t in trials),
    }
    result = {
        "schema": "openadapt.wrap-fallback-benchmark/v1",
        "app": "MockMed (synthetic), headless Chromium",
        "agent": "scripted (no model)",
        "gate": "scripted: approves or refuses every save that reaches it",
        "measured_at": time.strftime("%Y-%m-%d"),
        "python": platform.python_version(),
        "summary": summary,
        "trials": trials,
    }
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["wrong_saves"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
