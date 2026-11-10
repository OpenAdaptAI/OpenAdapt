"""The MCP teach tools on a native desktop app, through the real backend.

The app is ``tests/native_apps/Chart.swift``: a Cocoa window with a patient
banner, a "New note" button, a note field and a "Save note" button that
inserts one row into a SQLite ``notes`` table. ``MacOSBackend`` drives it (AX
reads, OS input, window screenshots); nothing is faked. A scripted MCP client
plays the agent: it reads the controls from ``teach_observe`` when the backend
lists them, or reads the screen (``teach_screen`` and OCR) and clicks x,y.

What it checks, end to end over MCP:

* without a person's form nothing is clicked (a desktop save can't be checked
  for the right record from the screen alone, so a person approves it);
* with a person, run 1 saves one row, compiles, passes its record check
  against the database, and a person confirms the check;
* the agent admits the draft under a signed policy (real canary saves through
  the compiled program), and the ``taught_<name>`` tool saves a new note with
  no model calls, verified in the database.

Opt-in: ``OPENADAPT_TEST_MACOS_AX=1`` on a Mac with Accessibility and Screen
Recording access and ``swiftc`` (the macOS lab VM; never the founder's Mac).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Optional

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin"
    or os.environ.get("OPENADAPT_TEST_MACOS_AX") != "1"
    or shutil.which("swiftc") is None
    or find_spec("mcp") is None,
    reason="needs OPENADAPT_TEST_MACOS_AX=1, macOS with AX access, swiftc and mcp",
)

APP_SOURCE = Path(__file__).resolve().parents[1] / "native_apps" / "Chart.swift"
TITLE = "Chart"
# No 0/O/1/l/I in the MRN: OCR can't tell those glyphs apart.
PATIENT = ("rc79284", "MRN RC79284 Okafor, Philip born 1966-02-17")
NOTE = "FOLLOW UP IN TWO WEEKS"
CANARY = "CANARY CHECK NOTE"
NEW_NOTE = "FOLLOW UP IN SIX WEEKS"
AGENT_ID = "agent:mcp-teach-desktop"
#: A blinking caret changes the frame between a run's lease and the save
#: click, which the run then refuses (it never clicks on an unsettled frame).
#: The app's AppKit defaults hold the caret steady, as an operator can.
STEADY_CARET = [
    "-NSTextInsertionPointBlinkPeriodOn",
    "100000000",
    "-NSTextInsertionPointBlinkPeriodOff",
    "0",
]
SECTION = {"kind": "macos", "macos_app": "chart", "macos_window_title": TITLE}


class ChartApp:
    """The chart app, relaunched before every run (the taught program starts
    from a fresh window)."""

    def __init__(self, binary: Path, db: Path) -> None:
        self.binary, self.db = binary, db
        self.proc: Optional[subprocess.Popen] = None

    def reset(self) -> None:
        self.close()
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("OPENADAPT_", "OAFLOW_"))
        }
        self.proc = subprocess.Popen(
            [str(self.binary), str(self.db), TITLE, *PATIENT, "plain", *STEADY_CARET],
            env=env,
        )
        from openadapt_flow.backends.remote_display import MacWindowClient

        client = MacWindowClient()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if client.find_windows("chart", TITLE):
                break
            time.sleep(0.2)
        time.sleep(0.5)

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
            # macOS re-activates the previous app a moment after a kill.
            time.sleep(1.5)
        self.proc = None


def _rows(db: Path) -> list[tuple[str, str]]:
    with sqlite3.connect(db) as conn:
        return [tuple(r) for r in conn.execute("SELECT patient_id, note FROM notes")]


def _admitter(key_dir: Path, origin: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    from openadapt_flow.jit import authority

    monkeypatch.setenv(authority.KEY_PASSPHRASE_ENV, "desktop passphrase")
    files = key_dir.parent / "keyfiles"
    for role, pid, name in (
        ("policy_signer", "dana.signer", "signer"),
        ("agent", AGENT_ID, "agent"),
    ):
        authority.register_principal(
            key_dir,
            role=role,
            principal_id=pid,
            public_key_hex=authority.generate_encrypted_key(files / f"{name}.pem"),
        )
    now = datetime.now(timezone.utc)
    policy = authority.AdmissionPolicy(
        policy_id="desktop-chart",
        signer="dana.signer",
        signed_at=(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        allowed_hosts=(origin,),
        allowed_actions=("click", "type"),
        allowed_effects=("record_written", "field_equals"),
        min_clean_canaries=1,
        agent_principals=(AGENT_ID,),
        allow_self_admission=True,
    )
    signed = authority.sign_policy(
        policy, authority.load_encrypted_key(files / "signer.pem")
    )
    return authority.principal_from_key(
        authority.load_encrypted_key(files / "agent.pem"), key_dir, policy=signed
    )


@asynccontextmanager
async def connect(server: Any, *, person: Optional[str]):
    import anyio
    import mcp.types as types
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    async def elicit(context: Any, params: Any) -> types.ElicitResult:
        return types.ElicitResult(
            action="accept", content={"name": person, "confirmed": True}
        )

    async with create_client_server_memory_streams() as (client, transport):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server.run, *transport, server.create_initialization_options()
            )
            kwargs = {"elicitation_callback": elicit} if person else {}
            async with ClientSession(*client, **kwargs) as session:
                await session.initialize()
                yield session
            tasks.cancel_scope.cancel()


async def _call(session: Any, name: str, args: Optional[dict] = None) -> dict:
    result = await session.call_tool(name, args or {})
    payload = json.loads(result.content[0].text)
    if getattr(result, "is_error", None) or getattr(result, "isError", None):
        return {"_error": payload.get("error")}
    return payload


def _squash(text: str) -> str:
    return "".join(text.split()).casefold().replace("0", "o")


class Eyes:
    """How the scripted agent finds a control: by its ref when the backend
    lists controls, else by reading the screen (OCR), as an agent would."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.used: list[str] = []

    async def click(self, label: str) -> dict:
        observed = await _call(self.session, "teach_observe")
        for line in str(observed.get("observation", "")).splitlines():
            if f'"{label}"' in line and line.startswith("[") and "]" in line:
                self.used.append("ref")
                ref = line[1 : line.index("]")]
                return await _call(
                    self.session, "teach_act", {"action": "click", "ref": ref}
                )
        import base64

        from openadapt_flow.vision import ocr

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            shot = await _call(self.session, "teach_screen")
            png = base64.b64decode(shot["screen_png_base64"])
            for line in ocr(png):
                if _squash(line.text) == _squash(label):
                    x, y, w, h = line.region
                    self.used.append("screen")
                    return await _call(
                        self.session,
                        "teach_act",
                        {"action": "click", "x": int(x + w // 2), "y": int(y + h // 2)},
                    )
            time.sleep(0.3)
        raise AssertionError(f"{label!r} is neither listed nor on the screen")


@pytest.mark.timeout(900)
def test_teach_and_call_on_a_macos_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anyio

    from openadapt_flow.agent.bridge import AgentBridge
    from openadapt_flow.agent.mcp import build_server
    from openadapt_flow.agent.runner import RunnerConfig
    from openadapt_flow.agent.teach import TeachBridge, TeachConfig
    from openadapt_flow.jit.compile_agent import SurfaceApp
    from tests._teach_authorization_support import authority

    binary = tmp_path / "chart"
    subprocess.run(
        ["swiftc", "-O", "-o", str(binary), str(APP_SOURCE), "-lsqlite3"],
        check=True,
        timeout=600,
    )
    db = tmp_path / "chart.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, patient_id TEXT, note TEXT)"
        )
    app = ChartApp(binary, db)
    verify = tmp_path / "verify.yaml"
    verify.write_text(
        "verifier:\n  kind: sql\n"
        f"  sqlite_database: {db}\n"
        "  sql_query: SELECT id, patient_id, note FROM notes\n"
    )
    runs = tmp_path / "runs"
    runs.mkdir()
    origin = SurfaceApp(SECTION).origin
    workflows = AgentBridge(
        None, RunnerConfig(runs_dir=runs), allow_run=True, mode="production"
    )
    teach = TeachBridge(
        TeachConfig(
            verify=verify,
            cache_dir=tmp_path / "teach",
            surface=SECTION,
            key_dir=tmp_path / "keys",
            admitter=_admitter(tmp_path / "keys", origin, monkeypatch),
            screens="unscrubbed",
            reset=app.reset,
            # The app's notes carry no duplicate key, so clinical-write (which
            # requires one on every save) refuses it by design. The standard
            # run gate still requires armed identity on every click, the
            # record check, and a verified effect on every run.
            policy="permissive",
            teach_authorization=authority(
                tmp_path / "keys",
                tmp_path / "teach-authorization",
                "taught.chart_note",
                monkeypatch,
                origins=[origin],
                record={"patient_id": PATIENT[0]},
                fields={"note": NOTE},
                inputs={"note": NOTE},
                constants=[PATIENT[1]],
                record_kind="note",
            ),
        ),
        workflows=workflows,
    )
    server = build_server(workflows, teach=teach)
    start = {
        "name": "chart_note",
        "task": "Add this note to the open chart: {note}",
        "inputs": {"note": NOTE},
        "record_fields": {"patient_id": PATIENT[0]},
        "on_screen": [PATIENT[1]],
    }
    evidence: dict[str, Any] = {}

    async def scenario() -> None:
        # No form: the first click needs a person, so nothing is clicked.
        async with connect(server, person=None) as session:
            started = await _call(session, "teach_start", start)
            assert started.get("surface") == "macos", started
            assert started["person_approves_saves"]
            refused = await Eyes(session).click("New note")
            assert "needs a person's approval" in refused["_error"], refused
            assert (await _call(session, "teach_abort"))["status"] == "aborted"
        assert _rows(db) == []

        async with connect(server, person="dana.reviewer") as session:
            started = await _call(session, "teach_start", start)
            assert started.get("status") == "recording", started
            eyes = Eyes(session)
            for label in ("New note", "Note"):
                done = await eyes.click(label)
                assert done.get("status") == "ok", done
            typed = await _call(
                session, "teach_act", {"action": "type", "input": "note"}
            )
            assert typed.get("status") == "ok", typed
            saved = await eyes.click("Save note")
            assert saved.get("status") == "ok", saved
            assert _rows(db) == [(PATIENT[0], NOTE)]
            draft = await _call(session, "teach_finish")
            assert draft.get("status") == "draft", draft
            assert draft["surface"] == "macos"
            assert draft["record_check"]["passed"], draft
            evidence["draft_parameters"] = draft["parameters"]

            confirmed = await _call(
                session,
                "teach_confirm_checks",
                {"task_key": draft["task_key"], "drop_fields": ["id"]},
            )
            assert confirmed.get("confirmed_by") == "dana.reviewer", confirmed
            admitted = await _call(
                session,
                "teach_admit",
                {"task_key": draft["task_key"], "canary_inputs": [{"note": CANARY}]},
            )
            evidence["admit"] = admitted
            assert admitted.get("status") == "admitted", admitted

            tools = {t.name for t in (await session.list_tools()).tools}
            (tool,) = [t for t in tools if t.startswith("taught_")]
            ran = await _call(
                session,
                tool,
                {"inputs": {"note": NEW_NOTE}, "request_id": "desktop-teach-0001"},
            )
            evidence["taught_run"] = ran
            assert ran.get("outcome") == "done" and ran.get("model_calls") == 0, ran
            evidence["found_controls_by"] = sorted(set(eyes.used))
        assert (PATIENT[0], NEW_NOTE) in _rows(db)
        assert _rows(db).count((PATIENT[0], NEW_NOTE)) == 1

    try:
        app.reset()
        anyio.run(scenario)
    finally:
        app.close()
        out = os.environ.get("OPENADAPT_TEACH_DESKTOP_EVIDENCE")
        if out:
            evidence["rows"] = _rows(db)
            Path(out).write_text(json.dumps(evidence, indent=2, default=str))
