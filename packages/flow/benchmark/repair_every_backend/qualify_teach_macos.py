#!/usr/bin/env python3
"""Qualify teaching a fix over MCP on a halted native macOS run (#1694).

The app is the Chart app from ``qualify_macos.py`` (native Cocoa, SQLite).
The whole path is the real one an operator gets:

1. the healthy workflow is recorded on the old version through
   ``MacOSBackend`` (accessibility) and compiled;
2. ``openadapt-agent``'s bridge runs it through the governed
   ``openadapt-flow run`` CLI (``--config`` with the macOS backend and an
   SQLite record check, ``--hold-on-halt``) on the new version, which shows an
   "Update available" notice. The run stops before any save, returns
   ``needs_repair`` and keeps its app session open;
3. the calling agent (scripted: it reads "Later" off ``get_repair_screen``)
   calls ``teach_repair``; the click lands on the held session through the
   hold socket, and Flow returns a candidate that waits for a person;
4. until a person approves, the next run still stops; then a scripted person
   reviews, Flow runs the replay and fault campaigns, the person approves,
   and the candidate is staged and canaried;
5. the next run on a fresh copy of the new version passes: VERIFIED against
   SQLite, exactly one new row for the patient, no model calls.

The calling agent and the person are scripted; the app, accessibility tree,
screen, input, CLI run, hold, campaigns and database are real.

Usage (from packages/flow)::

    python benchmark/repair_every_backend/qualify_teach_macos.py --steady-caret
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import qualify_macos as mac  # noqa: E402

AGENT = "qualification:calling-agent"
PERSON_ID = "qualification:scripted-person"


def _deploy(root: Path, db: Path) -> Path:
    import yaml

    path = root / "deploy.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "chart-note",
                "backend": {
                    "kind": "macos",
                    "macos_app": "chart",
                    "macos_window_title": mac.TITLE,
                },
                "effects": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": (
                        "SELECT id, patient_id, note FROM notes "
                        "WHERE patient_id = :patient_id"
                    ),
                    "sql_query_params": {"patient_id": mac.PATIENT[0]},
                    "timeout_s": 10.0,
                    "poll_interval_s": 0.2,
                },
                "runtime": {"profile": "standard"},
                "policy": {"policy": "clinical-write"},
            }
        )
    )
    return path


class _Person(threading.Thread):
    """Answers every save waiting under ``runs`` through the decision portal."""

    def __init__(self, runs: Path) -> None:
        super().__init__(daemon=True)
        self.runs, self.decisions, self.stop = runs, [], threading.Event()

    def run(self) -> None:
        answer = mac._approver(self.runs, self.decisions)
        while not self.stop.is_set():
            try:
                answer(0.2)
            except Exception:  # noqa: BLE001 - a save not yet listed; try again
                time.sleep(0.2)


def _later(screen_png: bytes) -> tuple[int, int]:
    from openadapt_flow.vision import ocr

    for line in ocr(screen_png):
        if mac._squash(line.text) == "later":
            x, y, w, h = line.region
            return x + w // 2, y + h // 2
    raise AssertionError("the agent found no 'Later' on the repair screen")


def qualify(root: Path) -> dict[str, Any]:
    from openadapt_flow.agent.bridge import AgentBridge
    from openadapt_flow.agent.repair import RepairAccessPolicy, RepairDesk, set_access
    from openadapt_flow.agent.runner import RunnerConfig
    from openadapt_flow.repair import Actor, CanaryRunObservation, RepairStore
    from openadapt_flow.repair.cli import _run_campaign

    mac._approver_roster(root)
    binary = mac._build(root)
    bundles = root / "bundles"
    bundle, compiled = mac._record(binary, root / "record", "ax", root / "demo.db")
    (bundles).mkdir()
    bundle = Path(bundle).rename(bundles / "chart-note")
    db = root / "sor.db"
    deploy = _deploy(root, db)
    runs = root / "runs"
    access = root / "access.json"
    set_access(access, workflow=None, mode="allow_unscrubbed", enabled_by=PERSON_ID)
    desk = RepairDesk(root / ".repair", RepairAccessPolicy(access), agent_id=AGENT)
    bridge = AgentBridge(
        bundles,
        RunnerConfig(
            flow_cli=(sys.executable, "-m", "openadapt_flow"),
            runs_dir=runs,
            deployment_config=str(deploy),
            hold_on_halt_s=300.0,
            timeout_s=900.0,
        ),
        allow_run=True,
        repair_desk=desk,
    )
    [name] = list(bridge.catalog)
    person = _Person(runs)
    person.start()
    steps: dict[str, Any] = {"workflow": name}

    def run(request_id: str) -> dict[str, Any]:
        return bridge.dispatch(
            "run_workflow",
            {
                "workflow": name,
                "inputs": {"note": mac.RUN_NOTE},
                "request_id": request_id,
            },
        )

    try:
        # 1-2. The new version stops at its notice and holds the session.
        app = mac.App(binary, db, mac.PATIENT, mac.NOTICE_MODE[0])
        try:
            halted = run("req-0001")
            steps["halted"] = {
                k: halted.get(k) for k in ("outcome", "reason", "model_calls")
            }
            block = halted.get("needs_repair") or {}
            steps["needs_repair"] = {
                k: block.get(k) for k in ("step_id", "teach_repair_available")
            }
            # 3. The calling agent teaches one click on the held session.
            shown = bridge.dispatch(
                "get_repair_screen", {"repair_token": block["repair_token"]}
            )
            import base64

            x, y = _later(base64.b64decode(shown["screen_png_base64"]))
            taught = bridge.dispatch(
                "teach_repair",
                {
                    "repair_token": block["repair_token"],
                    "demonstration": {"action": "click", "x": x, "y": y},
                },
            )
            steps["taught"] = {
                k: taught.get(k) for k in ("status", "candidate_id", "what_happened")
            }
        finally:
            app.close()
        if taught.get("status") != "pending_approval":
            return {"steps": steps, "rows": _rows(db)}

        # 4. Not yet approved: the next run still stops.
        app = mac.App(binary, db, mac.PATIENT, mac.NOTICE_MODE[0])
        try:
            again = run("req-0002")
            steps["before_approval"] = again.get("outcome")
            _release_hold(runs, again)
        finally:
            app.close()
        store = RepairStore(desk.store_for(name))
        cid = taught["candidate_id"]
        person_actor = Actor(kind="human", id=PERSON_ID)
        automation = Actor(kind="automation", id="qualification")
        store.review(cid, person_actor)
        campaigns = {}
        for kind in ("replay", "fault"):
            campaign = _run_campaign(store.load_candidate(cid), kind)
            campaigns[kind] = campaign.passed
            store.record_campaign(cid, campaign, automation)
        steps["campaigns"] = campaigns
        store.approve(cid, person_actor, non_interactive=True)
        store.stage(cid, automation)
        store.start_canary(cid, automation, max_runs=1)

        # 5. The approved fix replays on a fresh copy of the new version.
        app = mac.App(binary, db, mac.PATIENT, mac.NOTICE_MODE[0])
        try:
            passed = run("req-0003")
        finally:
            app.close()
        steps["after_approval"] = {
            k: passed.get(k) for k in ("outcome", "reason", "model_calls")
        }
        if passed.get("outcome") == "done":
            store.record_canary_run(
                cid,
                CanaryRunObservation(
                    run_id=passed["run_id"], verified=True, silent_incorrect=False
                ),
            )
        steps["candidate_state"] = store.load_candidate(cid).state
    finally:
        person.stop.set()
    return {"steps": steps, "rows": _rows(db), "decisions": len(person.decisions)}


def _release_hold(runs: Path, result: dict[str, Any]) -> None:
    """End a held run's hold (a second halt isn't taught here)."""

    from openadapt_flow.agent.repair import HeldRunDemonstrator

    run_dir = runs / str(result.get("run_id", ""))
    held = HeldRunDemonstrator.for_run(run_dir)
    if held is not None:
        held.release()


def _rows(db: Path) -> list[tuple[str, str]]:
    if not db.is_file():
        return []
    with sqlite3.connect(db) as conn:
        return [tuple(r) for r in conn.execute("SELECT patient_id, note FROM notes")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--steady-caret", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.steady_caret:
        mac.STEADY_CARET.extend(
            [
                "-NSTextInsertionPointBlinkPeriodOn",
                "100000000",
                "-NSTextInsertionPointBlinkPeriodOff",
                "0",
            ]
        )
    root = Path(tempfile.mkdtemp(prefix="oa-teach-ax-"))
    saved_env = dict(os.environ)
    try:
        result = qualify(root)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
    steps = result["steps"]
    checks = {
        "halted_with_needs_repair": (steps.get("needs_repair") or {}).get(
            "teach_repair_available"
        )
        is True,
        "taught_pending_approval": (steps.get("taught") or {}).get("status")
        == "pending_approval",
        "still_halts_before_approval": steps.get("before_approval") == "needs_review",
        "campaigns_passed": all((steps.get("campaigns") or {"x": False}).values()),
        "approved_fix_verified": (steps.get("after_approval") or {}).get("outcome")
        == "done",
        "no_model_calls_after_approval": (steps.get("after_approval") or {}).get(
            "model_calls"
        )
        == 0,
        "exactly_one_row_for_the_patient": result["rows"]
        == [(mac.PATIENT[0], mac.RUN_NOTE)],
    }
    report = {
        "schema": "openadapt.repair-every-backend-qualification/v1",
        "kind": "teach-ax",
        "backend": "MacOSBackend (AX), through openadapt-flow run --hold-on-halt",
        "app": "tests/native_apps/Chart.swift (native Cocoa, SQLite)",
        "steady_caret": bool(mac.STEADY_CARET),
        "at": datetime.now(timezone.utc).isoformat(),
        "git_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=mac.FLOW
        ).stdout.strip(),
        "git_dirty": [
            line
            for line in subprocess.run(
                ["git", "status", "--porcelain", "--", "."],
                capture_output=True,
                text=True,
                cwd=mac.FLOW,
            ).stdout.splitlines()
            if "repair_every_backend/results/" not in line
        ],
        **result,
        "checks": checks,
        "passed": all(checks.values()),
    }
    mac.RESULTS.mkdir(parents=True, exist_ok=True)
    out = mac.RESULTS / f"macos-teach-ax-{datetime.now(timezone.utc):%Y%m%d}.json"
    out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"teach-ax: {'PASSED' if report['passed'] else 'FAILED'} -> {out}")
    for name, ok in checks.items():
        print(f"  {'ok ' if ok else 'BAD'} {name}")
    print(json.dumps(steps, indent=2, default=str))
    if args.keep:
        print(f"work folder: {root}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
