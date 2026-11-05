"""Golden free path inside a loopback-only Linux network namespace.

Run through ``deploy/on-prem/bin/run-egress-blocked.sh``. The first test
checks direct IP connections independently of the location environment
variable. The real tutorial still records, compiles, certifies, runs and
verifies a write through independent read-back.

Strict no-outbound mode adds a Python audit hook. Its empty refusal log means
no refusal was recorded by that hook. Native sockets, children without the
hook and allowed pathname Unix brokers are outside that observation. The
native-child positive control demonstrates this limit with a real attempt
that the network namespace refuses.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

FLOW = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENADAPT_EGRESS_BLOCKED") != "netns",
    reason="runs inside deploy/on-prem/bin/run-egress-blocked.sh (Linux, root)",
)

#: Public anycast hosts every connected machine reaches.
CANARIES = (("1.1.1.1", 443), ("8.8.8.8", 53), ("2606:4700:4700::1111", 443))


def _strict_env(log: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENADAPT_NO_")}
    env.update(
        OPENADAPT_NO_OUTBOUND="1",
        # Children inherit the direct-IP restriction; their sockets are not audited.
        OPENADAPT_NO_OUTBOUND_CHILDREN="sandbox",
        OPENADAPT_NO_OUTBOUND_LOG=str(log),
        PYTHONPATH=os.pathsep.join(
            [str(FLOW)] + [p for p in [env.get("PYTHONPATH", "")] if p]
        ),
    )
    return env


def test_nothing_off_the_machine_is_reachable() -> None:
    for host, port in CANARIES:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with pytest.raises(OSError):
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect((host, port))
    # Loopback still works: the local system of record lives there.
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        socket.create_connection(server.getsockname(), timeout=5).close()


def test_strict_mode_logs_an_attempt_to_leave(tmp_path: Path) -> None:
    log = tmp_path / "outbound-blocked.jsonl"
    probe = textwrap.dedent(
        """\
        import socket
        import openadapt_flow
        from openadapt_flow import no_outbound
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=1)
        except no_outbound.OutboundBlocked:
            print("blocked")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        env=_strict_env(log),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "blocked"
    assert json.loads(log.read_text().splitlines()[0])["host"] == "1.1.1.1"


def test_the_free_path_verifies_without_reaching_another_machine(
    tmp_path: Path,
) -> None:
    log = tmp_path / "outbound-blocked.jsonl"
    out = tmp_path / "tutorial"
    proc = subprocess.run(
        [sys.executable, "-m", "openadapt_flow", "tutorial", "--out", str(out)],
        env=_strict_env(log),
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    report = json.loads((out / "run" / "report.json").read_text())
    assert report["execution_outcome"] == "VERIFIED"
    envelope = report["outcome_envelope"]
    assert envelope["model_calls"] == 0
    assert envelope["external_network_calls"] == "none"
    assert envelope["passed_contracts"]["effect"] >= 1
    assert (out / "run" / "receipt.json").is_file()
    # No refusal was recorded by the Python hook; native attempts are not logged.
    assert not log.exists() or log.read_text() == ""


def test_native_child_attempt_is_blocked_without_a_python_refusal(tmp_path: Path) -> None:
    log = tmp_path / "native-outbound-blocked.jsonl"
    probe = textwrap.dedent(
        """\
        import json
        import subprocess
        import openadapt_flow
        proc = subprocess.run(
            ["/bin/bash", "-c", "printf native-marker >/dev/tcp/192.0.2.123/443"],
            capture_output=True, text=True, timeout=5,
        )
        print(json.dumps({"returncode": proc.returncode, "stderr": proc.stderr}))
        """
    )
    env = _strict_env(log)
    env["LC_ALL"] = "C"
    proc = subprocess.run(
        [sys.executable, "-c", probe], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    child = json.loads(proc.stdout)
    assert child["returncode"] != 0
    assert "Network is unreachable" in child["stderr"]
    assert not log.exists() or log.read_text() == ""
