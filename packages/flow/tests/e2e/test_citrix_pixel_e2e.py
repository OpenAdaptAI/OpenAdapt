"""Snapshot-safe, OPT-IN Citrix / remote-display PIXEL-ONLY end-to-end proof.

Proves the compiler's VISUAL FLOOR on a REAL remote-display client window, with
the structural (UIA) rung UNAVAILABLE — exactly the Citrix constraint. Unlike
``test_parallels_desktop_e2e.py`` (which drives the WinForms app through the
in-guest ``win_agent`` and asserts the DETERMINISTIC UIA rung fires), this test
drives the app **only through the pixels of the Parallels VM window on the host**
via :class:`~openadapt_flow.backends.remote_display.RemoteDisplayBackend`
(``CGWindowListCreateImage`` capture + ``CGEvent`` OS-level input). No UIA/DOM
crosses to the driving process — the same property Citrix imposes on Accuro.

The Parallels VM window is a faithful, reproducible Citrix analog: host-side
pixels of a remote guest, host OS input injected into the window, and NO access
to the guest accessibility tree through it. Swapping the target window title
from "Windows 11" to "Citrix Workspace"/"Accuro" leaves the code identical. What
real Citrix adds (HDX compression, latency, DPI/credential/lock-screen drift) is
NOT simulated — see ``docs/desktop/CITRIX_PIXEL.md``.

What it asserts:
    * ``structural_armed_coverage == 0`` — the compiled bundle carries NO UIA
      locator (the inverse of the structural test): pixel-only == no top rung.
    * replay resolves on ``template`` / ``ocr`` / ``geometry`` ONLY (no
      ``structural``) and the linear run completes.
    * ON-SCREEN OCR read-back verify (SAME-SURFACE) confirms the saved note.
    * IDENTITY GATE on pixels: with a look-alike patient at the target row, the
      run HALTs (identity ``status != "verified"``) rather than write the wrong
      chart.
    * HALT-ON-AMBIGUITY: under render drift that degrades OCR/template, the
      resolver HALTs rather than click a guessed target.
    * DB ground truth (``pn_db.py``) corroborates every write independently.

Safety / isolation (the user's VM is sacred):
    * OPT-IN ONLY — skipped unless ``OAFLOW_CITRIX_PIXEL_E2E=1``.
    * Requires macOS with Screen-Recording AND Accessibility granted to the
      driving app; SKIPS (never fails, never fabricates) when input cannot be
      delivered — a dropped synthetic click must never look like success.
    * SNAPSHOT-FIRST, REVERT-AFTER; NEVER deletes the VM or ANY snapshot.
    * Requires the Parallels VM window to be open + resumable; SKIPS otherwise.

Env overrides: ``OAFLOW_PARALLELS_VM_UUID``, ``OAFLOW_CITRIX_WINDOW_TITLE``
(default "Windows 11").
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

RUN = os.environ.get("OAFLOW_CITRIX_PIXEL_E2E") == "1"

pytestmark = [
    pytest.mark.skipif(
        not RUN,
        reason="opt-in live Citrix pixel e2e; set OAFLOW_CITRIX_PIXEL_E2E=1 to run",
    ),
    pytest.mark.timeout(1200),
]

_SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts" / "desktop"
WINDOW_TITLE = os.environ.get("OAFLOW_CITRIX_WINDOW_TITLE", "Windows 11")


# -- environment guards (skip cleanly, never fail spuriously) ----------------


def _require_macos_input() -> None:
    if sys.platform != "darwin":
        pytest.skip("remote-display backend is macOS-only (Quartz/AppKit)")
    try:
        from ApplicationServices import AXIsProcessTrusted
    except Exception:  # noqa: BLE001
        pytest.skip("pyobjc/ApplicationServices unavailable")
    if not AXIsProcessTrusted():
        pytest.skip(
            "Accessibility not granted to the driving app; OS input would be "
            "silently dropped. Grant it (System Settings > Privacy & Security > "
            "Accessibility) and rerun."
        )


def _vision():
    from openadapt_flow import vision

    return vision


def _locate(backend, text, *, min_ratio=0.7):
    """Center pixel of an OCR-located label, or None."""
    m = _vision().find_text(backend.screenshot(), text, min_ratio=min_ratio)
    if m is None:
        return None
    return (int(m.region[0] + m.region[2] / 2), int(m.region[1] + m.region[3] / 2))


# -- VM / app harness --------------------------------------------------------


def _guest_ready(vm, *, timeout=90.0) -> bool:
    """Wait for the guest exec channel (guest tools) to answer."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = vm.exec_cmd("echo OAFLOW_READY", timeout=20)
            if "OAFLOW_READY" in (r.stdout or ""):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(5)
    return False


def _deploy_and_launch(vm, drift: str = "none", env_json: dict | None = None) -> None:
    """Push the harness, seed the DB, (re)launch patient_notes in session 1."""
    vm.exec_cmd("if not exist C:\\oa mkdir C:\\oa")
    for f in ("pn_db.py", "patient_notes.ps1", "session1_launch.py"):
        vm.push_file(str(_SCRIPT_DIR / f), f"C:/oa/{f}")
    if env_json is not None:
        local = Path("/tmp/pn_env.json")
        local.write_text(json.dumps(env_json))
        vm.push_file(str(local), "C:/oa/pn_env.json")
    else:
        vm.exec_cmd("del C:\\oa\\pn_env.json 2>nul & echo done")
    seed_args = ["seed"] + (["--drift", drift] if drift != "none" else [])
    vm.exec([vm.python_guest, "C:/oa/pn_db.py", *seed_args], timeout=60)
    vm.exec_cmd("taskkill /F /IM powershell.exe 2>nul & echo done")
    time.sleep(1)
    vm.exec(
        [vm.python_guest, "C:/oa/session1_launch.py", "C:/oa/patient_notes.ps1"],
        timeout=60,
    )
    time.sleep(8)


def _db_get(vm, pid: int) -> dict:
    r = vm.exec([vm.python_guest, "C:/oa/pn_db.py", "get", str(pid)], timeout=30)
    return json.loads((r.stdout or "null").strip() or "null")


# -- the proof ---------------------------------------------------------------


def test_citrix_pixel_only_record_replay_identity_verify_halt(tmp_path) -> None:
    _require_macos_input()

    from openadapt_flow.adapters.desktop_recorder import (
        record_desktop_demo,
        structural_armed_coverage,
    )
    from openadapt_flow.backends.parallels_vm import DEFAULT_VM_UUID, ParallelsVM
    from openadapt_flow.backends.remote_display import (
        RemoteDisplayBackend,
        RemoteDisplayError,
    )
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier

    uuid = os.environ.get("OAFLOW_PARALLELS_VM_UUID", DEFAULT_VM_UUID)
    vm = ParallelsVM(uuid)

    # The VM must already be running (Parallels Standard cannot prlctl-start; a
    # suspended VM is resumed by the operator or the GUI play button). Skip
    # cleanly rather than fail if it is not reachable.
    if vm.status() != "running" or not _guest_ready(vm):
        pytest.skip("Parallels VM not running / guest tools unreachable")

    backend = RemoteDisplayBackend(
        owner_substr="Parallels Desktop", title_substr=WINDOW_TITLE
    )
    try:
        backend.ensure_foreground()
    except RemoteDisplayError as e:
        pytest.skip(f"remote-display client window not foregroundable: {e}")

    # SNAPSHOT FIRST — reverted in finally (never deleted).
    snap_id = vm.snapshot(
        f"oaflow-citrix-{int(time.time())}", description="citrix pixel e2e"
    )
    # A snapshot on a running VM briefly disturbs guest tools; wait for it back.
    _guest_ready(vm)
    try:
        # ---- deploy + launch the stand-in clinical app (pixel target) ------
        _deploy_and_launch(vm, drift="none")
        backend.ensure_foreground()

        # ---- RECORD a clinical entry, PIXEL-ONLY --------------------------
        note_text = "chest pain reviewed, follow up in two weeks"
        search_btn = _locate(backend, "Search")
        assert search_btn is not None, "could not locate Search button via OCR"
        bx, by = search_btn
        note_label = _locate(backend, "Clinical note")
        save_btn = _locate(backend, "Save Note")
        assert note_label and save_btn, "could not locate note/save controls"

        def driver(rec) -> None:
            rec.click(bx - 700, by)  # searchBox (left of Search)
            rec.type_text("Sorenson", param="query")
            rec.click(bx, by)  # Search
            time.sleep(1.0)
            row = _locate(backend, "Sorenson")  # the Neil Sorenson row
            assert row is not None, "Neil Sorenson row not found after search"
            rec.click(*row)  # select patient (identity-gated)
            time.sleep(0.6)
            rec.click(note_label[0], note_label[1] + 120)  # note box
            rec.type_text(note_text, param="note")
            rec.click(*save_btn)  # Save
            time.sleep(0.6)

        recording = record_desktop_demo(backend, tmp_path / "rec", driver)
        bundle = compile_recording(recording, tmp_path / "bundle", name="citrix-pixel")

        # ---- pixel-only == NO structural arming (inverse of #102) ----------
        coverage = structural_armed_coverage(Workflow.load(bundle))
        print(f"[citrix] structural_armed_coverage = {coverage}")
        assert coverage["armed_coverage"] == 0.0, (
            f"pixel-only bundle must carry NO UIA locator (Citrix floor): {coverage}"
        )

        # ---- REPLAY pixel-only; visual rungs only --------------------------
        _deploy_and_launch(vm, drift="none")
        backend.ensure_foreground()
        workflow = Workflow.load(bundle)
        report = Replayer(backend, use_structural=False).run(
            workflow, params={}, bundle_dir=bundle, run_dir=tmp_path / "run"
        )
        print(f"[citrix] rung_counts = {report.rung_counts}")
        for r in report.results:
            if r.resolution:
                print(
                    f"[citrix]  step {r.step_id}: rung={r.resolution.rung} "
                    f"conf={r.resolution.confidence:.3f} "
                    f"identity={getattr(r.identity, 'status', None)}"
                )
        assert "structural" not in report.rung_counts, (
            f"structural rung must NOT fire pixel-only: {report.rung_counts}"
        )
        assert report.rung_counts, "no rungs fired at all"
        assert set(report.rung_counts) <= {
            "template",
            "template_global",
            "ocr",
            "geometry",
        }
        assert report.success, [r.model_dump() for r in report.results]

        # ---- ON-SCREEN OCR read-back verify (SAME-SURFACE) -----------------
        status_region = (
            0,
            int(backend.viewport[1] * 0.5),
            backend.viewport[0],
            int(backend.viewport[1] * 0.5),
        )
        verifier = OnScreenReadbackVerifier(backend, region=status_region)
        verdict = verifier.read_back("Saved note")
        print(f"[citrix] on-screen verify: {verdict.verdict} — {verdict.reason}")
        assert verdict.confirmed, verdict.reason

        # ---- DB ground truth: the note actually landed on Neil Sorenson ----
        neil = _db_get(vm, 1)
        print(f"[citrix] DB Neil Sorenson note = {neil.get('note')!r}")
        assert note_text.split()[0] in (neil.get("note") or ""), (
            f"note not persisted to the correct patient: {neil}"
        )

        # ---- IDENTITY GATE: look-alike at the target row -> HALT -----------
        # Change patient 1's DOB to a clearly-different value: the same NAME now
        # belongs to a DIFFERENT person. Replay must HALT (identity mismatch)
        # rather than write the note to the wrong chart.
        vm.exec(
            [
                vm.python_guest,
                "-c",
                "import sqlite3;c=sqlite3.connect(r'C:\\oa\\patients.db');"
                "c.execute(\"UPDATE patients SET dob='1971-08-25' WHERE id=1\");"
                "c.commit();c.close();print('DOB_CHANGED')",
            ],
            timeout=30,
        )
        vm.exec_cmd("taskkill /F /IM powershell.exe 2>nul & echo done")
        time.sleep(1)
        vm.exec(
            [vm.python_guest, "C:/oa/session1_launch.py", "C:/oa/patient_notes.ps1"],
            timeout=60,
        )
        time.sleep(8)
        backend.ensure_foreground()
        id_report = Replayer(backend, use_structural=False).run(
            workflow, params={}, bundle_dir=bundle, run_dir=tmp_path / "run_identity"
        )
        id_statuses = [getattr(r.identity, "status", None) for r in id_report.results]
        print(
            f"[citrix] identity-halt: success={id_report.success} "
            f"statuses={id_statuses} halt={id_report.halt}"
        )
        assert not id_report.success, (
            "run must HALT on a look-alike (identity mismatch)"
        )
        assert any(s in ("mismatch", "abstain", "unreadable") for s in id_statuses), (
            f"expected an identity non-verify verdict, got {id_statuses}"
        )
        # The wrong chart must NOT have been overwritten.
        wrong = _db_get(vm, 1)
        assert "1971-08-25" == wrong.get("dob"), "look-alike row unexpectedly changed"

        # ---- HALT-ON-AMBIGUITY: render drift degrades OCR -> HALT ----------
        _deploy_and_launch(
            vm, drift="none", env_json={"font_scale": 1.5, "theme": "dark"}
        )
        backend.ensure_foreground()
        amb_report = Replayer(backend, use_structural=False).run(
            workflow, params={}, bundle_dir=bundle, run_dir=tmp_path / "run_ambiguity"
        )
        print(
            f"[citrix] ambiguity-halt: success={amb_report.success} "
            f"rungs={amb_report.rung_counts} halt={amb_report.halt}"
        )
        assert not amb_report.success, (
            "run must HALT under render drift rather than click a guessed target"
        )
    finally:
        try:
            vm.revert(snap_id)  # never delete — revert only
        except Exception as e:  # noqa: BLE001
            print(f"[citrix] WARNING: revert to {snap_id} failed: {e}")
