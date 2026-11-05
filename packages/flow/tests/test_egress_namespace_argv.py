"""_namespace must pass child env on the env argv; sudo -E is not available in CI."""

from __future__ import annotations

import os

from tests.e2e import test_egress_namespace_e2e as ns


def test_namespace_forwards_env_without_sudo_dash_e() -> None:
    forward = {
        "OPENADAPT_NO_OUTBOUND": "1",
        "OPENADAPT_NO_OUTBOUND_CHILDREN": "sandbox",
        "PYTHONPATH": "/tmp/flow-src",
        "PATH": "/should-not-override",
    }
    argv = ns._namespace("python", "-c", "pass", forward=forward)
    assert argv[0] == "sudo"
    assert argv[1] == "-n"
    assert argv[2] == "env"
    assert "-E" not in argv
    assert f"PATH={os.environ['PATH']}" in argv
    assert "PATH=/should-not-override" not in argv
    assert "OPENADAPT_NO_OUTBOUND=1" in argv
    assert "OPENADAPT_NO_OUTBOUND_CHILDREN=sandbox" in argv
    assert "PYTHONPATH=/tmp/flow-src" in argv
    assert str(ns.WRAPPER) in argv
    assert argv[-3:] == ["python", "-c", "pass"]


def test_namespace_without_forward_only_sets_path() -> None:
    argv = ns._namespace("/bin/true")
    assert "-E" not in argv
    assert argv[2] == "env"
    assert any(a.startswith("PATH=") for a in argv)
    assert not any(a.startswith("OPENADAPT_") for a in argv)
