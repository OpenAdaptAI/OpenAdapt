"""Lab host profiles in ``scripts/desktop/winrm_lab.py`` (no network)."""

from __future__ import annotations

import base64
import importlib.util
import json
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
    assert kw["input"] == b"27\nWrite-Output 'secret-token'"
    assert not any("secret-token" in a for a in argv)
    bootstrap = base64.b64decode(argv[-1].rsplit(" ", 1)[1]).decode("utf-16-le")
    assert "ReadToEnd" not in bootstrap
    assert "ReadLine" in bootstrap and ".Read($b,$r,$n-$r)" in bootstrap
    assert "if ($k -le 0) { exit 2 }" in bootstrap


def test_ssh_timeout_names_only_the_host(monkeypatch):
    monkeypatch.setenv("OAFLOW_WINLAP_HOST", "10.0.0.2")
    monkeypatch.setenv("OAFLOW_WINLAP_USER", "user")
    monkeypatch.setenv("OAFLOW_WINLAP_SSH_KEY", "/k")

    def fake_run(argv, **kw):
        assert kw["timeout"] == 600
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
        units, script = kw["input"].decode().split("\n", 1)
        assert int(units) == len(script.encode("utf-16-le")) // 2
        scripts.append(script)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    lab_mod.WinRMLab(profile="winlap").run_cmd("tool.exe", "a&b", "it's")
    assert scripts == ["& 'tool.exe' 'a&b' 'it''s'; exit $LASTEXITCODE"]


def test_ssh_frame_counts_utf16_units_and_keeps_payload_off_argv(monkeypatch):
    monkeypatch.setenv("OAFLOW_WINLAP_HOST", "192.0.2.1")
    monkeypatch.setenv("OAFLOW_WINLAP_USER", "operator")
    monkeypatch.setenv("OAFLOW_WINLAP_SSH_KEY", "/synthetic/key")
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    script = "Write-Output 'synthetic-\U0001f680-\u00e9'"
    lab_mod.WinRMLab(profile="winlap").run_ps(script)
    argv, kw = calls[0]
    units, payload = kw["input"].decode("utf-8").split("\n", 1)
    assert payload == script
    assert int(units) == len(script) + 1
    assert not any(script in arg for arg in argv)
    assert "OAFlowLastOk" in base64.b64decode(argv[-1].rsplit(" ", 1)[1]).decode(
        "utf-16-le"
    )


def test_stop_task_has_a_short_redacted_timeout_without_retry(monkeypatch):
    monkeypatch.setenv("OAFLOW_WINLAP_HOST", "192.0.2.1")
    monkeypatch.setenv("OAFLOW_WINLAP_USER", "operator")
    monkeypatch.setenv("OAFLOW_WINLAP_SSH_KEY", "/synthetic/key")
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        raise subprocess.TimeoutExpired(argv, kw["timeout"], output=b"sensitive")

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    with pytest.raises(lab_mod.WinRMLabError) as err:
        lab_mod.WinRMLab(profile="winlap").stop_interactive("SyntheticAgent")
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 30
    assert "stop interactive task" in str(err.value)
    assert "30s" in str(err.value)
    assert "sensitive" not in str(err.value)
    assert "SyntheticAgent" not in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__


def test_ssh_exit_status_and_check_false_are_preserved(monkeypatch):
    monkeypatch.setenv("OAFLOW_WINLAP_HOST", "192.0.2.1")
    monkeypatch.setenv("OAFLOW_WINLAP_USER", "operator")
    monkeypatch.setenv("OAFLOW_WINLAP_SSH_KEY", "/synthetic/key")
    monkeypatch.setattr(
        lab_mod.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 7, b"partial", b"failed"),
    )
    lab = lab_mod.WinRMLab(profile="winlap")
    result = lab.run_ps("Write-Output 'value'", check=False)
    assert (result.status, result.stdout, result.stderr) == (7, "partial", "failed")
    with pytest.raises(lab_mod.WinRMLabError, match="guest PowerShell exited 7"):
        lab.run_ps("Write-Output 'value'")


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_refuses_before_ssh(monkeypatch, timeout):
    monkeypatch.setenv("OAFLOW_WINLAP_HOST", "192.0.2.1")
    monkeypatch.setenv("OAFLOW_WINLAP_USER", "operator")
    monkeypatch.setenv("OAFLOW_WINLAP_SSH_KEY", "/synthetic/key")
    monkeypatch.setattr(
        lab_mod.subprocess, "run", lambda *args, **kw: pytest.fail("SSH was called")
    )
    with pytest.raises(ValueError, match="finite and positive"):
        lab_mod.WinRMLab(profile="winlap").run_ps(
            "Write-Output 'value'", timeout_s=timeout
        )


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


def test_app_python_is_the_console_interpreter_beside_the_agents(monkeypatch):
    monkeypatch.setenv("OAFLOW_WINLAP_HOST", "10.0.0.2")
    monkeypatch.setenv("OAFLOW_WINLAP_USER", "user")
    monkeypatch.setenv("OAFLOW_WINLAP_SSH_KEY", "/k")
    monkeypatch.setenv("OAFLOW_WINLAP_PYTHON", r"C:\Python312\pythonw.exe")
    lab = lab_mod.WinRMLab(profile="winlap")
    assert lab.resolve_app_python() == r"C:\Python312\python.exe"
    monkeypatch.setenv("OAFLOW_WINLAP_APP_PYTHON", r"D:\py\python.exe")
    assert (
        lab_mod.WinRMLab(profile="winlap").resolve_app_python() == r"D:\py\python.exe"
    )


def test_lab_harnesses_name_no_interpreter_path():
    desktop = _PATH.parent
    for script in desktop.glob("qualify_lab_*.py"):
        assert "Python312-arm64" not in script.read_text(), script.name


@pytest.fixture
def synthetic_ssh_lab(monkeypatch):
    monkeypatch.setenv("OAFLOW_SYNTHETIC_HOST", "192.0.2.1")
    monkeypatch.setenv("OAFLOW_SYNTHETIC_USER", "operator")
    monkeypatch.setenv("OAFLOW_SYNTHETIC_SSH_KEY", "/synthetic/not-a-key")
    return lab_mod.WinRMLab(profile="synthetic")


@pytest.mark.parametrize("method", ["_ssh", "run_ps", "run_cmd"])
def test_generic_ssh_budget_preserves_ten_minutes(
    monkeypatch, synthetic_ssh_lab, method
):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, b"ok", b"")

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    getattr(synthetic_ssh_lab, method)("synthetic-command")
    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 600


@pytest.mark.parametrize(
    "method,check",
    [
        ("_ssh", None),
        ("run_ps", False),
        ("run_ps", True),
        ("run_cmd", False),
        ("run_cmd", True),
    ],
)
def test_timeout_retains_unknown_guest_outcome_without_payload(
    monkeypatch, synthetic_ssh_lab, method, check
):
    calls = []
    payload = "synthetic-private-payload"

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        raise subprocess.TimeoutExpired(
            argv, kw["timeout"], output=payload.encode(), stderr=b"partial-output"
        )

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    kwargs = {"timeout_s": 90.0, "operation": "owned command phase"}
    if check is not None:
        kwargs["check"] = check
    with pytest.raises(lab_mod.WinRMLabError) as err:
        getattr(synthetic_ssh_lab, method)(payload, **kwargs)
    assert isinstance(err.value, lab_mod.SSHTimeoutError)
    assert len(calls) == 1 and calls[0][1]["timeout"] == 90
    assert err.value.as_dict() == {
        "host": "192.0.2.1",
        "operation": "owned command phase",
        "timeout_s": 90.0,
        "guest_state": "MAY_STILL_RUN",
        "identity_verified": False,
        "cancellation_attempted": False,
        "reconciliation_required": True,
    }
    assert "owned command phase" in str(err.value) and "90s" in str(err.value)
    assert "MAY_STILL_RUN" in str(err.value)
    assert "reconciliation required" in str(err.value)
    retained = str(err.value) + json.dumps(err.value.as_dict())
    assert payload not in retained and "partial-output" not in retained
    assert "/synthetic/not-a-key" not in retained
    assert err.value.__cause__ is None and err.value.__suppress_context__


@pytest.mark.parametrize(
    "method,phase",
    [
        ("launch_interactive", "launch interactive task"),
        ("stop_interactive", "stop interactive task"),
        ("attach_console", "attach desktop console"),
    ],
)
def test_task_preparation_retains_short_budget_and_unknown_timeout(
    monkeypatch, synthetic_ssh_lab, method, phase
):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    with pytest.raises(lab_mod.WinRMLabError) as err:
        if method == "attach_console":
            synthetic_ssh_lab.attach_console()
        else:
            getattr(synthetic_ssh_lab, method)("synthetic-owned-task")
    assert len(calls) == 1 and calls[0][1]["timeout"] == 30
    assert phase in str(err.value)
    assert err.value.as_dict()["guest_state"] == "MAY_STILL_RUN"
    assert not err.value.as_dict()["cancellation_attempted"]
    assert "synthetic-owned-task" not in str(err.value)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_command_budget_refuses_invalid_before_ssh(
    monkeypatch, synthetic_ssh_lab, timeout
):
    monkeypatch.setattr(
        lab_mod.subprocess, "run", lambda *args, **kw: pytest.fail("SSH was called")
    )
    with pytest.raises(ValueError, match="finite and positive"):
        synthetic_ssh_lab.run_cmd("synthetic-command", timeout_s=timeout)


def test_file_hash_check_retains_generic_budget(
    monkeypatch, synthetic_ssh_lab, tmp_path
):
    import hashlib

    source = tmp_path / "source.py"
    source.write_bytes(b"synthetic source")
    digest = hashlib.sha256(source.read_bytes()).hexdigest().encode()
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, digest, b"")

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    synthetic_ssh_lab.push_file(source, r"C:\synthetic\source.py")
    assert len(calls) == 1 and calls[0][1]["timeout"] == 600
    assert "Get-FileHash" in calls[0][1]["input"].decode()


def test_interpreter_discovery_retains_generic_budget(monkeypatch, synthetic_ssh_lab):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, b"C:\\synthetic\\pythonw.exe", b"")

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    assert synthetic_ssh_lab.resolve_python() == r"C:\synthetic\pythonw.exe"
    assert len(calls) == 1 and calls[0][1]["timeout"] == 600


def test_command_explicit_budget_preserves_nonzero_status(
    monkeypatch, synthetic_ssh_lab
):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 7, b"partial", b"failed")

    monkeypatch.setattr(lab_mod.subprocess, "run", fake_run)
    result = synthetic_ssh_lab.run_cmd("synthetic-command", check=False, timeout_s=90)
    assert (result.status, result.stdout, result.stderr) == (7, "partial", "failed")
    with pytest.raises(lab_mod.WinRMLabError, match="guest command exited 7"):
        synthetic_ssh_lab.run_cmd("synthetic-command", timeout_s=90)
    assert len(calls) == 2 and all(call[1]["timeout"] == 90 for call in calls)
