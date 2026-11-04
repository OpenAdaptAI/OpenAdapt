"""The golden free path with egress blocked: VERIFIED with no route off the machine.

The on-prem promise is that a run needs nothing outside the building. These
tests prove it on the real free path (record, compile, certify, run, verify
the write by an independent read) inside
``deploy/on-prem/bin/run-egress-blocked.sh``, a Linux network namespace that
holds only loopback. The kernel refuses every packet bound off the machine,
from Python, native code and the browser alike.

They run only inside that namespace (``OPENADAPT_EGRESS_BLOCKED=netns``), and
the first test does not trust the variable: it shows that public hosts,
public DNS resolvers included, are unreachable, so on a connected machine it
fails instead of passing.

The run also goes through strict no-outbound mode (:mod:`openadapt_flow.no_outbound`)
with its refusal log on. An empty log means the engine did not even try to
reach another machine; the second test shows that the log does fill when
something tries, so the empty log is not vacuous.
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
        # The namespace contains every child, the browser included.
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
    # Not one refused attempt: the engine never tried to leave.
    assert not log.exists() or log.read_text() == ""
