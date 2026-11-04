"""Live compensating rollback on MockMed, through the real CLI.

One in-process MockMed (the SPA plus its record store) on 127.0.0.1 and one
headless Chromium at a time:

1. record the canonical triage demonstration with network capture; compile
   it; bind the save's record check as an operator would;
2. ``openadapt-flow rollback qualify`` on the app declared a test copy: a
   stop before and after the save; the compensation that restores the store
   is written as a proposal, and the harness approves it as the operator
   (``accept_proposal``; the CLI's ``undo accept`` needs a terminal);
3. three ``openadapt-flow replay`` runs with ``rollback.enabled``, each made to
   stop by one added step whose screen check never passes:
   - ``after_save``: the stop comes after the save. Expect ``ROLLED_BACK``
     and an empty store;
   - ``before_save``: the stop comes before the save (a filled form). Expect
     nothing landed;
   - ``after_save_unapproved``: as ``after_save`` with the proposal not
     approved. Expect ``RECONCILIATION_REQUIRED``, the record left in place,
     and the diff file.

Synthetic data only; no model calls.

    cd packages/flow && PYTHONPATH=../types:. python benchmark/rollback/run_mockmed.py \\
        --out benchmark/rollback/results
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import requests
import yaml

HERE = Path(__file__).resolve().parent


def _undo_harness() -> Any:
    path = HERE.parent / "undo_proposal" / "run.py"
    spec = importlib.util.spec_from_file_location("undo_proposal_run", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _stop_step(step_id: str) -> dict[str, Any]:
    """A step that stops the run: its screen check never passes."""
    return {
        "id": step_id,
        "intent": "sign and lock the encounter (made to fail)",
        "action": "key",
        "key": "Escape",
        "expect": [
            {
                "kind": "text_present",
                "text": "Encounter signed and locked",
                "timeout_s": 1.5,
            }
        ],
    }


def _variant(bundle: Path, out: Path, *, stop_before_save: bool, save_id: str) -> Path:
    from openadapt_flow.ir import Step, Workflow

    shutil.copytree(bundle, out)
    workflow = Workflow.load(out)
    index = next(i for i, s in enumerate(workflow.steps) if s.id == save_id)
    at = index if stop_before_save else index + 1
    workflow.steps.insert(at, Step.model_validate(_stop_step("stop_here")))
    workflow.save(out)
    return out


def _records(base: str) -> list[dict[str, Any]]:
    return requests.get(f"{base}/api/db", timeout=5).json()["records"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    from openadapt_flow.__main__ import main as flow
    from openadapt_flow.compiler.compile import compile_recording
    from openadapt_flow.ir import RunReport
    from openadapt_flow.undo_proposal import accept_proposal

    harness = _undo_harness()
    fixture = harness._fixtures()["mockmed"]
    args.out.mkdir(parents=True, exist_ok=True)
    url, _db, stop = fixture.serve(port=0)
    base = url.rstrip("/")
    started = time.monotonic()
    rows: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="rollback-mockmed-") as tmp:
        work = Path(tmp)
        try:
            recording = harness._record_with_capture(
                fixture, base + "/" + fixture.query, work / "rec"
            )
            bundle = work / "bundle"
            compile_recording(recording, bundle, name="mockmed-triage")
            body = harness._save_body(recording)
            save_id, _labelled = harness._bind_save_effect(bundle, fixture, body)
            contract = harness._contract(work / "contract", base, fixture, body)
            deployment = harness._deployment(
                work / "deployment.yaml", fixture, base, contract
            )
            cfg = yaml.safe_load(deployment.read_text())
            # MockMed's only compensation is a DELETE; this synthetic app has no
            # retention rule, so the deployment allows deletes explicitly.
            # It is also a declared test copy no one else writes.
            cfg["rollback"] = {"enabled": True, "allow_delete": True, "exclusive": True}
            deployment.write_text(yaml.safe_dump(cfg, sort_keys=False))

            qualify_report = args.out / "mockmed-qualify.json"
            code = flow(
                [
                    "rollback",
                    "qualify",
                    str(bundle),
                    "--config",
                    str(deployment),
                    "--recording",
                    str(recording),
                    "--work-dir",
                    str(work / "qualify"),
                    "--report",
                    str(qualify_report),
                ]
            )
            qualified = json.loads(qualify_report.read_text())
            rows["qualify"] = {"exit_code": code, "summary": qualified["summary"]}
            written = qualified.get("written") or []

            unapproved = work / "bundle-unapproved"
            shutil.copytree(bundle, unapproved)
            for undo_id in written:
                accept_proposal(bundle, undo_id, approver="benchmark-operator")

            cases = {
                "after_save": (bundle, False),
                "before_save": (bundle, True),
                "after_save_unapproved": (unapproved, False),
            }
            for name, (source, before_save) in cases.items():
                requests.post(f"{base}/api/reset", json={}, timeout=5)
                variant = _variant(
                    source,
                    work / f"variant-{name}",
                    stop_before_save=before_save,
                    save_id=save_id,
                )
                run_dir = work / f"run-{name}"
                try:
                    code = flow(
                        [
                            "replay",
                            str(variant),
                            "--config",
                            str(deployment),
                            "--run-dir",
                            str(run_dir),
                        ]
                    )
                except SystemExit as exc:
                    # A write that can't be undone and no person to ask:
                    # refused before the run.
                    rows[name] = {
                        "refused_before_run": True,
                        "message": str(exc.code),
                        "report_written": (run_dir / "report.json").is_file(),
                        "records_left": len(_records(base)),
                    }
                    continue
                report = RunReport.model_validate_json(
                    (run_dir / "report.json").read_text()
                )
                rollback = report.rollback
                rows[name] = {
                    "exit_code": code,
                    "stopped_at": next(
                        (r.step_id for r in report.results if not r.ok), None
                    ),
                    "transaction_outcome": report.transaction_outcome,
                    "rollback_status": rollback.status if rollback else None,
                    "rollback_reason": rollback.reason if rollback else None,
                    "after_stop_diff": rollback.after_stop_diff if rollback else None,
                    "remaining_diff": rollback.remaining_diff if rollback else None,
                    "steps": [
                        {k: s[k] for k in ("step_id", "decision", "fresh_read")}
                        for s in (
                            rollback.model_dump(mode="json")["steps"]
                            if rollback
                            else []
                        )
                    ],
                    "diff_file_written": bool(rollback and rollback.diff_path),
                    "records_left": len(_records(base)),
                    "model_calls": report.model_calls,
                }
        finally:
            stop()
    summary = {
        "schema": "openadapt.rollback-measurement/v1",
        "app": "mockmed",
        "mode": "live (real browser, in-process app declared a test copy)",
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": platform.platform(),
        "load_1m": round(os.getloadavg()[0], 2),
        "seconds": round(time.monotonic() - started, 1),
        "rows": rows,
    }
    (args.out / "mockmed.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: rows[k] for k in rows if k != "qualify"}, indent=2)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
