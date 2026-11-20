"""Every standalone agent launch ships the same complete-value implementation."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.backends import parallels_vm as pv
from openadapt_flow.backends.win_agent import server, tls
from openadapt_flow.rl_env.windows_guest import WindowsGuest
from tests.test_winrm_lab import lab_mod


@pytest.mark.parametrize("route", ["parallels", "winrm", "guest"])
@pytest.mark.parametrize("missing", [False, True])
def test_actual_launch_paths_preflight_and_copy_exact_sibling(
    monkeypatch, tmp_path, route, missing
):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copyfile(server.__file__, source / "server.py")
    helper = Path(server.__file__).with_name("native_value.py")
    if not missing:
        shutil.copyfile(helper, source / "native_value.py")
    # These are path placeholders, not generated or copied signing material.
    bundle = SimpleNamespace(
        certfile=str(tmp_path / "owned" / "cert"),
        keyfile=str(tmp_path / "owned" / "key"),
        fingerprint="a" * 64,
    )
    monkeypatch.setattr(tls, "generate_self_signed_cert", lambda _: bundle)
    monkeypatch.setattr(
        pv.AgentEndpoint, "backend", lambda _: SimpleNamespace(probe=lambda: True)
    )
    calls, pushed = [], []
    if route == "parallels":
        instance = pv.ParallelsVM("synthetic-vm")
        monkeypatch.setattr(instance, "host_ip", lambda *a: "127.0.0.1")
        monkeypatch.setattr(instance, "guest_ip", lambda: "127.0.0.1")
        monkeypatch.setattr(
            instance, "_agent_server_path", lambda: str(source / "server.py")
        )
        monkeypatch.setattr(instance, "_agent_alive", lambda *a, **k: True)
        for name in ("exec", "exec_cmd", "kill_shim"):
            monkeypatch.setattr(
                instance, name, lambda *a, **k: calls.append("guest mutation")
            )

        def invoke():
            return instance.launch_agent(token="synthetic-token")
    elif route == "winrm":
        instance = object.__new__(lab_mod.WinRMLab)
        instance.host = "127.0.0.1"
        instance.profile = SimpleNamespace(guest_dir=r"C:\synthetic")
        for name in (
            "attach_console",
            "stop_interactive",
            "run_ps",
            "launch_interactive",
        ):
            monkeypatch.setattr(
                instance, name, lambda *a, **k: calls.append("guest mutation")
            )

        def invoke():
            return instance.launch_agent(
                token="synthetic-token",
                python=r"C:\pythonw.exe",
                server_path=source / "server.py",
            )
    else:
        instance = object.__new__(WindowsGuest)
        instance.host, instance.python = "127.0.0.1", r"C:\python.exe"
        monkeypatch.setattr(server, "__file__", str(source / "server.py"))
        monkeypatch.setattr(instance, "_via", lambda *a: ("127.0.0.1", 5000))
        for name in ("attach_console", "run_ps", "_interactive_script"):
            monkeypatch.setattr(
                instance, name, lambda *a, **k: calls.append("guest mutation")
            )

        def invoke():
            return instance.launch_agent(token="synthetic-token")

    monkeypatch.setattr(
        instance,
        "push_file",
        lambda local, guest, **k: pushed.append((Path(local), guest)),
    )
    if missing:
        with pytest.raises(FileNotFoundError, match="native-value helper"):
            invoke()
        assert calls == [] and pushed == []
    else:
        invoke()
        copied = [
            (local, target)
            for local, target in pushed
            if "win_agent_native_value.py" in target
        ]
        assert len(copied) == 1
        assert (
            hashlib.sha256(copied[0][0].read_bytes()).digest()
            == hashlib.sha256(helper.read_bytes()).digest()
        )


@pytest.mark.parametrize("missing", [False, True])
def test_standalone_server_requires_exact_local_helper_without_package_fallback(
    tmp_path, missing
):
    shutil.copyfile(server.__file__, tmp_path / "win_agent_server.py")
    if not missing:
        shutil.copyfile(
            Path(server.__file__).with_name("native_value.py"),
            tmp_path / "win_agent_native_value.py",
        )
    script = """import sys
sys.path.insert(0, sys.argv[1])
import win_agent_server as server
value = server._form_field_value(None, lambda: 'scope')
assert not value.complete and value.value is None
import win_agent_native_value as helper
from pathlib import Path
assert Path(helper.__file__).resolve().parent == Path(sys.argv[1]).resolve()
print('exact standalone helper imported; incomplete observation refused')
"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    result = subprocess.run(
        [sys.executable, "-S", "-c", script, str(tmp_path)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    if missing:
        assert result.returncode != 0 and "win_agent_native_value" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "incomplete observation refused" in result.stdout
