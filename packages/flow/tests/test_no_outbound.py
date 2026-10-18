"""Strict no-outbound mode is enforced, not advisory.

The audit hook cannot be removed once installed, so every check that turns
it on runs in a fresh interpreter.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from openadapt_flow import no_outbound
from openadapt_flow.no_outbound import host_is_local, parse_allow

ROOT = Path(__file__).resolve().parents[1]


def _child(code: str, tmp_path: Path, **extra: str) -> subprocess.CompletedProcess:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("OPENADAPT_NO_OUTBOUND")
    }
    env.update(
        {
            "OPENADAPT_NO_OUTBOUND": "1",
            "OPENADAPT_NO_OUTBOUND_LOG": str(tmp_path / "blocked.jsonl"),
            "PYTHONPATH": str(ROOT),
            # Proxies would change which host a client connects to.
            "NO_PROXY": "*",
        }
    )
    env.update(extra)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp_path),
    )


def _blocked(tmp_path: Path) -> list[dict]:
    path = tmp_path / "blocked.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_enabled_reads_the_env():
    assert no_outbound.enabled({"OPENADAPT_NO_OUTBOUND": "1"})
    assert no_outbound.enabled({"OPENADAPT_NO_OUTBOUND": "strict"})
    assert not no_outbound.enabled({})
    assert not no_outbound.enabled({"OPENADAPT_NO_OUTBOUND": "0"})


def test_allow_list_takes_private_ip_ranges_only():
    assert [str(n) for n in parse_allow("10.0.0.5, 192.168.1.0/24")] == [
        "10.0.0.5/32",
        "192.168.1.0/24",
    ]
    with pytest.raises(ValueError):
        parse_allow("records.example.org")  # a host name would need DNS
    with pytest.raises(ValueError):
        parse_allow("8.8.8.8")  # public
    with pytest.raises(ValueError):
        parse_allow("0.0.0.0/0")


def test_host_classification():
    allow = parse_allow("10.1.2.0/24")
    assert host_is_local("127.0.0.1")
    assert host_is_local("::1")
    assert host_is_local("localhost")
    assert host_is_local(None)
    assert host_is_local("10.1.2.7", allow)
    assert not host_is_local("10.1.3.7", allow)
    assert not host_is_local("93.184.216.34")
    assert not host_is_local("example.org")


def test_public_connect_is_refused_before_any_packet(tmp_path):
    proc = _child(
        """
        import socket
        from openadapt_flow import no_outbound
        no_outbound.enforce()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("192.0.2.10", 443))
        except no_outbound.OutboundBlocked:
            print("BLOCKED")
        """,
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "BLOCKED" in proc.stdout
    events = _blocked(tmp_path)
    assert events[0]["event"] == "socket.connect"
    assert events[0]["host"] == "192.0.2.10" and events[0]["port"] == 443


def test_dns_lookup_of_an_outside_name_is_refused(tmp_path):
    proc = _child(
        """
        import socket
        from openadapt_flow import no_outbound
        no_outbound.enforce()
        for fn in (lambda: socket.getaddrinfo("app.openadapt.ai", 443),
                   lambda: socket.gethostbyname("app.openadapt.ai")):
            try:
                fn()
            except no_outbound.OutboundBlocked:
                print("BLOCKED")
        """,
        tmp_path,
    )
    assert proc.stdout.count("BLOCKED") == 2, proc.stderr
    assert {e["event"] for e in _blocked(tmp_path)} == {
        "socket.getaddrinfo",
        "socket.gethostbyname",
    }


def test_udp_sendto_outside_is_refused(tmp_path):
    proc = _child(
        """
        import socket
        from openadapt_flow import no_outbound
        no_outbound.enforce()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(b"x", ("192.0.2.53", 53))
        except no_outbound.OutboundBlocked:
            print("BLOCKED")
        """,
        tmp_path,
    )
    assert "BLOCKED" in proc.stdout, proc.stderr


def test_http_client_to_cloud_fails_closed(tmp_path):
    proc = _child(
        """
        import httpx
        from openadapt_flow import no_outbound
        no_outbound.enforce()
        try:
            httpx.get("https://app.openadapt.ai/api/public/seal-keys", timeout=5)
        except httpx.HTTPError as exc:
            print("FAILED", type(exc).__name__)
        """,
        tmp_path,
    )
    assert "FAILED" in proc.stdout, proc.stderr
    assert _blocked(tmp_path), "the refusal was not logged"


def test_loopback_and_allow_listed_lan_still_work(tmp_path):
    proc = _child(
        """
        import socket, threading
        from openadapt_flow import no_outbound
        no_outbound.enforce()
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        port = srv.getsockname()[1]
        threading.Thread(target=lambda: srv.accept()[0].sendall(b"ok"), daemon=True).start()
        c = socket.create_connection(("127.0.0.1", port), timeout=5)
        print(c.recv(2).decode())
        # An allow-listed LAN literal passes the hook (nothing listens there,
        # so the OS answers; what matters is that the hook did not refuse).
        s = socket.socket(); s.settimeout(0.2)
        try:
            s.connect(("10.255.255.1", 9))
        except no_outbound.OutboundBlocked:
            print("WRONGLY_BLOCKED")
        except OSError:
            print("LAN_ALLOWED")
        """,
        tmp_path,
        OPENADAPT_NO_OUTBOUND_ALLOW="10.255.255.0/24",
    )
    assert "ok" in proc.stdout, proc.stderr
    assert "LAN_ALLOWED" in proc.stdout and "WRONGLY_BLOCKED" not in proc.stdout
    assert _blocked(tmp_path) == []


def test_the_hook_cannot_be_turned_off_once_on(tmp_path):
    proc = _child(
        """
        import os, socket
        from openadapt_flow import no_outbound
        no_outbound.enforce()
        os.environ["OPENADAPT_NO_OUTBOUND"] = "0"
        no_outbound.enforce()
        try:
            socket.getaddrinfo("example.org", 80)
        except no_outbound.OutboundBlocked:
            print("STILL_BLOCKED")
        """,
        tmp_path,
    )
    assert "STILL_BLOCKED" in proc.stdout, proc.stderr


def test_bad_allow_list_stops_startup(tmp_path):
    proc = _child(
        """
        from openadapt_flow import no_outbound
        no_outbound.enforce_from_env()
        """,
        tmp_path,
        OPENADAPT_NO_OUTBOUND_ALLOW="records.example.org",
    )
    assert proc.returncode != 0
    assert "OPENADAPT_NO_OUTBOUND_ALLOW" in proc.stderr


def test_cli_enforces_the_mode_and_attach_seal_refuses(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text(json.dumps({"execution_outcome": "VERIFIED"}))
    proc = _child(
        f"""
        import sys
        from openadapt_flow.__main__ import main
        rc = main(["attach-seal", {str(run)!r}, "--execution-id", "exe_1"])
        from openadapt_flow import no_outbound
        print("RC", rc, "HOOK", no_outbound.installed())
        """,
        tmp_path,
        OPENADAPT_EXECUTE_API_KEY="not-a-real-key",
    )
    assert "RC 1 HOOK True" in proc.stdout, proc.stderr
    assert "no_outbound" in proc.stderr
    # Refused before any connection was attempted.
    assert _blocked(tmp_path) == []


def test_offline_seal_and_batch_work_under_the_mode(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "report.json").write_text(
        json.dumps({"run_id": "r", "execution_outcome": "VERIFIED"})
    )
    proc = _child(
        f"""
        from openadapt_flow.__main__ import main
        home = {str(tmp_path / "home")!r}
        assert main(["local-seal", "--home", home, "run", {str(run)!r}]) == 0
        assert main(["local-seal", "--home", home, "verify", {str(run)!r}]) == 0
        assert main(["local-seal", "--home", home, "batch"]) == 0
        print("SEALED_OFFLINE")
        """,
        tmp_path,
    )
    assert "SEALED_OFFLINE" in proc.stdout, proc.stderr
    assert _blocked(tmp_path) == []
