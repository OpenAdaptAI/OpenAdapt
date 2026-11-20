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


@pytest.mark.parametrize(
    "layout", ["renamed", "copied_folder", "both_identical", "conflicting", "missing"]
)
def test_standalone_server_requires_exact_local_helper_for_every_documented_layout(
    tmp_path, layout
):
    source = Path(server.__file__).with_name("native_value.py")
    destination = tmp_path / (
        "server.py" if layout == "copied_folder" else "win_agent_server.py"
    )
    shutil.copyfile(server.__file__, destination)
    if layout in {"renamed", "both_identical", "conflicting"}:
        shutil.copyfile(source, tmp_path / "win_agent_native_value.py")
    if layout in {"copied_folder", "both_identical"}:
        shutil.copyfile(source, tmp_path / "native_value.py")
    elif layout == "conflicting":
        (tmp_path / "native_value.py").write_text(
            "raise AssertionError('conflicting file must never execute')\n"
        )
    script = """import importlib.util,sys,hashlib
from pathlib import Path
from types import SimpleNamespace
# A same-name installed/preloaded module cannot satisfy the local-source rule.
sys.modules['win_agent_native_value'] = SimpleNamespace(PROOF_SCHEMA='poison')
sys.modules['native_value'] = SimpleNamespace(PROOF_SCHEMA='poison')
spec = importlib.util.spec_from_file_location('isolated_agent', sys.argv[1])
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)
helper = server._native_value_api()
assert Path(helper.__file__).resolve().parent == Path(sys.argv[1]).resolve().parent
assert Path(helper.__spec__.origin).resolve() == Path(helper.__file__).resolve()
assert hashlib.sha256(Path(helper.__file__).read_bytes()).hexdigest() == sys.argv[2]
assert helper.PROOF_SCHEMA == 'openadapt.native-value-completeness/v3'
value = server._form_field_value(None, lambda: 'scope')
assert not value.complete and value.value is None
assert server._native_value_api() is helper
print('exact local helper imported; incomplete observation refused')
"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            str(destination),
            hashlib.sha256(source.read_bytes()).hexdigest(),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    if layout in {"missing", "conflicting"}:
        assert result.returncode != 0
        assert (
            "exact local native-value helper"
            if layout == "missing"
            else "conflicting local native-value helpers"
        ) in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "incomplete observation refused" in result.stdout
