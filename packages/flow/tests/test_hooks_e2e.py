"""End to end: a scripted agent's tool calls through the installed hooks.

A harness plays Claude Code (and Codex): for each tool call it runs the
hook command that ``openadapt hooks install`` wrote, with the documented
stdin JSON, honours the decision, runs the call itself if allowed, then
sends ``PostToolUse``. The systems are local and synthetic: a SQLite file
and a small JSON API on 127.0.0.1. No model, no real agent session.

It shows: an allowed write read back with a signed receipt, a blocked
out-of-scope write, and a write whose read-back fails coming out UNCERTAIN.
"""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest
import yaml

pytest.importorskip("openadapt_types.receipt")
pytest.importorskip("cryptography")

from openadapt_flow.hooks import engine as hooks_engine  # noqa: E402
from openadapt_flow.hooks.install import install, launcher_path  # noqa: E402
from openadapt_flow.receipt_record import local_registry, verify_run_receipt  # noqa: E402

NOTE = "Follow-up in two weeks"


class _Api(BaseHTTPRequestHandler):
    records: list[dict[str, Any]] = []
    broken_reads = False
    break_after_write = False

    def log_message(self, *args: Any) -> None:  # quiet
        pass

    def _send(self, code: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/api/notes":
            return self._send(404, {})
        if type(self).broken_reads:
            return self._send(500, {"error": "read replica down"})
        self._send(200, {"records": list(type(self).records)})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/notes":
            return self._send(404, {})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        record = {"id": len(type(self).records) + 1, **body}
        type(self).records.append(record)
        if type(self).break_after_write:
            type(self).broken_reads = True
        self._send(201, record)


@pytest.fixture
def api() -> Iterator[str]:
    _Api.records = [{"id": 1, "patient_id": "p0", "note": "older note"}]
    _Api.broken_reads = False
    _Api.break_after_write = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@pytest.fixture
def project(tmp_path: Path, api: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.delenv("OPENADAPT_VERIFIER_STATE_DIR", raising=False)
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "contracts").mkdir()
    db = root / "data" / "clinic.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, patient_id TEXT, note TEXT)")
    con.execute("INSERT INTO notes (patient_id, note) VALUES ('p0', 'older note')")
    con.commit()
    con.close()
    effect = {
        "kind": "record_written",
        "match": {"patient_id": {"param": "patient_id"}, "note": {"param": "note"}},
        "expected_count": 1,
        "count_new_only": True,
        "timeout_s": 0.5,
    }
    (root / "contracts" / "note-db.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": "SELECT id, patient_id, note FROM notes",
                },
                "effects": [effect],
            }
        )
    )
    (root / "contracts" / "note-api.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": {
                    "kind": "rest",
                    "base_url": api,
                    "records_path": "/api/notes",
                    "records_key": "records",
                    "records_complete": True,
                    "timeout_s": 2,
                },
                "effects": [effect],
            }
        )
    )
    sql = (
        r"INSERT INTO notes \(patient_id, note\) VALUES "
        r"\('(?P<patient_id>[^']*)', '(?P<note>[^']*)'\)"
    )
    (root / "openadapt.systems.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "openadapt.systems/v1",
                "unknown_write": "block",
                "systems": [
                    {
                        "name": "clinic-db",
                        "kind": "database",
                        "url": "sqlite:///data/clinic.sqlite",
                        "commands": ["sqlite3"],
                        "writes": [
                            {
                                "name": "add-note",
                                "sql": sql,
                                "contract": "contracts/note-db.yaml",
                                "approval": "allow",
                            }
                        ],
                    },
                    {
                        "name": "notes-api",
                        "kind": "http",
                        "base_url": api,
                        "writes": [
                            {
                                "name": "add-note",
                                "method": "POST",
                                "path": "/api/notes",
                                "contract": "contracts/note-api.yaml",
                                "approval": "ask",
                            }
                        ],
                    },
                ],
            }
        )
    )
    return root


def _run_tool(root: Path, command: str) -> dict[str, Any]:
    """Stand in for the agent's shell: run a sqlite3 or curl command."""
    words = shlex.split(command)
    if words[0] == "sqlite3":
        con = sqlite3.connect(root / words[1])
        try:
            con.executescript(words[2])
            con.commit()
        finally:
            con.close()
        return {"stdout": "", "stderr": "", "interrupted": False}
    if words[0] == "curl":
        method = words[words.index("-X") + 1]
        body = words[words.index("-d") + 1].encode()
        url = [w for w in words if w.startswith("http")][0]
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"stdout": resp.read().decode(), "stderr": "", "interrupted": False}
    raise AssertionError(f"the harness doesn't run {words[0]}")


class Harness:
    """Plays one agent session against the installed hook command."""

    def __init__(self, root: Path, agent: str, session: str = "session-1") -> None:
        self.root = root
        self.agent = agent
        self.session = session
        self.n = 0

    def hook(self, event: str, extra: dict[str, Any]) -> tuple[int, Optional[dict], str]:
        payload = {
            "session_id": self.session,
            "transcript_path": str(self.root / "t.jsonl"),
            "cwd": str(self.root),
            "permission_mode": "default",
            "hook_event_name": event,
            **extra,
        }
        if self.agent == "codex":
            payload.update({"model": "test-model", "turn_id": "turn-1"})
        proc = subprocess.run(
            [str(launcher_path(self.root)), self.agent, event],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=self.root,
            timeout=120,
        )
        doc = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc.returncode, doc, proc.stderr

    def call(self, command: str, *, person_says: str = "yes", post: bool = True) -> dict:
        self.n += 1
        tool = {
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "test"},
            "tool_use_id": f"toolu_{self.n:04d}",
        }
        code, doc, err = self.hook("PreToolUse", tool)
        assert code == 0, err
        decision = (doc or {}).get("hookSpecificOutput", {}).get("permissionDecision")
        reason = (doc or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
        out: dict[str, Any] = {"decision": decision or "pass", "reason": reason}
        if decision == "deny" or (decision == "ask" and person_says != "yes"):
            return out
        if not post:
            return out
        response = _run_tool(self.root, command)
        code, doc, err = self.hook("PostToolUse", {**tool, "tool_response": response})
        assert code == 0, err
        out["post"] = doc or {}
        out["context"] = (doc or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        return out

    def stop(self) -> Optional[dict]:
        code, doc, err = self.hook(
            "Stop", {"stop_hook_active": False, "last_assistant_message": "done"}
        )
        assert code == 0, err
        return doc


def _receipt(root: Path, context: str) -> tuple[Path, Any]:
    rel = context.split("Receipt: ", 1)[1].rstrip(".").split(" ")[0]
    run_dir = (root / rel).parent
    verified = verify_run_receipt(run_dir, local_registry())
    return run_dir, verified


def _rows(root: Path) -> list[tuple]:
    con = sqlite3.connect(root / "data" / "clinic.sqlite")
    try:
        return con.execute("SELECT patient_id, note FROM notes ORDER BY id").fetchall()
    finally:
        con.close()


def test_claude_code_allowed_blocked_and_uncertain(project: Path, api: str) -> None:
    install(project, "claude-code")
    agent = Harness(project, "claude-code")

    # 1. An allowed, declared write: read back, VERIFIED, signed receipt.
    ok = agent.call(
        f"sqlite3 data/clinic.sqlite \"INSERT INTO notes (patient_id, note) "
        f"VALUES ('p1', '{NOTE}')\""
    )
    assert ok["decision"] == "pass"  # in scope: Claude Code's own prompt still applies
    assert "VERIFIED" in ok["context"], ok
    assert "decision" not in ok["post"]
    run_dir, verified = _receipt(project, ok["context"])
    assert verified.ok, verified
    assert verified.outcome == "VERIFIED"
    assert verified.signer_lines == ["Signed by this computer"]
    report = json.loads((run_dir / "report.json").read_text())
    assert report["outcome"] == "VERIFIED" and report["checked"]["delta"] == "held"
    # The receipt names no value, path or URL from the call.
    text = (run_dir / "receipt.json").read_text()
    assert NOTE not in text and "clinic" not in text and "127.0.0.1" not in text

    # 2. An out-of-scope write to the declared database: blocked, nothing sent.
    before = _rows(project)
    blocked = agent.call('sqlite3 data/clinic.sqlite "DELETE FROM notes"')
    assert blocked["decision"] == "deny"
    assert "no such write" in blocked["reason"]
    assert _rows(project) == before
    run_dir, verified = _receipt(project, blocked["reason"])
    assert verified.ok and verified.outcome == "STOPPED"

    # Reads pass through unchecked; the agent can't edit its own scope.
    assert agent.call('sqlite3 data/clinic.sqlite "SELECT count(*) FROM notes"', post=False)[
        "decision"
    ] == "pass"
    assert agent.call("openadapt hooks approve hk-0000", post=False)["decision"] == "deny"
    assert agent.call("echo x >> openadapt.systems.yaml", post=False)["decision"] == "deny"

    # 3. A write whose read-back fails: UNCERTAIN, never a pass, and held.
    _Api.break_after_write = True
    body = json.dumps({"patient_id": "p2", "note": NOTE})
    command = f"curl -sS -X POST {api}/api/notes -H 'Content-Type: application/json' -d '{body}'"
    uncertain = agent.call(command)
    assert uncertain["decision"] == "ask"  # approval: ask; the person said yes
    assert "UNCERTAIN" in uncertain["context"], uncertain
    assert uncertain["post"]["decision"] == "block"  # flagged to the agent
    assert len(_Api.records) == 2  # the write did land; the read-back failed
    run_dir, verified = _receipt(project, uncertain["context"])
    assert verified.ok and verified.outcome == "UNKNOWN"
    retry = agent.call(command, post=False)
    assert retry["decision"] == "deny" and "UNCERTAIN" in retry["reason"]

    stop = agent.stop()
    assert stop and "1 VERIFIED" in stop["systemMessage"]
    assert "1 UNCERTAIN" in stop["systemMessage"]

    status = hooks_engine.summary(project)
    assert [h["label"] for h in status["held"]] == ["UNCERTAIN"]
    assert sorted(r["outcome"] for r in status["receipts"]) == [
        "BLOCKED",
        "UNCERTAIN",
        "VERIFIED",
    ]


def test_codex_asks_by_denying_until_a_person_approves(project: Path, api: str) -> None:
    install(project, "codex")
    assert (project / ".codex" / "hooks.json").is_file()
    agent = Harness(project, "codex")
    body = json.dumps({"patient_id": "p3", "note": NOTE})
    command = f"curl -sS -X POST {api}/api/notes -H 'Content-Type: application/json' -d '{body}'"

    first = agent.call(command)
    assert first["decision"] == "deny"  # Codex runs the tool on "ask"; never sent
    assert len(_Api.records) == 1
    request = first["reason"].split("openadapt hooks approve ", 1)[1].split("`")[0]
    print(hooks_engine.approve(project, request))

    second = agent.call(command)
    assert second["decision"] == "pass"
    assert "VERIFIED" in second["context"], second
    _, verified = _receipt(project, second["context"])
    assert verified.ok and verified.outcome == "VERIFIED"
    # The approval was for one call only.
    assert agent.call(command, post=False)["decision"] == "deny"


def test_a_write_with_no_post_event_is_read_back_at_stop(project: Path) -> None:
    install(project, "claude-code")
    agent = Harness(project, "claude-code", session="session-2")
    command = (
        "sqlite3 data/clinic.sqlite \"INSERT INTO notes (patient_id, note) "
        "VALUES ('p4', 'never sent')\""
    )
    assert agent.call(command, post=False)["decision"] == "pass"
    stop = agent.stop()  # the call never ran: nothing to read back
    assert stop and "1 UNCERTAIN" in stop["systemMessage"]
    assert hooks_engine.summary(project)["held"][0]["label"] == "UNCERTAIN"


def test_a_broken_hook_blocks_the_call(project: Path) -> None:
    install(project, "claude-code", python=str(project / "no-such-python"))
    agent = Harness(project, "claude-code")
    code, doc, err = agent.hook(
        "PreToolUse",
        {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "t1"},
    )
    assert code == 2 and doc is None
    assert "failed to run" in err


@pytest.fixture(autouse=True)
def _child_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # The launcher runs a fresh interpreter: give it this test's import path.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in sys.path if p))
