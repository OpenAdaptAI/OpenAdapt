"""ScrubbedEgress (J7): refusal up front, the macOS egress block, bypass checks.

The sandbox tests run for real on macOS: a sandboxed ``python -c`` must fail
to connect to a TEST-NET address (192.0.2.1, never routed) with "operation
not permitted" and must reach a loopback port. Nothing is sent off this
machine: the only non-loopback address used is TEST-NET, and the trap test
names a ``.invalid`` host that can't resolve.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.flight import egress as egress_mod
from openadapt_flow.flight.egress import (
    EgressRefused,
    ScrubbedEgress,
    egress_block_available,
    sandbox_profile,
)
from tests.fixtures.flight.fake_provider import FakeProvider

darwin_only = pytest.mark.skipif(
    sys.platform != "darwin", reason="the egress block is macOS sandbox-exec only"
)
SUMMARY_KEYS = {
    "mode",
    "enforced",
    "model_calls",
    "cost_usd",
    "cost_source",
    "refused_requests",
}


class FakeScrubber:
    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return text.replace("Zyxwv Qtest", "<PERSON>")

    def scrub_image(self, image: Any, fill_color: Any = None) -> Any:
        return image


@pytest.fixture
def provider():
    fake = FakeProvider().start()
    yield fake
    fake.stop()


@pytest.fixture
def unenforced(monkeypatch):
    monkeypatch.setattr(
        egress_mod, "egress_block_available", lambda: (False, "test: no block")
    )


def _loopback_server() -> tuple[socket.socket, int]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)

    def accept() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.close()

    threading.Thread(target=accept, daemon=True).start()
    return server, server.getsockname()[1]


_CONNECT = (
    "import socket,sys\n"
    "s=socket.socket();s.settimeout(3)\n"
    "try:\n"
    "    s.connect((sys.argv[1], int(sys.argv[2])))\n"
    "    print('connected')\n"
    "except PermissionError:\n"
    "    print('EPERM')\n"
    "except OSError as e:\n"
    "    print('error', type(e).__name__)\n"
)


def _run(argv: list[str], env: dict[str, str] | None = None) -> str:
    import os

    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **(env or {})},
    )
    return result.stdout.strip()


def test_importing_the_module_starts_nothing():
    code = (
        "import threading, openadapt_flow.flight.egress as e\n"
        "print(threading.active_count(), hasattr(e, 'ScrubbedEgress'))"
    )
    out = _run([sys.executable, "-c", code])
    assert out == "1 True"


@darwin_only
def test_the_sandbox_blocks_a_public_address_and_allows_loopback():
    ok, why = egress_block_available()
    assert ok, why
    server, port = _loopback_server()
    try:
        wrapped = ["/usr/bin/sandbox-exec", "-p", sandbox_profile(), sys.executable]
        blocked = _run([*wrapped, "-c", _CONNECT, "192.0.2.1", "443"])
        loop = _run([*wrapped, "-c", _CONNECT, "127.0.0.1", str(port)])
        # The control: unsandboxed, the same TEST-NET connect is not EPERM
        # (it times out or is unreachable), so EPERM above is the block.
        control = _run([sys.executable, "-c", _CONNECT, "192.0.2.1", "443"])
    finally:
        server.close()
    assert blocked == "EPERM"
    assert loop == "connected"
    assert control != "EPERM"


@darwin_only
def test_wrap_and_env_send_model_calls_through_the_proxy_and_nothing_else(
    tmp_path, provider
):
    server, port = _loopback_server()
    agent = (
        "import json,os,socket,sys,urllib.request\n"
        "req=urllib.request.Request(os.environ['ANTHROPIC_BASE_URL']+'/v1/messages',"
        "data=json.dumps({'model':'claude-opus-5-5','max_tokens':8,"
        "'messages':[{'role':'user','content':'hi Zyxwv Qtest'}]}).encode(),"
        "headers={'content-type':'application/json'},method='POST')\n"
        "opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
        "print(opener.open(req,timeout=30).status)\n"
        "s=socket.socket();s.settimeout(3)\n"
        "try:\n"
        "    s.connect(('192.0.2.1',443));print('connected')\n"
        "except PermissionError:\n"
        "    print('EPERM')\n"
        "s=socket.socket();s.connect(('127.0.0.1',int(sys.argv[1])));print('loop')\n"
    )
    run_dir = tmp_path / "task-1"
    try:
        with ScrubbedEgress(
            run_dir,
            spend_cap_usd=1.0,
            upstreams={"anthropic": provider.url},
            scrubber=FakeScrubber(),
        ) as egress:
            assert egress.enforced is True
            argv = egress.wrap([sys.executable, "-c", agent, str(port)])
            assert argv[0] == "/usr/bin/sandbox-exec"
            out = _run(argv, egress.env)
    finally:
        server.close()
    assert out.splitlines() == ["200", "EPERM", "loop"]
    assert b"Zyxwv Qtest" not in provider.received_bytes()
    summary = json.loads((run_dir / "egress.json").read_text())
    assert set(summary) == SUMMARY_KEYS
    assert summary["mode"] == "scrubbed"
    assert summary["enforced"] is True
    assert summary["model_calls"] == 1
    assert summary["cost_source"] == "metered" and summary["cost_usd"] > 0
    assert summary["refused_requests"] == 0
    assert egress.summary() == summary


def test_a_missing_scrubber_refuses_before_anything_starts(tmp_path, monkeypatch):
    from openadapt_flow.flight import scrub

    def missing(**_kw):
        raise scrub.ScrubberUnavailable("the tesseract binary is not on PATH")

    monkeypatch.setattr(scrub, "require_scrubber", missing)
    with pytest.raises(EgressRefused, match="tesseract"):
        with ScrubbedEgress(tmp_path / "run", spend_cap_usd=None):
            raise AssertionError("must not start")
    assert not (tmp_path / "run").exists()


def test_the_real_scrubber_check_names_what_is_missing(monkeypatch):
    from openadapt_flow import privacy
    from openadapt_flow.flight import scrub

    monkeypatch.setattr(scrub.shutil, "which", lambda name: None)
    with pytest.raises(scrub.ScrubberUnavailable, match="tesseract"):
        scrub.require_scrubber()
    monkeypatch.undo()

    class LeavesNames(FakeScrubber):
        def scrub_text(self, text: str, is_separated: bool = False) -> str:
            return text

    pytest.importorskip("pytesseract")
    if scrub.shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed (CI without it)")
    privacy.set_text_scrubber(LeavesNames())
    try:
        with pytest.raises(scrub.ScrubberUnavailable, match="probe"):
            scrub.require_scrubber()
    finally:
        privacy.reset_scrubbers()


def test_no_egress_block_refuses_unless_allowed(tmp_path, unenforced, provider):
    with pytest.raises(EgressRefused, match="egress block is not available"):
        with ScrubbedEgress(
            tmp_path / "a", spend_cap_usd=None, scrubber=FakeScrubber()
        ):
            raise AssertionError("must not start")
    with ScrubbedEgress(
        tmp_path / "b",
        spend_cap_usd=None,
        allow_unenforced=True,
        upstreams={"anthropic": provider.url},
        scrubber=FakeScrubber(),
    ) as egress:
        assert egress.enforced is False
        assert egress.wrap(["agent", "--x"]) == ["agent", "--x"]
        env = egress.env
        assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
        assert env["HTTPS_PROXY"].startswith("http://127.0.0.1:")
        assert "127.0.0.1" in env["NO_PROXY"]
    summary = json.loads((tmp_path / "b" / "egress.json").read_text())
    assert summary == {
        "mode": "scrubbed",
        "enforced": False,
        "model_calls": 0,
        "cost_usd": 0.0,
        "cost_source": "metered",
        "refused_requests": 0,
    }


def test_more_calls_in_usage_json_than_the_proxy_saw_refuses_the_run(
    tmp_path, unenforced, provider
):
    run_dir = tmp_path / "task"
    with pytest.raises(EgressRefused, match="reports 3 model calls but 0"):
        with ScrubbedEgress(
            run_dir,
            spend_cap_usd=None,
            allow_unenforced=True,
            upstreams={"anthropic": provider.url},
            scrubber=FakeScrubber(),
        ) as egress:
            (run_dir / "usage.json").write_text(
                json.dumps({"schema": "openadapt.agent-usage/v1", "calls": 3})
            )
    assert json.loads((run_dir / "egress.json").read_text())["mode"] == "blocked"
    assert egress.checks["agent_reported_calls"] == 3
    assert egress.checks["proxy_calls"] == 0


def test_a_direct_call_through_the_proxy_variables_is_trapped_and_refuses(
    tmp_path, unenforced, provider
):
    agent = (
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('https://api.anthropic.invalid/v1/messages',"
        "timeout=10)\n"
        "    print('sent')\n"
        "except Exception as e:\n"
        "    print('refused', type(e).__name__)\n"
    )
    run_dir = tmp_path / "task"
    with pytest.raises(EgressRefused, match="api.anthropic.invalid"):
        with ScrubbedEgress(
            run_dir,
            spend_cap_usd=None,
            allow_unenforced=True,
            upstreams={"anthropic": provider.url},
            scrubber=FakeScrubber(),
        ) as egress:
            out = _run(egress.wrap([sys.executable, "-c", agent]), egress.env)
    assert out.startswith("refused")
    assert provider.requests == []
    summary = json.loads((run_dir / "egress.json").read_text())
    assert summary["mode"] == "blocked" and summary["refused_requests"] == 1
    assert egress.checks["trapped_hosts"] == ["api.anthropic.invalid:443"]


def test_an_exception_in_the_body_is_not_masked_by_the_refusal(
    tmp_path, unenforced, provider
):
    run_dir = tmp_path / "task"
    with pytest.raises(KeyError):
        with ScrubbedEgress(
            run_dir,
            spend_cap_usd=None,
            allow_unenforced=True,
            scrubber=FakeScrubber(),
        ):
            (run_dir / "usage.json").write_text(json.dumps({"calls": 1}))
            raise KeyError("agent crashed")
    assert json.loads((run_dir / "egress.json").read_text())["mode"] == "blocked"


def test_env_before_enter_is_an_error(tmp_path):
    egress = ScrubbedEgress(Path(tmp_path), spend_cap_usd=None)
    with pytest.raises(RuntimeError):
        _ = egress.env


def test_use_block_false_drops_the_trap_too(tmp_path, provider):
    with ScrubbedEgress(
        tmp_path / "run",
        spend_cap_usd=None,
        allow_unenforced=True,
        use_block=False,
        upstreams={"anthropic": provider.url},
        scrubber=FakeScrubber(),
    ) as egress:
        assert egress.enforced is False
        assert "HTTPS_PROXY" not in egress.env and "NO_PROXY" not in egress.env
        assert egress.env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
