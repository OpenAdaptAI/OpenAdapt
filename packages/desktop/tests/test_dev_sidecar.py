"""The development sidecar puts a runnable engine where Tauri looks for one."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import dev_sidecar


def _rustc(stdout: str):
    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess(["rustc", "-vV"], 0, stdout=stdout, stderr="")

    return run


def test_host_triple_comes_from_rustc() -> None:
    output = "rustc 1.97.0\nbinary: rustc\nhost: aarch64-apple-darwin\nrelease: 1.97.0\n"
    assert dev_sidecar.host_target_triple(run=_rustc(output)) == "aarch64-apple-darwin"


def test_missing_host_triple_stops_with_rustc_output() -> None:
    with pytest.raises(SystemExit, match="did not report a host triple"):
        dev_sidecar.host_target_triple(run=_rustc("rustc 1.97.0\n"))


def test_sidecar_name_matches_the_tauri_external_bin(tmp_path: Path) -> None:
    """Tauri appends the target triple, plus .exe on Windows, to externalBin."""
    import json

    config = json.loads(
        (Path(dev_sidecar.ROOT) / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    assert config["bundle"]["externalBin"] == [f"binaries/{dev_sidecar.SIDECAR_NAME}"]
    assert dev_sidecar.sidecar_path("x86_64-unknown-linux-gnu", tmp_path) == (
        tmp_path / "openadapt-engine-x86_64-unknown-linux-gnu"
    )
    assert dev_sidecar.sidecar_path("x86_64-pc-windows-msvc", tmp_path) == (
        tmp_path / "openadapt-engine-x86_64-pc-windows-msvc.exe"
    )


def test_launcher_requires_a_synced_venv(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="uv sync"):
        dev_sidecar.write_launcher(tmp_path / "bin" / "engine", tmp_path / "missing-venv")


@pytest.mark.skipif(os.name == "nt", reason="the launcher is a POSIX script")
def test_launcher_runs_the_engine_from_the_venv(tmp_path: Path) -> None:
    venv = tmp_path / "venv with space"
    (venv / "bin").mkdir(parents=True)
    # Stand in for the venv's interpreter: print the arguments and PATH it got.
    fake_python = venv / "bin" / "python"
    fake_python.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "print(' '.join(sys.argv[1:]))\n"
        "print(os.environ['PATH'].split(os.pathsep)[0])\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    target = dev_sidecar.write_launcher(tmp_path / "binaries" / "openadapt-engine-test", venv)

    assert os.access(target, os.X_OK)
    assert dev_sidecar.LAUNCHER_MARKER in target.read_text(encoding="utf-8")
    result = subprocess.run(
        [str(target), "doctor"], capture_output=True, text=True, check=True, timeout=30
    )
    args, first_path = result.stdout.splitlines()
    assert args == "-m openadapt_desktop doctor"
    assert first_path == str(venv / "bin")


def test_windows_needs_the_frozen_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dev_sidecar, "host_target_triple", lambda: "x86_64-pc-windows-msvc")
    with pytest.raises(SystemExit, match="--frozen"):
        dev_sidecar.main([])


def test_frozen_copies_the_ci_build(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "openadapt-engine").write_bytes(b"frozen")
        return subprocess.CompletedProcess(command, 0)

    target = tmp_path / "binaries" / "openadapt-engine-x86_64-unknown-linux-gnu"
    assert dev_sidecar.build_frozen(target, root=tmp_path, run=run) == target
    assert calls == [[sys.executable, str(tmp_path / "scripts" / "build_frozen_engine.py")]]
    assert target.read_bytes() == b"frozen"


def test_release_build_refuses_the_launcher() -> None:
    """src-tauri/build.rs stops a release build that would package the launcher.

    It finds the launcher by the same marker line, so the two must match.
    """

    build_rs = (Path(dev_sidecar.ROOT) / "src-tauri" / "build.rs").read_text(encoding="utf-8")
    assert f'"{dev_sidecar.LAUNCHER_MARKER}"' in build_rs
    assert 'env::var("PROFILE").as_deref() != Ok("release")' in build_rs
