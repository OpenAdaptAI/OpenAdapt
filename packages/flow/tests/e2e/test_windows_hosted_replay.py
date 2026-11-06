"""Record, compile and replay Patient Notes through ``WindowsBackend`` on this machine.

The CI form of ``test_parallels_desktop_e2e.py``. Everything runs on the
Windows host the test runs on, which on CI is a GitHub-hosted
``windows-latest`` runner:

* the in-tree WinForms Patient Notes app (``scripts/desktop/patient_notes.ps1``)
  with its SQLite record (``scripts/desktop/pn_db.py``);
* the real ``win_agent`` server over pinned TLS with a bearer token;
* ``WindowsBackend`` talking to that agent.

The demonstration is recorded through the agent, compiled, and replayed from a
reseeded database. The database, read with ``pn_db.py all`` and never through
the screen, decides the outcome: the replay must write the note to patient 1
and change no other row. A second replay, against a roster where the search
selects a near-identical sibling, must stop at the identity check and write
nothing.

Opt-in: skipped unless ``OAFLOW_WINDOWS_HOSTED_E2E=1`` on Windows.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

RUN_E2E = os.environ.get("OAFLOW_WINDOWS_HOSTED_E2E") == "1" and sys.platform == "win32"

pytestmark = [
    pytest.mark.skipif(
        not RUN_E2E,
        reason="opt-in hosted Windows e2e; set OAFLOW_WINDOWS_HOSTED_E2E=1 on Windows",
    ),
    pytest.mark.timeout(900),
]

FLOW = Path(__file__).resolve().parents[2]
DESKTOP_SCRIPTS = FLOW / "scripts" / "desktop"
CONTROLS = ("searchBox", "searchButton", "noteBox", "saveButton")
DEMO_SEARCH = "Neil"
DEMO_NOTE = "BP 128/82; follow-up in 2 weeks"
TARGET_ID = 1
EVIDENCE_ENV = "OAFLOW_WINDOWS_HOSTED_EVIDENCE"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Host:
    """The app, its database and the agent, all on this machine."""

    def __init__(self, root: Path) -> None:
        self.root = root
        for name in ("pn_db.py", "patient_notes.ps1"):
            shutil.copy2(DESKTOP_SCRIPTS / name, root / name)
        self.env = {**os.environ, "PN_DB": str(root / "patients.db")}
        self.app: subprocess.Popen[bytes] | None = None
        self.agent: subprocess.Popen[bytes] | None = None

    def db(self, *args: str) -> str:
        done = subprocess.run(
            [sys.executable, str(self.root / "pn_db.py"), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return done.stdout

    def rows(self) -> list[dict[str, Any]]:
        for line in reversed(self.db("all").splitlines()):
            try:
                value = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if isinstance(value, list):
                return value
        raise AssertionError("pn_db.py all returned no JSON rows")

    def launch_app(self, drift: str = "none") -> None:
        self.stop_app()
        self.db("seed", "--drift", drift)
        self.app = subprocess.Popen(
            [
                "powershell.exe",
                "-STA",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.root / "patient_notes.ps1"),
                "-Python",
                sys.executable,
                "-DbCli",
                str(self.root / "pn_db.py"),
            ],
            env=self.env,
        )

    def stop_app(self) -> None:
        if self.app is not None and self.app.poll() is None:
            self.app.kill()
            self.app.wait(timeout=30)
        self.app = None

    def start_agent(self, *, capture_telemetry_sidecar: Path | None = None):
        from openadapt_flow.backends.win_agent.tls import generate_self_signed_cert
        from openadapt_flow.backends.windows_backend import WindowsBackend

        cert = generate_self_signed_cert(dirpath=str(self.root / "tls"))
        token = secrets.token_hex(16)
        port = _free_port()
        log = open(self.root / "agent.log", "wb")  # noqa: SIM115
        command = [
            sys.executable,
            "-m",
            (
                "tests.e2e.win_agent_capture_telemetry"
                if capture_telemetry_sidecar is not None
                else "openadapt_flow.backends.win_agent.server"
            ),
        ]
        if capture_telemetry_sidecar is not None:
            if capture_telemetry_sidecar.exists() or capture_telemetry_sidecar.is_symlink():
                raise AssertionError("capture telemetry sidecar must be new")
            command.extend(
                ["--capture-telemetry-sidecar", str(capture_telemetry_sidecar)]
            )
        command.extend(
            [
                "--port",
                str(port),
                "--token",
                token,
                "--certfile",
                cert.certfile,
                "--keyfile",
                cert.keyfile,
            ]
        )
        self.agent = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        backend = WindowsBackend(
            f"https://127.0.0.1:{port}",
            auth_token=token,
            pin_fingerprint=cert.fingerprint,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if backend.probe():
                    return backend
            except Exception:  # noqa: BLE001 - the agent is still starting
                pass
            time.sleep(0.5)
        raise AssertionError((self.root / "agent.log").read_text(errors="replace"))

    def close(self) -> None:
        self.stop_app()
        if self.agent is not None and self.agent.poll() is None:
            self.agent.kill()
            self.agent.wait(timeout=30)


def _points(backend, *, timeout_s: float = 30.0) -> dict[str, tuple[int, int]]:
    """Each control's UIA centre; waits for the window to come up."""
    from openadapt_flow.ir import StructuralLocator

    points: dict[str, tuple[int, int]] = {}
    deadline = time.monotonic() + timeout_s
    for aid in CONTROLS:
        while True:
            handle = backend.locate_structural(StructuralLocator(automation_id=aid))
            if handle is not None:
                points[aid] = (int(handle.point[0]), int(handle.point[1]))
                break
            assert time.monotonic() < deadline, f"UIA never found {aid!r}"
            time.sleep(0.5)
    return points


@pytest.fixture
def host(tmp_path):
    h = _Host(tmp_path)
    try:
        yield h
    finally:
        h.close()


def test_record_compile_replay_through_windows_backend(host, tmp_path) -> None:
    from openadapt_flow.adapters.desktop_recorder import (
        record_desktop_demo,
        structural_armed_coverage,
    )
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    evidence: dict[str, Any] = {"runner": os.environ.get("RUNNER_NAME")}
    backend = host.start_agent()
    evidence["viewport"] = list(backend.viewport)
    capabilities = backend.agent_capabilities()
    assert {"typed_input_v1", "uia_v1"}.issubset(capabilities)
    assert "legacy_exec" not in capabilities

    host.launch_app()
    points = _points(backend)

    def driver(rec) -> None:
        rec.click(*points["searchBox"])
        rec.type_text(DEMO_SEARCH)
        rec.press("Enter")
        time.sleep(0.4)
        rec.click(*points["searchButton"])
        time.sleep(0.4)
        rec.click(*points["noteBox"])
        rec.type_text(DEMO_NOTE)
        rec.click(*points["saveButton"])
        time.sleep(0.4)

    recording = record_desktop_demo(backend, tmp_path / "recording", driver)
    recorded = {r["id"]: r["note"] for r in host.rows()}
    assert recorded[TARGET_ID] == DEMO_NOTE, "the recorded demonstration did not save"

    bundle = tmp_path / "bundle"
    workflow = compile_recording(recording, bundle, name="patient-notes-hosted")
    coverage = structural_armed_coverage(workflow)
    evidence["coverage"] = coverage
    assert coverage["armed_coverage"] == 1.0, coverage
    assert coverage["click_steps"] == len(CONTROLS), coverage

    outcomes = {}
    for drift in ("none", "siblings"):
        host.launch_app(drift)
        before = {r["id"]: r for r in host.rows()}
        assert all(r["note"] == "" for r in before.values())
        _points(backend)  # the reseeded window is up
        report = Replayer(backend, use_structural=True).run(
            Workflow.load(bundle),
            params={},
            bundle_dir=bundle,
            run_dir=tmp_path / f"run-{drift}",
        )
        after = {r["id"]: r for r in host.rows()}
        changed = sorted(i for i in after if after[i] != before[i])
        stops = [str(r.error) for r in report.results if r.error]
        outcomes[drift] = {
            "success": report.success,
            "rung_counts": report.rung_counts,
            "model_calls": report.model_calls,
            "changed_ids": changed,
            "target_note": after[TARGET_ID]["note"],
            "stop": stops[-1] if stops else None,
        }
        # The record decides, and it must agree with the report: a completed
        # run wrote exactly the demonstrated note to the target and nothing
        # else; a stopped run wrote nothing at all.
        if report.success:
            assert changed == [TARGET_ID], outcomes[drift]
            assert after[TARGET_ID]["note"] == DEMO_NOTE, outcomes[drift]
        else:
            assert changed == [], outcomes[drift]
        assert report.model_calls == 0, outcomes[drift]
    evidence["outcomes"] = outcomes
    # The healthy replay must save; a stop there is a regression.
    assert outcomes["none"]["success"], outcomes["none"]
    assert outcomes["none"]["rung_counts"].get("structural", 0) >= len(CONTROLS)
    # On the sibling roster the search selects a near-identical patient, so no
    # recorded click can reach patient 1: the run must stop at the identity
    # check, not save and not stop for some other reason.
    siblings = outcomes["siblings"]
    assert not siblings["success"], siblings
    assert siblings["stop"] and "Identity check failed" in siblings["stop"], siblings

    if os.environ.get(EVIDENCE_ENV):
        Path(os.environ[EVIDENCE_ENV]).write_text(json.dumps(evidence, indent=2))
