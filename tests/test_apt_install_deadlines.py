"""Owned command deadlines and evidence-based apt failure classification."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_apt_install_action import _module

PROBE = Path(__file__).parent / "ci" / "apt_deadline_probe.py"


@pytest.mark.parametrize("uid", [0, 1001])
def test_deadline_sits_inside_the_command_privilege_boundary(monkeypatch, uid):
    apt = _module()
    monkeypatch.setattr(apt.os, "geteuid", lambda: uid)
    monkeypatch.setattr(apt.shutil, "which", lambda _name: "/subject/timeout")
    monkeypatch.setattr(apt.os, "access", lambda *_args: True)
    command = apt._deadline_command(apt._sudo(["apt-get", "update"]), 7)
    prefix = ["sudo", "-n"] if uid else []
    assert command == [
        *prefix,
        "/usr/bin/timeout",
        "--signal=KILL",
        "--",
        "7s",
        "apt-get",
        "update",
    ]
    assert "--foreground" not in command


@pytest.mark.parametrize("deadline", [0, -1])
def test_invalid_deadline_cannot_disable_the_timer(deadline):
    with pytest.raises(ValueError, match="positive"):
        _module()._deadline_command(["synthetic"], deadline)


def test_missing_timer_is_a_setup_refusal(monkeypatch):
    apt = _module()
    monkeypatch.setattr(apt, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(apt.os, "geteuid", lambda: 501)
    monkeypatch.setattr(apt.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit, match="GNU timeout is required"):
        apt._deadline_command(["synthetic"], 1)


@pytest.mark.parametrize("stubborn", [False, True])
def test_timed_command_releases_its_descendants_lock(tmp_path, stubborn):
    spec = importlib.util.spec_from_file_location("apt_deadline_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    result = module.probe(
        _module(), tmp_path / "case", real_sudo=False, stubborn=stubborn
    )
    assert result["lock_released_when_helper_returned"]


@pytest.mark.parametrize(
    "update,download,reason",
    [
        (
            (100, "Temporary failure resolving 'archive.ubuntu.com'"),
            None,
            "dns-unreachable",
        ),
        ((100, "Could not connect to archive.ubuntu.com"), None, "mirror-unreachable"),
        ((137, "interrupted"), None, "command-interrupted"),
        ((100, "E: Read-only file system"), None, "index-update-failed"),
        (
            (0, "Hit:1 http://archive.ubuntu.com/ubuntu noble InRelease\n"),
            (100, "E: Could not get lock: held by process"),
            "download-failed",
        ),
        (
            (0, "Hit:1 http://archive.ubuntu.com/ubuntu noble InRelease\n"),
            (137, "interrupted"),
            "download-failed",
        ),
        (
            (0, "Hit:1 http://archive.ubuntu.com/ubuntu noble InRelease\n"),
            (100, "Unable to locate package synthetic"),
            "package-not-found",
        ),
    ],
)
def test_failure_reason_uses_observed_command_phase(
    tmp_path, monkeypatch, capsys, update, download, reason
):
    apt = _module()
    monkeypatch.setenv("RUNNER_OS", "Linux")
    monkeypatch.setattr(apt.shutil, "which", lambda _name: "/fake/apt-get")
    monkeypatch.setattr(apt, "_arch", lambda: "amd64")
    monkeypatch.setattr(apt, "_write_root", lambda *_args: None)
    monkeypatch.setattr(apt, "_missing", lambda packages: packages)
    monkeypatch.setattr(apt, "third_party_hosts", lambda: set())
    monkeypatch.setattr(
        apt, "Sources", lambda: type("Sources", (), {"apply": lambda *_args: None})()
    )
    monkeypatch.setattr(
        apt,
        "_run",
        lambda command, timeout=None: update if "update" in command else download,
    )
    result = tmp_path / "result.json"
    code = apt.cmd_install(
        argparse.Namespace(
            packages="synthetic",
            recommends="false",
            cache_dir=str(tmp_path / "cache"),
            result=str(result),
            cache_hit="none",
            refresh_index="false",
        )
    )
    assert code == 1
    assert json.loads(result.read_text())["reason"] == reason
    output = capsys.readouterr().out
    if reason in {"download-failed", "index-update-failed", "command-interrupted"}:
        assert "infrastructure outage" not in output.lower()
        assert "no Ubuntu mirror answered" not in output


@pytest.mark.parametrize("stubborn", [False, True])
def test_offline_playwright_resolution_owns_its_timed_descendants(
    tmp_path, monkeypatch, stubborn
):
    apt = _module()
    monkeypatch.setattr(apt, "PLAYWRIGHT_TIMEOUT", 1)
    spec = importlib.util.spec_from_file_location("apt_resolver_deadline_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    def invoke(command, timeout):
        assert timeout == 1
        with pytest.raises(SystemExit, match=r"exit (-9|137)") as raised:
            apt.resolve_playwright(shlex.join(command), "chromium", str(tmp_path))
        return 137, str(raised.value)

    result = module.probe(
        SimpleNamespace(_run=invoke),
        tmp_path / "resolver",
        real_sudo=False,
        stubborn=stubborn,
    )
    assert result["lock_released_when_helper_returned"]


def test_real_sudo_probe_refuses_an_ordinary_local_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    spec = importlib.util.spec_from_file_location("apt_refused_sudo_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="ephemeral GitHub Linux runner"):
        module.probe(None, tmp_path / "refused", real_sudo=True, stubborn=False)
    assert not (tmp_path / "refused").exists()


def test_owned_download_interruption_can_recover_at_the_next_mirror(
    tmp_path, monkeypatch
):
    apt = _module()
    state = {"downloads": 0, "installed": False}
    monkeypatch.setenv("RUNNER_OS", "Linux")
    monkeypatch.setattr(apt.shutil, "which", lambda _name: "/fake/apt-get")
    monkeypatch.setattr(apt, "_arch", lambda: "amd64")
    monkeypatch.setattr(apt, "_write_root", lambda *_args: None)
    monkeypatch.setattr(apt, "_prune_cache", lambda *_args: None)
    monkeypatch.setattr(apt, "third_party_hosts", lambda: set())
    monkeypatch.setattr(
        apt, "_missing", lambda packages: [] if state["installed"] else packages
    )
    monkeypatch.setattr(
        apt, "Sources", lambda: type("Sources", (), {"apply": lambda *_args: None})()
    )

    def run(command, timeout=None):
        if "update" in command:
            return 0, "Hit:1 http://archive.ubuntu.com/ubuntu noble InRelease\n"
        if "--download-only" in command:
            state["downloads"] += 1
            return (
                (137, "interrupted") if state["downloads"] == 1 else (0, "downloaded")
            )
        assert "--no-download" in command
        state["installed"] = True
        return 0, "installed"

    monkeypatch.setattr(apt, "_run", run)
    result = tmp_path / "result.json"
    assert (
        apt.cmd_install(
            argparse.Namespace(
                packages="synthetic",
                recommends="false",
                cache_dir=str(tmp_path / "cache"),
                result=str(result),
                cache_hit="none",
                refresh_index="false",
            )
        )
        == 0
    )
    record = json.loads(result.read_text())
    assert state == {"downloads": 2, "installed": True}
    assert record["source"] == "network"
    assert record["mirror"] == "archive.ubuntu.com and security.ubuntu.com"
    assert record["reason"] == ""


def test_requested_refresh_failure_does_not_claim_available_packages_are_missing(
    tmp_path, monkeypatch, capsys
):
    apt = _module()
    monkeypatch.setenv("RUNNER_OS", "Linux")
    monkeypatch.setattr(apt.shutil, "which", lambda _name: "/fake/apt-get")
    monkeypatch.setattr(apt, "_arch", lambda: "amd64")
    monkeypatch.setattr(apt, "_write_root", lambda *_args: None)
    monkeypatch.setattr(apt, "_missing", lambda _packages: [])
    monkeypatch.setattr(apt, "third_party_hosts", lambda: set())
    monkeypatch.setattr(
        apt, "Sources", lambda: type("Sources", (), {"apply": lambda *_args: None})()
    )
    monkeypatch.setattr(
        apt, "_run", lambda *_args, **_kwargs: (100, "E: Read-only file system")
    )
    result = tmp_path / "result.json"
    assert (
        apt.cmd_install(
            argparse.Namespace(
                packages="synthetic",
                recommends="false",
                cache_dir=str(tmp_path / "cache"),
                result=str(result),
                cache_hit="none",
                refresh_index="true",
            )
        )
        == 1
    )
    assert json.loads(result.read_text())["reason"] == "index-update-failed"
    output = capsys.readouterr().out
    assert "Requested packages available: synthetic; index refresh failed." in output
    assert "Packages not installed:" not in output


@pytest.mark.parametrize("with_sudo", [False, True])
def test_linux_timer_identity_is_independent_of_subject_path(monkeypatch, with_sudo):
    apt = _module()
    monkeypatch.setattr(apt, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(apt.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(apt.os, "access", lambda *_args: True)
    monkeypatch.setattr(apt.shutil, "which", lambda _name: "/subject/timeout")
    command = ["sudo", "-n", "synthetic"] if with_sudo else ["synthetic"]
    bounded = apt._deadline_command(command, 1)
    assert bounded[2 if with_sudo else 0] == "/usr/bin/timeout"
    assert "/subject/timeout" not in bounded


def test_absent_system_timer_never_falls_back_to_subject_path(monkeypatch):
    apt = _module()
    monkeypatch.setattr(apt, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(apt.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(apt.os, "access", lambda *_args: False)
    monkeypatch.setattr(apt.shutil, "which", lambda _name: "/subject/timeout")
    with pytest.raises(SystemExit, match="GNU timeout is required"):
        apt._deadline_command(["sudo", "-n", "synthetic"], 1)
