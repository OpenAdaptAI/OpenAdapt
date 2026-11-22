"""End to end: an agent that never asks is held at a code prompt anyway.

Real headless Chromium. The scripted Playwright agent
(``benchmark/agent_guard/agent.py``) works in MockMed and never calls
``raise_exception``. ``openadapt-flow guard`` attaches to its browser over
CDP, and so does ``openadapt-flow exceptions watch``, each in its own
process. A synthetic one-time-code dialog appears on the agent's page. The
watch raises a ``second_factor`` card by itself and blocks the save the agent
tries while the card is open. A person clears the code prompt in the live
page and answers ``done_by_person`` with a registered test key; the agent's
next save goes through the guard and reads back VERIFIED.

Asserted from the app's own store: no write while the card was open, exactly
one write after the answer.

Synthetic MockMed in front of the ``effect_e2e`` record store; localhost only.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from openadapt_types.exception_card import answer_message  # noqa: E402

from benchmark.agent_guard import run as bench  # noqa: E402
from benchmark.agent_guard.agent import ScriptedAgent  # noqa: E402
from benchmark.agent_guard.app import serve  # noqa: E402
from openadapt_flow.approval_mode.approver_roster import (  # noqa: E402
    ROOT_ENV,
    file_key_signer,
    init_root,
)
from openadapt_flow.exceptions.registry import ExceptionRegistry  # noqa: E402
from openadapt_flow.execute.keys import sign_bytes  # noqa: E402
from openadapt_flow.jit.authority import public_hex  # noqa: E402

pytestmark = pytest.mark.timeout(600)

#: A synthetic code prompt laid over the page, as a sign-in service shows one.
CODE_PROMPT = """() => {
  const box = document.createElement('div');
  box.id = 'synthetic-code-prompt';
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  box.style.cssText = 'position:fixed;left:30%;top:30%;width:40%;height:20%;'
    + 'background:#fff;border:1px solid #000;z-index:1000';
  box.innerHTML = '<p>Check your phone</p><label for="otc">Verification code</label>'
    + '<input id="otc" autocomplete="one-time-code" inputmode="numeric" maxlength="6">';
  document.body.appendChild(box);
}"""
#: The agent's save while the prompt is up: MockMed's own write, sent from
#: its page. It ignores the prompt and never asks anyone.
AGENT_SAVE = """async (note) => {
  const r = await fetch('/api/encounter?fault=ok', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({patient_id: 'p1', type: 'Triage', note: note})});
  return {status: r.status, header: r.headers.get('X-OpenAdapt-Exception')};
}"""


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _chromium_or_skip() -> None:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium unavailable: {exc}")


class _Watch:
    """``openadapt-flow exceptions watch`` in its own process."""

    def __init__(self, args: list[str]) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "openadapt_flow", "exceptions", "watch", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        self.lines: "queue.Queue[str]" = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr.append(line)
            self.lines.put(line)

    def wait_attached(self, timeout_s: float = 90.0) -> None:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                line = self.lines.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    break
                continue
            if "exceptions: watching" in line:
                return
        raise RuntimeError("the watch did not attach:\n" + "".join(self.stderr))

    def finish(self) -> dict[str, Any]:
        try:
            out, _ = self.proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _ = self.proc.communicate()
        return {
            "exit": self.proc.returncode,
            **json.loads(out.strip().splitlines()[-1]),
        }


def _until(check, timeout_s: float = 60.0, what: str = "") -> Any:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_the_watch_holds_an_agent_that_never_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _chromium_or_skip()
    home = tmp_path / "home"
    key_dir = tmp_path / "keys"
    key = Ed25519PrivateKey.generate()
    init_root(
        key_dir,
        principal="front-desk",
        public_key=public_hex(key),
        sign=file_key_signer(key),
    )
    monkeypatch.setenv("OPENADAPT_HOME", str(home))
    monkeypatch.setenv("OPENADAPT_JIT_KEY_DIR", str(key_dir))
    monkeypatch.setenv(ROOT_ENV, public_hex(key))
    registry = ExceptionRegistry(home / "exceptions")

    app = serve(tmp_path / "app.sqlite")
    port = _port()
    work = tmp_path / "run"
    agent = guard = watch = None
    try:
        bench._post(
            app.base_url, "/api/reset", {"seed_concurrent": None, "fault": None}
        )
        files = bench.write_setup(tmp_path / "setup", app.base_url, teach_dir=None)
        agent = ScriptedAgent(port=port, think_s=0.2)
        agent.open(f"{app.base_url}/?fault=ok&idempotency=demo#tasks")
        guard = bench._GuardProcess(
            [
                "--cdp",
                f"http://127.0.0.1:{port}",
                "--config",
                str(files["guard"]),
                "--task",
                str(files["task"]),
                "--out",
                str(work),
                "--timeout",
                "120",
                "--no-teach",
            ]
        )
        guard.wait_attached()
        watch = _Watch(
            [
                "--cdp",
                f"http://127.0.0.1:{port}",
                "--config",
                str(files["guard"]),
                "--run-dir",
                str(work),
                "--run-ref",
                "watch-e2e",
                "--task-id",
                "triage-p1-0001",
                "--poll-s",
                "0.2",
                "--max-s",
                "240",
            ]
        )
        watch.wait_attached()
        writes_before = app.writes

        # A code prompt appears. The agent doesn't ask anyone.
        agent.page.evaluate(CODE_PROMPT)
        card = _until(
            lambda: next(iter(registry.open_cards("watch-e2e")), None), what="a card"
        )
        assert card.kind == "second_factor"
        # It saves anyway: the watch blocks it at the browser.
        tried = agent.page.evaluate(AGENT_SAVE, bench.NOTE)
        assert tried["status"] == 409, tried
        assert tried["header"] == "blocked; code=exception_open"
        assert app.writes == writes_before

        # A person clears the prompt in the live page, then answers.
        agent.page.evaluate(
            "() => document.getElementById('synthetic-code-prompt').remove()"
        )
        unsigned = registry.unsigned_answer(card, "done_by_person", route="cli")
        registry.write_answer(
            card,
            {
                **unsigned,
                "public_key": public_hex(key),
                "signature": sign_bytes(key, answer_message(unsigned)),
            },
        )
        log = work / "exceptions_watch.jsonl"
        _until(
            lambda: log.exists() and '"proceed": true' in log.read_text(),
            what="the watch to settle the card",
        )

        # The agent saves the ordinary way: the guard checks it, it lands once.
        acted = agent.add_triage_note(patient="p1", note=bench.NOTE)
        result = guard.finish(wait_s=30)
        assert result["run"]["verdict"] == "VERIFIED", (acted, result)
        assert app.writes == writes_before + 1

        watch.proc.terminate()
        done = watch.finish()
        assert done["run_ref"] == "watch-e2e" and done["stopped"] is None
        [outcome] = done["outcomes"]
        assert outcome["answer"] == "done_by_person" and outcome["proceed"] is True
        events = [json.loads(line) for line in log.read_text().splitlines()]
        kinds = [e["event"] for e in events]
        assert kinds.count("card_opened") == 1
        assert {"event": "request_blocked", "code": "exception_open"}.items() <= next(
            e for e in events if e["event"] == "request_blocked"
        ).items()
        assert bench.NOTE.encode() not in log.read_bytes()
    finally:
        for proc in (watch, guard):
            if proc is not None and proc.proc.poll() is None:
                proc.proc.kill()
        if agent is not None:
            agent.close()
        app.stop()
