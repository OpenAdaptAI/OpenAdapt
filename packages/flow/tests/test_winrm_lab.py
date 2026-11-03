"""Lab host profiles in ``scripts/desktop/winrm_lab.py`` (no network)."""

from __future__ import annotations

import base64
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "desktop" / "winrm_lab.py"
_spec = importlib.util.spec_from_file_location("winrm_lab", _PATH)
lab_mod = importlib.util.module_from_spec(_spec)
# @dataclass looks its module up in sys.modules.
sys.modules.setdefault("winrm_lab", lab_mod)
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
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, b"ok\r\n", b"")

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    lab = lab_mod.WinRMLab(profile="winlap")
    assert lab.run_ps("Write-Output 'secret-token'").stdout.strip() == "ok"
    argv, kw = calls[0]
    assert argv[:3] == ["ssh", "-i", "/k"] and "user@10.0.0.2" in argv
    # The script travels on stdin, never on the command line.
    assert kw["input"] == b"Write-Output 'secret-token'"
    assert not any("secret-token" in a for a in argv)
    bootstrap = base64.b64decode(argv[-1].rsplit(" ", 1)[1]).decode("utf-16-le")
    assert "ReadToEnd" in bootstrap


def test_ssh_timeout_names_only_the_host(monkeypatch):
    monkeypatch.setenv("OAFLOW_WINLAP_HOST", "10.0.0.2")
    monkeypatch.setenv("OAFLOW_WINLAP_USER", "user")
    monkeypatch.setenv("OAFLOW_WINLAP_SSH_KEY", "/k")

    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 600)

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    lab = lab_mod.WinRMLab(profile="winlap")
    with pytest.raises(lab_mod.WinRMLabError) as err:
        lab.run_ps("Write-Output 'secret-token'")
    assert "secret-token" not in str(err.value) and "10.0.0.2" in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__


def test_ssh_run_cmd_quotes_for_powershell(monkeypatch):
    monkeypatch.setenv("OAFLOW_WINLAP_HOST", "10.0.0.2")
    monkeypatch.setenv("OAFLOW_WINLAP_USER", "user")
    monkeypatch.setenv("OAFLOW_WINLAP_SSH_KEY", "/k")
    scripts = []

    def fake_run(argv, **kw):
        scripts.append(kw["input"].decode())
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    lab_mod.WinRMLab(profile="winlap").run_cmd("tool.exe", "a&b", "it's")
    assert scripts == ["& 'tool.exe' 'a&b' 'it''s'; exit $LASTEXITCODE"]


def test_explicit_host_uses_the_winlab_password(monkeypatch):
    import types

    sessions = []
    fake = types.SimpleNamespace(Session=lambda *a, **kw: sessions.append((a, kw)))
    monkeypatch.setitem(sys.modules, "winrm", fake)
    monkeypatch.setenv("OAFLOW_WINLAB_PASSWORD", "pw")
    lab = lab_mod.WinRMLab("10.0.0.9", "admin")
    assert (lab.host, lab.user, lab.profile.transport) == ("10.0.0.9", "admin", "winrm")
    assert sessions[0][1]["auth"] == ("admin", "pw")


def test_clixml_stderr_becomes_text():
    raw = (
        '#< CLIXML\r\n<Objs><S S="Error">Access is denied._x000D__x000A_</S>'
        '<Obj S="progress"/></Objs>'
    )
    assert lab_mod._clixml_text(raw).strip() == "Access is denied."
    assert lab_mod._clixml_text("plain") == "plain"
