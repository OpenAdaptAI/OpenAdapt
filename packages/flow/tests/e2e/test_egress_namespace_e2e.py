"""Real namespace setup and an owned pathname Unix broker boundary.

Opt in with OPENADAPT_TEST_NETNS=1 on Linux with passwordless sudo. These
tests run outside the wrapper and create only fresh synthetic listeners.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

FLOW = Path(__file__).resolve().parents[2]
WRAPPER = FLOW / "deploy/on-prem/bin/run-egress-blocked.sh"
pytestmark = pytest.mark.skipif(
    os.environ.get("OPENADAPT_TEST_NETNS") != "1",
    reason="opt in to real Linux namespace creation with OPENADAPT_TEST_NETNS=1",
)


def _invoke(
    argv: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)


def _namespace(*argv: str) -> list[str]:
    return ["sudo", "-n", "-E", "env", f"PATH={os.environ['PATH']}", str(WRAPPER), *argv]


@pytest.mark.parametrize("root", [False, True])
def test_real_namespace_and_invoking_identity(root: bool) -> None:
    code = (
        "import json,os,subprocess; print(json.dumps(dict("
        "uid=os.getuid(),gid=os.getgid(),netns=os.readlink('/proc/self/ns/net'),"
        "links=subprocess.check_output(['ip','-o','link','show'],text=True),"
        "routes4=subprocess.check_output(['ip','route','show'],text=True),"
        "routes6=subprocess.check_output(['ip','-6','route','show'],text=True))))"
    )
    argv = _namespace(sys.executable, "-c", code)
    if root:
        argv[4:4] = ["-u", "SUDO_UID", "-u", "SUDO_GID"]
    proc = _invoke(argv)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["uid"] == (0 if root else os.getuid())
    assert result["gid"] == (0 if root else os.getgid())
    assert result["netns"] != os.readlink("/proc/self/ns/net")
    assert [line.split(": ", 2)[1] for line in result["links"].splitlines()] == ["lo"]
    assert result["routes4"] == ""
    assert result["routes6"] == ""


@pytest.mark.parametrize(
    ("inspection", "output", "status"),
    [
        ("-o link show", "", 69),
        ("route show", "", 69),
        ("-6 route show", "", 69),
        ("-o link show", "", 0),
        ("-o link show", "2: synthetic0: <UP> mtu 1500", 0),
        ("route show", "default via 192.0.2.1 dev synthetic0", 0),
        ("-6 route show", "default via 2001:db8::1 dev synthetic0", 0),
    ],
)
def test_unreadable_or_unexpected_inspection_prevents_command(
    tmp_path: Path, inspection: str, output: str, status: int
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("uname", "id", "bash", "unshare", "setpriv", "awk", "grep"):
        real = shutil.which(name)
        assert real is not None, name
        (tools / name).symlink_to(real)
    real_ip = shutil.which("ip")
    assert real_ip is not None
    fake_ip = tools / "ip"
    fake_ip.write_text(
        "#!/bin/bash\n"
        f"if [[ \"$*\" == {inspection!r} ]]; then\n"
        f"  printf '%s' {output!r}\n  exit {status}\nfi\n"
        f"exec {real_ip!r} \"$@\"\n"
    )
    fake_ip.chmod(0o755)
    marker = tmp_path / "command-ran"
    proc = _invoke(
        ["sudo", "-n", "env", f"PATH={tools}", str(WRAPPER),
         "/usr/bin/touch", str(marker)]
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert not marker.exists()


@pytest.mark.parametrize("mode", ["nonroot", "missing-unshare", "failed-unshare"])
def test_setup_failure_prevents_command(tmp_path: Path, mode: str) -> None:
    marker = tmp_path / "command-ran"
    argv = [str(WRAPPER), "/usr/bin/touch", str(marker)]
    expected = 2
    if mode == "nonroot":
        assert os.getuid() != 0, "this outer test must run as an unprivileged user"
    else:
        tools = tmp_path / "tools"
        tools.mkdir()
        for name in ("uname", "id", "bash", "ip", "setpriv"):
            real = shutil.which(name)
            assert real is not None, name
            (tools / name).symlink_to(real)
        if mode == "failed-unshare":
            unshare = tools / "unshare"
            unshare.write_text("#!/bin/bash\nexit 73\n")
            unshare.chmod(0o755)
            expected = 73
        argv = ["sudo", "-n", "env", f"PATH={tools}", *argv]
    proc = _invoke(argv)
    assert proc.returncode == expected, (proc.stdout, proc.stderr)
    assert not marker.exists()


def test_owned_pathname_unix_relay_remains_outside_the_ip_restriction(
    tmp_path: Path,
) -> None:
    marker = b"synthetic-boundary-marker"
    ack = b"synthetic-boundary-ack"
    receipts: list[bytes] = []
    errors: list[str] = []
    # AF_UNIX paths have a short OS limit; pytest's evidence path may be long.
    with tempfile.TemporaryDirectory(prefix="oa-egress-") as directory:
        unix_path = str(Path(directory) / "relay.sock")
        with socket.socket() as tcp, socket.socket(socket.AF_UNIX) as unix:
            tcp.bind(("127.0.0.1", 0))
            tcp.listen(3)
            tcp.settimeout(15)
            address = tcp.getsockname()
            unix.bind(unix_path)
            unix.listen(2)
            unix.settimeout(15)

            def sink() -> None:
                try:
                    for _ in range(3):
                        conn, _ = tcp.accept()
                        with conn:
                            conn.settimeout(5)
                            receipts.append(conn.recv(128))
                            conn.sendall(ack)
                except Exception as exc:
                    errors.append(repr(exc))

            def relay() -> None:
                try:
                    for _ in range(2):
                        conn, _ = unix.accept()
                        with conn, socket.create_connection(address, timeout=5) as upstream:
                            conn.settimeout(5)
                            upstream.sendall(conn.recv(128))
                            conn.sendall(upstream.recv(128))
                except Exception as exc:
                    errors.append(repr(exc))

            workers = [threading.Thread(target=sink), threading.Thread(target=relay)]
            for worker in workers:
                worker.start()
            try:
                # First prove the independent host-side listener actually works.
                with socket.create_connection(address, timeout=5) as direct:
                    direct.sendall(b"direct-control")
                    assert direct.recv(128) == ack
                code = (
                    "import json,os,socket; import openadapt_flow; "
                    "from openadapt_flow import no_outbound; "
                    "assert no_outbound.installed(); "
                    f"s=socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect({unix_path!r}); "
                    f"s.sendall({marker!r}); ack=s.recv(128).decode(); s.close(); "
                    "print(json.dumps(dict(ack=ack,netns=os.readlink('/proc/self/ns/net'))))"
                )
                log = tmp_path / "relay-refusals.jsonl"
                env = {k: v for k, v in os.environ.items() if not k.startswith("OPENADAPT_NO_")}
                env.update(
                    OPENADAPT_NO_OUTBOUND="1",
                    OPENADAPT_NO_OUTBOUND_CHILDREN="sandbox",
                    OPENADAPT_NO_OUTBOUND_LOG=str(log),
                    PYTHONPATH=str(FLOW),
                )
                baseline = _invoke([sys.executable, "-c", code], env=env)
                assert baseline.returncode == 0, baseline.stderr
                assert json.loads(baseline.stdout)["ack"] == ack.decode()
                assert receipts == [b"direct-control", marker]
                # The same outside TCP listener is unreachable by direct IP.
                direct_code = (
                    "import socket,errno; s=socket.socket(); s.settimeout(2); "
                    f"\ntry: s.connect({address!r}); raise AssertionError('host TCP reachable')"
                    "\nexcept OSError as exc: assert exc.errno in (errno.ENETUNREACH,errno.ECONNREFUSED), exc"
                )
                direct = _invoke(_namespace(sys.executable, "-c", direct_code), env=env)
                assert direct.returncode == 0, direct.stderr
                isolated = _invoke(_namespace(sys.executable, "-c", code), env=env)
                assert isolated.returncode == 0, isolated.stderr
                result = json.loads(isolated.stdout)
                assert result["netns"] != os.readlink("/proc/self/ns/net")
                assert result["ack"] == ack.decode()
            finally:
                for worker in workers:
                    worker.join(20)
                (tmp_path / "independent-relay-receipt.json").write_text(
                    json.dumps({"received": [x.decode() for x in receipts], "errors": errors})
                )
            assert not any(worker.is_alive() for worker in workers)
            assert not errors, errors
            assert receipts == [b"direct-control", marker, marker]
            assert not log.exists() or log.read_text() == ""
