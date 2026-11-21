"""End to end: a scripted agent hits a code prompt, asks through MCP, a
person answers, and the agent goes on or stops.

The app is a synthetic two-step site on 127.0.0.1: a one-time-code page
until the session is verified, then a form whose saves the app keeps in
memory (read back from ``/api/records``). The "agent" is a script that reads
each page as HTML, runs the exception detectors on it, and talks to
``openadapt-flow exceptions serve`` over real MCP stdio in a subprocess. The
"person" types the code into the app directly (as in the live browser),
then answers with ``openadapt-flow exceptions answer`` in another
subprocess, signed with a synthetic approver key on a fresh roster. When the
monorepo's Desktop package is on disk, Desktop's stop queue is checked to
show the same card.

Asserted: the agent writes once after ``done_by_person`` and zero times
after a decline or an expiry (read from the app's own state), both answer
timestamps are present, and no planted identifier is in the registry, the
MCP result or Desktop's card.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

pytest.importorskip("mcp")
import anyio  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from openadapt_flow.approval_mode.approver_roster import (  # noqa: E402
    ROOT_ENV,
    file_key_signer,
    init_root,
)
from openadapt_flow.exceptions.detectors import Observation, detect, elements_from_html  # noqa: E402
from openadapt_flow.jit.authority import public_hex  # noqa: E402

PLANTED = ("Maribel Quintanilla", "1961-04-09", "88812345")
SUMMARY = "The sign-in for Maribel Quintanilla (1961-04-09, MRN 88812345) wants a code"
CODE = "482913"  # synthetic; the person types it into the app, never through OpenAdapt
PASSPHRASE = "synthetic-e2e-passphrase"
REPO = Path(__file__).resolve().parents[4]
DESKTOP = REPO / "packages" / "desktop"

CODE_PAGE = """<!doctype html><html><body><h1>Check your phone</h1>
<form method="post" action="/verify"><label for="otc">Verification code</label>
<input id="otc" name="otc" autocomplete="one-time-code" inputmode="numeric" maxlength="6">
<button>Verify</button></form></body></html>"""
FORM_PAGE = """<!doctype html><html><body><h1>New note</h1>
<form method="post" action="/save"><label for="note">Note</label>
<input id="note" name="note" type="text"><button>Save</button></form></body></html>"""


class SyntheticApp:
    """A code gate, then a form. State lives here, read back by the test."""

    def __init__(self) -> None:
        self.verified = False
        self.records: list[dict[str, str]] = []
        self.lock = threading.Lock()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return None

            def _send(self, status: int, body: str, kind: str = "text/html") -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _form(self) -> dict[str, str]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode()
                return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/records":
                    with app.lock:
                        self._send(200, json.dumps(app.records), "application/json")
                    return
                self._send(200, FORM_PAGE if app.verified else CODE_PAGE)

            def do_POST(self) -> None:  # noqa: N802
                form = self._form()
                if self.path == "/verify":
                    app.verified = form.get("otc") == CODE
                    self._send(200, FORM_PAGE if app.verified else CODE_PAGE)
                    return
                if self.path == "/save" and app.verified:
                    with app.lock:
                        app.records.append({"note": form.get("note", "")})
                    self._send(200, "<p>Saved</p>")
                    return
                self._send(403, "<p>Not signed in</p>")

        return Handler


@pytest.fixture
def app():
    state = SyntheticApp()
    server = ThreadingHTTPServer(("127.0.0.1", 0), state.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
    server.shutdown()
    server.server_close()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """A fresh OPENADAPT_HOME, roster and encrypted approver key."""

    key_dir = tmp_path / "keys"
    key = Ed25519PrivateKey.generate()
    init_root(key_dir, principal="front-desk", public_key=public_hex(key), sign=file_key_signer(key))
    pem = tmp_path / "approver.pem"
    pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(PASSPHRASE.encode()),
        )
    )
    pem.chmod(0o600)
    home = tmp_path / "home"
    home.mkdir()
    out = {
        k: v
        for k, v in os.environ.items()
        if k not in {ROOT_ENV, "OPENADAPT_JIT_KEY_DIR", "OPENADAPT_HOME"}
    }
    out.update(
        {
            "OPENADAPT_HOME": str(home),
            "OPENADAPT_JIT_KEY_DIR": str(key_dir),
            ROOT_ENV: public_hex(key),
            "OPENADAPT_JIT_KEY_PASSPHRASE": PASSPHRASE,
            "OPENADAPT_TEST_KEY_PEM": str(pem),
            "HOME": str(tmp_path),
        }
    )
    return out


def _get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - 127.0.0.1
        return response.read().decode()


def _post(url: str, data: dict[str, str]) -> str:
    body = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(url, data=body, timeout=10) as response:  # noqa: S310
        return response.read().decode()


def _registry(env: dict[str, str]) -> Path:
    return Path(env["OPENADAPT_HOME"]) / "exceptions"


def _wait_for_card(env: dict[str, str], timeout: float = 60) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for path in _registry(env).rglob("card-*.json"):
            answer = path.with_name(path.name.replace("card-", "answer-"))
            if not answer.exists():
                return json.loads(path.read_text())
        time.sleep(0.2)
    raise AssertionError("no card appeared")


def _desktop_cards(env: dict[str, str]) -> list[dict[str, Any]] | None:
    """Desktop's stop queue for the registry, when Desktop's source is here."""

    if not (DESKTOP / "openadapt_desktop" / "exception_queue.py").is_file():
        return None
    if str(DESKTOP) not in sys.path:
        sys.path.insert(0, str(DESKTOP))
    from openadapt_desktop import exception_queue, stop_queue

    cards = [stop_queue.card_for_exception(raw) for raw in exception_queue.open_cards(_registry(env))]
    return [c for c in cards if c is not None]


def _answer(env: dict[str, str], card_id: str, choice: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable, "-m", "openadapt_flow", "exceptions", "answer", card_id, choice,
            "--key", env["OPENADAPT_TEST_KEY_PEM"], "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


async def _agent(base: str, env: dict[str, str], deadline_s: float) -> dict[str, Any]:
    """The scripted agent: look, ask when a detector fires, write only if told."""

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "openadapt_flow", "exceptions", "serve",
            "--run-ref", "e2e-run", "--task-id", "note-intake",
            "--deadline-s", str(deadline_s),
        ],
        env=env,
    )
    log: dict[str, Any] = {"asked": [], "wrote": False}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == ["raise_exception"]
            for _step in range(4):
                page = await anyio.to_thread.run_sync(_get, base + "/")
                elements, text = elements_from_html(page)
                kind = detect(Observation(screen_text=text, elements=elements))
                if kind is not None:
                    result = await session.call_tool(
                        "raise_exception", {"kind": kind, "summary": SUMMARY}
                    )
                    body = json.loads(result.content[0].text)
                    log["asked"].append(body)
                    if not body.get("proceed"):
                        return log  # stop before any write
                    continue
                if "Save" in page:
                    await anyio.to_thread.run_sync(_post, base + "/save", {"note": "synthetic note"})
                    log["wrote"] = True
                    return log
    return log


def _run_agent(base: str, env: dict[str, str], deadline_s: float, person: Callable[[], None]):
    result: dict[str, Any] = {}

    def agent() -> None:
        result.update(anyio.run(_agent, base, env, deadline_s))

    thread = threading.Thread(target=agent)
    thread.start()
    person()
    thread.join(timeout=180)
    assert not thread.is_alive(), "the agent never finished"
    return result


def _no_planted(*blobs: bytes) -> None:
    for blob in blobs:
        for planted in PLANTED:
            assert planted.encode() not in blob


def _registry_bytes(env: dict[str, str]) -> bytes:
    return b"".join(p.read_bytes() for p in _registry(env).rglob("*") if p.is_file())


def test_done_by_person_after_a_code_prompt_lets_the_agent_save_once(app, env):
    base, state = app
    seen: dict[str, Any] = {}

    def person() -> None:
        card = _wait_for_card(env)
        seen["card"] = card
        seen["desktop"] = _desktop_cards(env)
        _post(base + "/verify", {"otc": CODE})  # typed in the live app by the person
        done = _answer(env, card["card_id"], "done_by_person")
        assert done.returncode == 0, done.stderr

    log = _run_agent(base, env, 120, person)

    assert seen["card"]["kind"] == "second_factor"
    if seen["desktop"] is not None:
        (desktop_card,) = seen["desktop"]
        assert desktop_card["exception_kind"] == "second_factor"
        assert desktop_card["source"]["id"] == seen["card"]["card_id"]
        assert [c["id"] for c in desktop_card["choices"]] == ["done_by_person", "skip", "abort", "leave"]
        _no_planted(json.dumps(desktop_card).encode())
    (asked,) = log["asked"]
    assert asked["answer"] == "done_by_person" and asked["proceed"] is True
    assert asked["opened_at"] and asked["decided_at"]
    assert log["wrote"] is True
    assert json.loads(_get(base + "/api/records")) == [{"note": "synthetic note"}]
    answer = json.loads(next(_registry(env).rglob("answer-*.json")).read_text())
    assert answer["opened_at"] and answer["decided_at"]
    _no_planted(_registry_bytes(env), json.dumps(log).encode())
    assert CODE.encode() not in _registry_bytes(env)


@pytest.mark.parametrize("choice", ["abort", "skip"])
def test_a_stop_answer_means_no_write(app, env, choice):
    base, state = app

    def person() -> None:
        card = _wait_for_card(env)
        done = _answer(env, card["card_id"], choice)
        assert done.returncode == 0, done.stderr

    log = _run_agent(base, env, 120, person)

    (asked,) = log["asked"]
    assert asked["proceed"] is False and asked["answer"] == choice
    assert asked["opened_at"] and asked["decided_at"]
    assert log["wrote"] is False
    assert json.loads(_get(base + "/api/records")) == []
    _no_planted(_registry_bytes(env), json.dumps(log).encode())


def test_an_expired_card_is_a_decline_and_no_write(app, env):
    base, state = app

    def person() -> None:
        _wait_for_card(env)  # nobody answers

    log = _run_agent(base, env, 2, person)

    (asked,) = log["asked"]
    assert asked["proceed"] is False
    assert asked["answer"] == "decline" and asked["reason"] == "expired"
    assert asked["opened_at"] and asked["decided_at"] is None
    assert log["wrote"] is False
    assert json.loads(_get(base + "/api/records")) == []
    record = json.loads(next(_registry(env).rglob("answer-*.json")).read_text())
    assert record["approver_kind"] == "expiry" and record["decided_at"] is None


def test_an_answer_from_a_key_off_the_roster_is_refused_and_nothing_is_written(app, env, tmp_path):
    base, state = app
    stranger = tmp_path / "stranger.pem"
    stranger.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(PASSPHRASE.encode()),
        )
    )

    def person() -> None:
        card = _wait_for_card(env)
        forged = _answer({**env, "OPENADAPT_TEST_KEY_PEM": str(stranger)}, card["card_id"], "done_by_person")
        assert forged.returncode == 2
        assert "human_approver_unregistered" in forged.stdout
        # nobody else answers: the card expires

    log = _run_agent(base, env, 3, person)
    (asked,) = log["asked"]
    assert asked["proceed"] is False
    assert json.loads(_get(base + "/api/records")) == []

