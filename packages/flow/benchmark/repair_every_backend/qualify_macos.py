#!/usr/bin/env python3
"""Qualify a repaired update notice on a real macOS app, on two backend kinds.

The app is ``tests/native_apps/Chart.swift``: a native Cocoa window with a
patient banner, a "New note" form and a "Save note" button that inserts one
row into a SQLite ``notes`` table. Its "new version" shows an in-window
"Update available" notice on the first "New note".

For each backend kind the script:

1. records the healthy workflow on the real app (old version) and compiles it;
   the save gets a SQL record check (``SqlRecordVerifier``) and the patient
   banner as its identity band;
2. runs it on the new version, where it stops at the notice before any save;
3. runs ``SelfRepair``. The repair agent is scripted (it proposes clicking
   "Later", found by OCR) and so is the approver (it approves every request,
   through the decision portal's own code). The app, its accessibility tree,
   the screen, the input and the database are real;
4. checks the database: exactly one new row for the patient, and none from
   the fault probe (another patient's chart, which must stop before the save).

Backend kinds:

``ax``     ``MacOSBackend``: the new click is admitted by its AX element
           identity (role, name, ancestry, window) plus the record check.
``pixel``  ``RemoteDisplayBackend`` over the same window: pixels in, OS input
           out, no accessibility reads. The new click is admitted by the record
           check alone.

It also checks the refusals: with no record verifier the fix is not admitted
and nothing is written.

Usage::

    python benchmark/repair_every_backend/qualify_macos.py --kind ax --kind pixel

Writes ``benchmark/repair_every_backend/results/macos-<kind>-<date>.json``.
Needs macOS with Screen Recording and Accessibility access, and ``swiftc``.
It takes the screen for about a minute per kind.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

FLOW = Path(__file__).resolve().parents[2]
if str(FLOW) not in sys.path:
    sys.path.insert(0, str(FLOW))

APP_SOURCE = FLOW / "tests" / "native_apps" / "Chart.swift"
RESULTS = Path(__file__).resolve().parent / "results"
TITLE = "Chart"
# The MRNs carry none of 0/O/1/l/I: pixel identity abstains on those glyphs
# (OCR can't tell a homonym's MRN apart), and this run must verify.
PATIENT = ("rc79284", "MRN RC79284 Okafor, Philip born 1966-02-17")
OTHER = ("rc48572", "MRN RC48572 Adeyemi, Grace born 1971-03-29")
# Upper case, no punctuation: RemoteDisplayBackend's typing on a macOS host
# arrives shifted (see the results README), and both kinds type the same text.
DEMO_NOTE = "FOLLOW UP IN TWO WEEKS"
RUN_NOTE = "REPAIRED RUN FOLLOW UP IN THREE WEEKS"
APPROVER = "qualification:scripted-approver (stands in for a person; approves all)"


#: Set by ``--steady-caret``: launch the app with its text caret not blinking
#: (AppKit user defaults). A blinking caret changes the frame between the
#: runtime's lease and the save click, and the click is refused; see the README.
STEADY_CARET: list[str] = []
#: Set by ``--sheet``: the new version shows its notice as a real AppKit sheet
#: (an NSAlert attached to the window, its own window), not drawn in the window.
NOTICE_MODE = ["update_notice"]


class App:
    """One launched chart process; killed by exact PID."""

    def __init__(self, binary: Path, db: Path, patient: tuple[str, str], mode: str):
        # The app gets none of the throwaway approver roster's settings.
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("OPENADAPT_", "OAFLOW_"))
        }
        self.proc = subprocess.Popen(
            [str(binary), str(db), TITLE, patient[0], patient[1], mode, *STEADY_CARET],
            env=env,
        )

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=10)
        # macOS re-activates the previous app a moment after a kill; let that
        # happen now, not after the next chart window took the focus.
        time.sleep(1.5)


def _build(root: Path) -> Path:
    binary = root / "chart"
    subprocess.run(
        ["swiftc", "-O", "-o", str(binary), str(APP_SOURCE), "-lsqlite3"],
        check=True,
        timeout=300,
    )
    return binary


def _backend(kind: str) -> Any:
    from openadapt_flow.backends.macos_backend import MacOSBackend, QuartzMacAXClient
    from openadapt_flow.backends.remote_display import (
        MacWindowClient,
        RemoteDisplayBackend,
    )

    client = MacWindowClient()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if len([w for w in client.find_windows("chart", TITLE)]) == 1:
            break
        time.sleep(0.2)
    if kind == "ax":
        backend: Any = MacOSBackend(
            client,
            app="chart",
            window_title=TITLE,
            settle_s=0.05,
            foreground_settle_s=0.1,
            ax_client=QuartzMacAXClient(),
        )
        backend.ensure_foreground()
    else:
        # Bring the one chart window to the front, as an operator would before
        # a remote session runs; the backend then refuses input if it loses it.
        window = client.find_windows("chart", TITLE)[0]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            client.activate(window.pid)
            if (
                client.focused_application_pid() == window.pid
                and client.frontmost_window_id() == window.window_id
            ):
                break
            time.sleep(0.3)
        backend = RemoteDisplayBackend(
            client, owner_substr="chart", title_substr=TITLE, settle_s=0.3
        )
    time.sleep(0.5)
    backend.screenshot()
    return backend


def _squash(text: str) -> str:
    """Text as OCR compares it: no spaces, case or 0/O difference."""
    return "".join(text.split()).casefold().replace("0", "o")


def _find_text(backend: Any, text: str) -> tuple[int, int, tuple[int, int, int, int]]:
    """The center and box of the OCR line that reads ``text``."""
    from openadapt_flow.vision import ocr

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for line in ocr(backend.screenshot()):
            if _squash(line.text) == _squash(text):
                x, y, w, h = line.region
                return x + w // 2, y + h // 2, (int(x), int(y), int(w), int(h))
        time.sleep(0.3)
    raise RuntimeError(f"{text!r} is not on the screen")


def _rows(db: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS notes "
            "(id INTEGER PRIMARY KEY, patient_id TEXT, note TEXT)"
        )
        return [tuple(r) for r in conn.execute("SELECT patient_id, note FROM notes")]


def _record(binary: Path, root: Path, kind: str, db: Path) -> tuple[Path, dict]:
    """Record the healthy workflow on the old version and compile it."""
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import Workflow
    from openadapt_flow.policy import evaluate_policy, load_policy
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.runtime.effects import Effect, EffectKind
    from openadapt_flow.runtime.effects.effect import ValueExpr

    app = App(binary, db, PATIENT, "plain")
    try:
        backend = _backend(kind)
        recording = root / "recording"
        recorder = Recorder(
            backend, recording, settle_interval_s=0.05, settle_timeout_s=3.0
        )
        x, y, _ = _find_text(backend, "New note")
        recorder.click(x, y)
        _bx, _by, banner = _find_text(backend, PATIENT[1])
        fx, fy, _ = _find_text(backend, "Note")
        recorder.click(fx, fy)
        recorder.type_text(DEMO_NOTE, param="note")
        sx, sy, _ = _find_text(backend, "Save note")
        recorder.click(sx, sy)
        recorder.finish()
    finally:
        app.close()
    # The banner is every click's identity band (``--identifier`` in a browser).
    events_path = recording / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    pad = 6
    for event in events:
        if event.get("kind") == "click":
            event["identifier_region"] = [
                banner[0] - pad,
                banner[1] - pad,
                banner[2] + 2 * pad,
                banner[3] + 2 * pad,
            ]
    events_path.write_text("".join(json.dumps(e) + "\n" for e in events))
    bundle = root / "bundle"
    workflow = compile_recording(recording, bundle, name=f"chart-note-{kind}")
    save = workflow.steps[-1]
    save.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"patient_id": PATIENT[0], "note": ValueExpr(param="note")},
            composite_key={"patient_id": PATIENT[0], "note": ValueExpr(param="note")},
            expected_count=1,
            count_new_only=True,
            risk="irreversible",
            timeout_s=5.0,
        )
    ]
    save.risk = "irreversible"
    # The operator's risk review of the steps that don't save (what
    # ``openadapt-flow qualify set-risk`` records for a person).
    for step in workflow.steps[:-1]:
        step.risk = "reversible"
    (bundle / "manifest.json").unlink(missing_ok=True)
    workflow.save(bundle, encrypt=bool(workflow.encrypted))
    workflow = Workflow.load(bundle)
    report = evaluate_policy(workflow, load_policy("clinical-write"))
    return bundle, {
        "steps": [f"{s.id}:{s.action.value}:{s.intent}" for s in workflow.steps],
        "save_identity_armed": bool(
            workflow.steps[-1].anchor and workflow.steps[-1].anchor.identifier_crop
        ),
        "certified_clinical_write": report.passed,
        "certification": report.render() if not report.passed else "passed",
        "encrypted": bool(workflow.encrypted),
    }


class LaterAgent:
    """Scripted repair agent: proposes clicking "Later". No model calls."""

    dialect = "generic"
    agent_id = "qualification:scripted-repair-agent"
    requires_model_key = False

    def propose(self, request: Any) -> Any:
        from openadapt_flow.repair.self_repair import RepairProposal
        from openadapt_flow.vision import ocr

        for line in ocr(request.live_png):
            if _squash(line.text) == "later":
                x, y, w, h = line.region
                return RepairProposal(
                    actions=[{"kind": "click", "x": x + w // 2, "y": y + h // 2}],
                    note="dismiss the update notice",
                )
        return None


def _approver(root: Path, decisions: list[float]) -> Any:
    from openadapt_flow.console.save_approvals import (
        SaveApprovalActionRequest,
        decide_save_approval,
        list_save_approvals,
        save_approval_detail,
    )

    def sleep(seconds: float) -> None:
        for row in list_save_approvals(root):
            detail = save_approval_detail(root, row.request_id)
            decide_save_approval(
                root,
                row.request_id,
                SaveApprovalActionRequest(
                    action="verify_and_resume",
                    task_digest=detail["task_digest"],
                    task_signature=detail["task"]["signature"],
                ),
            )
            decisions.append(time.time())
        time.sleep(min(seconds, 0.2))

    return sleep


def _target(binary: Path, db: Path, kind: str, *, verifier: bool, profile: str) -> Any:
    from openadapt_flow.repair.self_repair import AppSession, ReplayTarget
    from openadapt_flow.runtime.effects import SqlRecordVerifier

    def open_session(fault: Optional[str]) -> Any:
        app = App(
            binary, db, OTHER if fault == "other_patient" else PATIENT, NOTICE_MODE[0]
        )
        try:
            backend = _backend(kind)
        except BaseException:
            app.close()
            raise
        return AppSession(backend=backend, close=app.close)

    return ReplayTarget(
        open_session,
        effect_verifier=SqlRecordVerifier(
            lambda: sqlite3.connect(db),
            "SELECT id, patient_id, note FROM notes WHERE patient_id = :patient_id",
            query_params={"patient_id": PATIENT[0]},
            poll_interval_s=0.1,
        )
        if verifier
        else None,
        policy="clinical-write",
        profile=profile,
        params={"note": RUN_NOTE},
        execution_target_kind="macos" if kind == "ax" else "rdp",
    )


def _repair(
    binary: Path,
    root: Path,
    bundle: Path,
    kind: str,
    case: str,
    *,
    verifier: bool = True,
    profile: str = "standard",
) -> dict[str, Any]:
    from openadapt_flow.repair.self_repair import (
        ScreenAccess,
        SelfRepair,
        SelfRepairRefused,
    )

    db = root / f"sor-{case}.db"
    _rows(db)
    decisions: list[float] = []
    run_root = root / case
    run_root.mkdir()
    repair = SelfRepair(
        LaterAgent(),
        _target(binary, db, kind, verifier=verifier, profile=profile),
        store=run_root / "repair-store",
        screen_access=ScreenAccess(
            enabled_by="qualification-operator", mode="allow_unscrubbed"
        ),
        fault_probes=("other_patient",),
        approval_wait_s=120,
        sleep=_approver(run_root, decisions),
    )
    started = time.monotonic()
    try:
        result = repair.run(bundle, run_root / "item")
    except SelfRepairRefused as exc:
        return {
            "status": "refused",
            "reason": str(exc)[:2000],
            "person_decisions": len(decisions),
            "stages": [],
            "rows": _rows(db),
        }
    evidence = getattr(result, "evidence", None)
    return {
        "status": result.status,
        "reason": evidence.reason if evidence is not None else "",
        "duration_s": round(time.monotonic() - started, 1),
        "person_decisions": len(decisions),
        "approver": APPROVER,
        "stages": [
            {"stage": s.stage, "passed": s.passed, "detail": s.detail}
            for s in (evidence.steps if evidence is not None else [])
        ],
        "rows": _rows(db),
    }


def _approver_roster(root: Path) -> None:
    """A throwaway approver roster in the work folder, for the scripted answer.

    The decision portal signs a person's answer with a key on the signed
    roster; this provisions one the same way the test fixture does. No real
    approver key is used or touched.
    """
    import secrets

    from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
    from openadapt_flow.approval_mode.human_attestation import PORTAL_KEY_ENV
    from openadapt_flow.jit.authority import KEY_PASSPHRASE_ENV
    from openadapt_flow.jit.lifecycle import KEY_DIR_ENV

    sys.path.insert(0, str(FLOW))
    from tests import _approver_keys

    key_dir = root / "approver-keys"
    _approver_keys.PASSPHRASE = secrets.token_urlsafe(16)
    os.environ[KEY_DIR_ENV] = str(key_dir)
    os.environ[KEY_PASSPHRASE_ENV] = _approver_keys.PASSPHRASE
    _approver_keys.provision(key_dir)
    os.environ[ROOT_ENV] = _approver_keys.root_of(key_dir)
    portal, _signer = _approver_keys.provision(
        key_dir, "decision-portal", method="portal_key"
    )
    os.environ[PORTAL_KEY_ENV] = str(portal)


def qualify(kind: str, root: Path) -> dict[str, Any]:
    _approver_roster(root)
    binary = _build(root)
    demo_db = root / "sor-demo.db"
    bundle, compiled = _record(binary, root / "record", kind, demo_db)
    checked = _repair(binary, root, bundle, kind, "with-record-check")
    no_verifier = _repair(binary, root, bundle, kind, "no-verifier", verifier=False)
    demo = _repair(binary, root, bundle, kind, "demo-profile", profile="demo")
    expected_basis = "element_and_record_check" if kind == "ax" else "record_check"
    checks = {
        "demonstration_saved_one_row": _rows(demo_db) == [(PATIENT[0], DEMO_NOTE)],
        "bundle_certified": compiled["certified_clinical_write"],
        "repair_resumed": checked["status"] == "resumed",
        "resume_verified": any(
            s["stage"] == "resume" and s["passed"] for s in checked["stages"]
        ),
        "exactly_one_new_row_for_the_patient": checked["rows"]
        == [(PATIENT[0], RUN_NOTE)],
        "click_admitted_on_expected_basis": any(
            s["stage"] == "patch" and f"admitted by {expected_basis}" in s["detail"]
            for s in checked["stages"]
        ),
        "one_person_decision": checked["person_decisions"] == 1,
        "fault_probe_stopped_before_save": any(
            s["stage"] == "campaign" and s["passed"] for s in checked["stages"]
        ),
        "no_verifier_refused": no_verifier["status"] == "refused",
        "no_verifier_wrote_nothing": no_verifier["rows"] == [],
        "demo_profile_click_not_admitted": demo["status"] not in ("resumed",)
        and "none covers this workflow" in json.dumps(demo),
        "demo_profile_wrote_nothing": demo["rows"] == [],
        "demo_profile_no_person_asked": demo["person_decisions"] == 0,
    }
    return {
        "schema": "openadapt.repair-every-backend-qualification/v1",
        "kind": kind,
        "backend": "MacOSBackend (AX)"
        if kind == "ax"
        else "RemoteDisplayBackend (pixels only)",
        "app": "tests/native_apps/Chart.swift (native Cocoa, SQLite)",
        "steady_caret": bool(STEADY_CARET),
        "notice": NOTICE_MODE[0],
        "at": datetime.now(timezone.utc).isoformat(),
        "git_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=FLOW
        ).stdout.strip(),
        # Uncommitted changes under packages/flow other than the results this
        # script writes. Evidence counts only from a clean tree.
        "git_dirty": [
            line
            for line in subprocess.run(
                ["git", "status", "--porcelain", "--", "."],
                capture_output=True,
                text=True,
                cwd=FLOW,
            ).stdout.splitlines()
            if "repair_every_backend/results/" not in line
        ],
        "compiled": compiled,
        "repair_with_record_check": checked,
        "repair_without_verifier": no_verifier,
        "repair_under_demo_profile": demo,
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", action="append", choices=("ax", "pixel"))
    parser.add_argument("--keep", action="store_true", help="keep the work folder")
    parser.add_argument(
        "--steady-caret",
        action="store_true",
        help="launch the app with a caret that doesn't blink",
    )
    parser.add_argument(
        "--sheet",
        action="store_true",
        help="show the notice as a real AppKit sheet (its own window)",
    )
    args = parser.parse_args()
    if args.sheet:
        NOTICE_MODE[0] = "update_sheet"
    if args.steady_caret:
        STEADY_CARET.extend(
            [
                "-NSTextInsertionPointBlinkPeriodOn",
                "100000000",
                "-NSTextInsertionPointBlinkPeriodOff",
                "0",
            ]
        )
    RESULTS.mkdir(parents=True, exist_ok=True)
    failed = False
    for kind in args.kind or ["ax", "pixel"]:
        root = Path(tempfile.mkdtemp(prefix=f"oa-repair-{kind}-"))
        saved_env = dict(os.environ)
        try:
            report = qualify(kind, root)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
            if not args.keep:
                shutil.rmtree(root, ignore_errors=True)
            else:
                print(f"work folder: {root}")
        mode = kind + ("-sheet" if NOTICE_MODE[0] == "update_sheet" else "")
        mode += "-steady-caret" if STEADY_CARET else ""
        out = RESULTS / f"macos-{mode}-{datetime.now(timezone.utc):%Y%m%d}.json"
        out.write_text(json.dumps(report, indent=2) + "\n")
        print(f"{kind}: {'PASSED' if report['passed'] else 'FAILED'} -> {out}")
        for name, ok in report["checks"].items():
            print(f"  {'ok ' if ok else 'BAD'} {name}")
        failed |= not report["passed"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
