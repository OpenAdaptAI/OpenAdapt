"""Lab host profiles in ``scripts/desktop/winrm_lab.py`` (no network)."""

from __future__ import annotations

import base64
import importlib.util
import subprocess
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "desktop" / "winrm_lab.py"
_spec = importlib.util.spec_from_file_location("winrm_lab", _PATH)
lab_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lab_mod)  # type: ignore[union-attr]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("OAFLOW_WIN") or key == "OAFLOW_LAB_PROFILE":
            monkeypatch.delenv(key)


def test_profile_reads_its_own_prefix(monkeypatch):
    monkeypatch.setenv("OAFLOW_LABA_HOST", "10.0.0.1")
    monkeypatch.setenv("OAFLOW_LABA_USER", "a")
    monkeypatch.setenv("OAFLOW_LABB_HOST", "10.0.0.2")
    monkeypatch.setenv("OAFLOW_LABB_USER", "b")
    monkeypatch.setenv("OAFLOW_LABB_SSH_KEY", "~/.ssh/k")
    monkeypatch.setenv("OAFLOW_LABB_PYTHON", r"C:\Py\pythonw.exe")
    a = lab_mod.HostProfile.from_env("laba")
    b = lab_mod.HostProfile.from_env("labb")
    assert (a.host, a.user, a.transport, a.python) == ("10.0.0.1", "a", "winrm", None)
    assert (b.host, b.user, b.transport) == ("10.0.0.2", "b", "ssh")
    assert b.ssh_key == str(Path("~/.ssh/k").expanduser())
    assert b.python == r"C:\Py\pythonw.exe"
    assert b.guest_dir == r"C:\oa"


def test_default_profile_is_named_by_env(monkeypatch):
    monkeypatch.setenv("OAFLOW_WINLAB_HOST", "10.0.0.1")
    monkeypatch.setenv("OAFLOW_WINLAB_USER", "a")
    monkeypatch.setenv("OAFLOW_WINLAP_HOST", "10.0.0.2")
    monkeypatch.setenv("OAFLOW_WINLAP_USER", "b")
    assert lab_mod.HostProfile.from_env().name == "winlab"
    monkeypatch.setenv("OAFLOW_LAB_PROFILE", "winlap")
    assert lab_mod.HostProfile.from_env().host == "10.0.0.2"


def test_missing_profile_names_the_variables():
    with pytest.raises(lab_mod.WinRMLabError, match="OAFLOW_NOPE_HOST"):
        lab_mod.HostProfile.from_env("nope")


def test_ssh_profile_needs_no_password_and_encodes_the_script(monkeypatch):
    monkeypatch.setenv("OAFLOW_WINLAP_HOST", "10.0.0.2")
    monkeypatch.setenv("OAFLOW_WINLAP_USER", "user")
    monkeypatch.setenv("OAFLOW_WINLAP_SSH_KEY", "/k")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"ok\r\n", b"")

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    lab = lab_mod.WinRMLab(profile="winlap")
    assert lab.run_ps("Write-Output 'ok'").stdout.strip() == "ok"
    argv = calls[0]
    assert argv[:3] == ["ssh", "-i", "/k"] and "user@10.0.0.2" in argv
    encoded = argv[-1].rsplit(" ", 1)[1]
    script = base64.b64decode(encoded).decode("utf-16-le")
    assert script.endswith("Write-Output 'ok'")


def test_clixml_stderr_becomes_text():
    raw = (
        "#< CLIXML\r\n<Objs><S S=\"Error\">Access is denied._x000D__x000A_</S>"
        "<Obj S=\"progress\"/></Objs>"
    )
    assert lab_mod._clixml_text(raw).strip() == "Access is denied."
    assert lab_mod._clixml_text("plain") == "plain"
