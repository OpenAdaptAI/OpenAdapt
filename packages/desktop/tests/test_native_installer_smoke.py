from __future__ import annotations

import os
import plistlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import smoke_test_native_installer as smoke


def _completed(
    args: object, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def _artifact(path: Path, signature: bytes) -> Path:
    path.write_bytes(signature + b"installer-payload")
    return path


def _dmg(path: Path) -> Path:
    payload = bytearray(1_024)
    payload[-512:-508] = b"koly"
    path.write_bytes(payload)
    return path


def _pe(path: Path, machine: int) -> Path:
    payload = bytearray(70)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 64)
    payload[64:68] = b"PE\x00\x00"
    struct.pack_into("<H", payload, 68, machine)
    path.write_bytes(payload)
    return path


def _elf(path: Path, machine: int, byte_order: int = 1) -> Path:
    payload = bytearray(20)
    payload[:6] = b"\x7fELF\x02" + bytes([byte_order])
    struct.pack_into("<H" if byte_order == 1 else ">H", payload, 18, machine)
    path.write_bytes(payload)
    return path


def _macos_app(path: Path) -> Path:
    executable = path / "Contents" / "MacOS" / "openadapt-desktop"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"native-app")
    executable.chmod(0o755)
    engine = executable.with_name("openadapt-engine")
    engine.write_bytes(b"frozen-engine")
    engine.chmod(0o755)
    with (path / "Contents" / "Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleExecutable": "openadapt-desktop",
                "CFBundleIdentifier": "ai.openadapt.desktop",
            },
            stream,
        )
    return path


def test_dmg_is_mounted_copied_verified_removed_and_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _dmg(tmp_path / "OpenAdapt.dmg")
    installed_app = tmp_path / "installed" / "OpenAdapt Desktop.app"
    commands: list[list[str]] = []

    def fake_run(
        args: object, *, timeout: float, ok_returncodes: object = (0,)
    ) -> subprocess.CompletedProcess[str]:
        command = [os.fspath(item) for item in args]  # type: ignore[union-attr]
        commands.append(command)
        if command[:2] == ["hdiutil", "attach"]:
            _macos_app(Path(command[-1]) / installed_app.name)
        elif command[0] == "ditto":
            shutil.copytree(command[1], command[2])
        elif command[0].endswith("openadapt-engine"):
            return _completed(command, stdout="openadapt-flow 1.19.0\n")
        return _completed(command)

    monkeypatch.setattr(smoke, "run_command", fake_run)

    result = smoke.smoke_test_installer(artifact, installed_app, platform_value="darwin", timeout=1)

    assert result.installer_kind == "dmg"
    assert not installed_app.exists()
    assert commands[0][:2] == ["hdiutil", "attach"]
    assert commands[1][0] == "ditto"
    assert commands[2][-2:] == ["__openadapt_flow__", "--version"]
    assert commands[3][:2] == ["hdiutil", "detach"]


def test_adhoc_mode_verifies_installed_app_and_asserts_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _macos_app(tmp_path / "OpenAdapt Desktop.app")
    app_path = tmp_path / "installed" / "OpenAdapt Desktop.app"
    commands: list[list[str]] = []

    def fake_run(
        args: object, *, timeout: float, ok_returncodes: object = (0,)
    ) -> subprocess.CompletedProcess[str]:
        command = [os.fspath(item) for item in args]  # type: ignore[union-attr]
        commands.append(command)
        if command[0] == "ditto":
            shutil.copytree(command[1], command[2])
        if command[:3] == ["codesign", "--display", "--verbose=4"]:
            return _completed(command, stderr="Signature=adhoc\nTeamIdentifier=not set\n")
        if command[0].endswith("openadapt-engine"):
            return _completed(command, stdout="openadapt-flow 1.19.0\n")
        return _completed(command)

    monkeypatch.setattr(smoke, "run_command", fake_run)

    result = smoke.smoke_test_installer(
        artifact,
        app_path,
        signing_mode="adhoc",
        platform_value="darwin",
        timeout=1,
    )

    assert result.signing_mode == "adhoc"
    assert any(command[:2] == ["codesign", "--verify"] for command in commands)
    assert any(command[:2] == ["codesign", "--display"] for command in commands)
    assert not app_path.exists()


def test_developer_id_mode_checks_codesign_gatekeeper_and_stapled_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _macos_app(tmp_path / "OpenAdapt Desktop.app")
    app_path = tmp_path / "installed" / "OpenAdapt Desktop.app"
    commands: list[list[str]] = []

    def fake_run(
        args: object, *, timeout: float, ok_returncodes: object = (0,)
    ) -> subprocess.CompletedProcess[str]:
        command = [os.fspath(item) for item in args]  # type: ignore[union-attr]
        commands.append(command)
        if command[0] == "ditto":
            shutil.copytree(command[1], command[2])
        if command[:3] == ["codesign", "--display", "--verbose=4"]:
            return _completed(
                command,
                stderr=(
                    "Authority=Developer ID Application: OpenAdapt AI (TEAMID1234)\n"
                    "TeamIdentifier=TEAMID1234\n"
                ),
            )
        if command[0].endswith("openadapt-engine"):
            return _completed(command, stdout="openadapt-flow 1.19.0\n")
        return _completed(command)

    monkeypatch.setattr(smoke, "run_command", fake_run)

    result = smoke.smoke_test_installer(
        artifact,
        app_path,
        signing_mode="developer-id-notarized",
        platform_value="darwin",
        timeout=1,
    )

    assert result.signing_mode == "developer-id-notarized"
    assert sum(command[0] == "codesign" for command in commands) == 4
    assert sum(command[0] == "spctl" for command in commands) == 2
    assert ["xcrun", "stapler", "validate", os.fspath(artifact)] in commands
    assert not app_path.exists()


def test_msi_uses_quiet_native_install_and_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _artifact(tmp_path / "OpenAdapt.msi", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    app_path = tmp_path / "programs" / "OpenAdapt Desktop.exe"
    commands: list[list[str]] = []

    def fake_run(
        args: object, *, timeout: float, ok_returncodes: object = (0,)
    ) -> subprocess.CompletedProcess[str]:
        command = [os.fspath(item) for item in args]  # type: ignore[union-attr]
        commands.append(command)
        if "/i" in command:
            app_path.parent.mkdir(parents=True)
            app_path.write_bytes(b"MZapp")
        if "/x" in command:
            app_path.unlink()
        return _completed(command)

    monkeypatch.setattr(smoke, "run_command", fake_run)

    result = smoke.smoke_test_installer(
        artifact,
        app_path,
        allow_system_install=True,
        platform_value="win32",
        timeout=1,
    )

    assert result.installer_kind == "msi"
    assert commands == [
        ["msiexec.exe", "/i", os.fspath(artifact), "/qn", "/norestart"],
        ["msiexec.exe", "/x", os.fspath(artifact), "/qn", "/norestart"],
    ]
    assert not app_path.exists()


def test_authenticode_mode_checks_installer_and_installed_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _artifact(tmp_path / "OpenAdapt.msi", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    app_path = tmp_path / "programs" / "OpenAdapt Desktop.exe"
    thumbprint = "A1" * 20
    commands: list[list[str]] = []

    def fake_run(
        args: object, *, timeout: float, ok_returncodes: object = (0,)
    ) -> subprocess.CompletedProcess[str]:
        command = [os.fspath(item) for item in args]  # type: ignore[union-attr]
        commands.append(command)
        if command[0] == "powershell.exe":
            return _completed(command, stdout=f"VALIDSIGNER={thumbprint}\n")
        if "/i" in command:
            app_path.parent.mkdir(parents=True)
            app_path.write_bytes(b"MZapp")
        if "/x" in command:
            app_path.unlink()
        return _completed(command)

    monkeypatch.setattr(smoke, "run_command", fake_run)

    result = smoke.smoke_test_installer(
        artifact,
        app_path,
        allow_system_install=True,
        signing_mode="authenticode",
        signing_fingerprint=thumbprint.lower(),
        platform_value="win32",
        timeout=1,
    )

    powershell_commands = [command for command in commands if command[0] == "powershell.exe"]
    assert result.signing_mode == "authenticode"
    assert len(powershell_commands) == 2
    assert os.fspath(artifact) in powershell_commands[0]
    assert os.fspath(app_path) in powershell_commands[1]
    assert thumbprint in powershell_commands[0]
    assert not app_path.exists()


def test_msi_install_failure_still_attempts_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _artifact(tmp_path / "OpenAdapt.msi", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    app_path = tmp_path / "OpenAdapt Desktop.exe"
    commands: list[list[str]] = []

    def fake_run(
        args: object, *, timeout: float, ok_returncodes: object = (0,)
    ) -> subprocess.CompletedProcess[str]:
        command = [os.fspath(item) for item in args]  # type: ignore[union-attr]
        commands.append(command)
        if "/i" in command:
            raise smoke.SmokeTestError("synthetic install failure")
        return _completed(command, returncode=1605)

    monkeypatch.setattr(smoke, "run_command", fake_run)

    with pytest.raises(smoke.SmokeTestError, match="synthetic install failure"):
        smoke.smoke_test_installer(
            artifact,
            app_path,
            allow_system_install=True,
            platform_value="win32",
            timeout=1,
        )

    assert any("/x" in command for command in commands)


def test_nsis_requires_and_executes_declared_uninstaller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _artifact(tmp_path / "OpenAdapt-setup.exe", b"MZ")
    app_path = tmp_path / "OpenAdapt" / "OpenAdapt Desktop.exe"
    uninstaller = tmp_path / "OpenAdapt" / "uninstall.exe"
    commands: list[list[str]] = []

    def fake_run(
        args: object, *, timeout: float, ok_returncodes: object = (0,)
    ) -> subprocess.CompletedProcess[str]:
        command = [os.fspath(item) for item in args]  # type: ignore[union-attr]
        commands.append(command)
        if Path(command[0]) == artifact:
            app_path.parent.mkdir(parents=True)
            app_path.write_bytes(b"MZapp")
            uninstaller.write_bytes(b"MZuninstaller")
        elif Path(command[0]) == uninstaller:
            app_path.unlink()
            uninstaller.unlink()
        return _completed(command)

    monkeypatch.setattr(smoke, "run_command", fake_run)

    result = smoke.smoke_test_installer(
        artifact,
        app_path,
        uninstaller_path_value=uninstaller,
        allow_system_install=True,
        platform_value="win32",
        timeout=1,
    )

    assert result.installer_kind == "nsis"
    assert commands == [[os.fspath(artifact), "/S"], [os.fspath(uninstaller), "/S"]]
    assert not app_path.exists()
    assert not uninstaller.exists()


def test_deb_checks_package_state_and_removes_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _artifact(tmp_path / "openadapt-desktop.deb", b"!<arch>\n")
    app_path = tmp_path / "usr" / "bin" / "openadapt-desktop"
    installed = False
    commands: list[list[str]] = []

    def fake_run(
        args: object, *, timeout: float, ok_returncodes: object = (0,)
    ) -> subprocess.CompletedProcess[str]:
        nonlocal installed
        command = [os.fspath(item) for item in args]  # type: ignore[union-attr]
        commands.append(command)
        if command[0] == "dpkg-deb":
            return _completed(command, stdout="openadapt-desktop\n")
        if command[0] == "dpkg-query":
            return _completed(command, 0 if installed else 1, "ii " if installed else "")
        if command[-2:] == ["dpkg", "--install"]:
            raise AssertionError("artifact argument must follow --install")
        if "--install" in command:
            installed = True
            app_path.parent.mkdir(parents=True)
            app_path.write_bytes(b"ELF app")
            app_path.chmod(0o755)
        elif "--remove" in command:
            installed = False
            app_path.unlink()
        return _completed(command)

    monkeypatch.setattr(smoke, "run_command", fake_run)
    monkeypatch.setattr(smoke, "_sudo_prefix", lambda: ["sudo", "--non-interactive"])

    result = smoke.smoke_test_installer(
        artifact,
        app_path,
        allow_system_install=True,
        platform_value="linux",
        timeout=1,
    )

    assert result.installer_kind == "deb"
    assert any("--install" in command for command in commands)
    assert any("--remove" in command for command in commands)
    assert not installed
    assert not app_path.exists()


def test_appimage_copy_install_is_executable_then_removed(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path / "OpenAdapt.AppImage", b"\x7fELF")
    app_path = tmp_path / "installed" / "OpenAdapt.AppImage"

    result = smoke.smoke_test_installer(artifact, app_path, platform_value="linux", timeout=1)

    assert result.installer_kind == "appimage"
    assert not app_path.exists()


@pytest.mark.parametrize(
    ("builder", "machine", "expected"),
    [
        (_pe, 0x8664, "x86_64"),
        (_pe, 0xAA64, "arm64"),
        (_elf, 62, "x86_64"),
        (_elf, 183, "arm64"),
    ],
)
def test_pe_and_elf_architecture_parsing(
    tmp_path: Path,
    builder: object,
    machine: int,
    expected: str,
) -> None:
    executable = builder(tmp_path / "executable", machine)  # type: ignore[operator]

    detected = (
        smoke._pe_architecture(executable)
        if builder is _pe
        else smoke._elf_architecture(executable)
    )

    assert detected == expected


def test_architecture_mismatch_fails_and_removes_installed_copy(tmp_path: Path) -> None:
    artifact = _elf(tmp_path / "OpenAdapt.AppImage", 62)
    app_path = tmp_path / "installed" / "OpenAdapt.AppImage"

    with pytest.raises(smoke.SmokeTestError, match="architecture mismatch"):
        smoke.smoke_test_installer(
            artifact,
            app_path,
            expected_architecture="arm64",
            platform_value="linux",
            timeout=1,
        )

    assert not app_path.exists()


def test_gpg_mode_rejects_raw_appimage_verification(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path / "OpenAdapt.AppImage", b"\x7fELF")

    with pytest.raises(smoke.SmokeTestError, match="pinned AppImage validator"):
        smoke.smoke_test_installer(
            artifact,
            tmp_path / "installed" / "OpenAdapt.AppImage",
            signing_mode="gpg",
            signing_fingerprint="A1" * 20,
            platform_value="linux",
        )


@pytest.mark.parametrize(
    ("platform", "name", "signature"),
    [
        ("darwin", "OpenAdapt.exe", b"MZ"),
        ("win32", "OpenAdapt.deb", b"!<arch>\n"),
        ("linux", "OpenAdapt.msi", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    ],
)
def test_rejects_cross_platform_artifact(
    tmp_path: Path, platform: str, name: str, signature: bytes
) -> None:
    artifact = _artifact(tmp_path / name, signature)

    with pytest.raises(smoke.SmokeTestError, match="not a"):
        smoke.smoke_test_installer(artifact, tmp_path / "unused-app", platform_value=platform)


def test_refuses_existing_application_without_running_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _artifact(tmp_path / "OpenAdapt.AppImage", b"\x7fELF")
    app_path = tmp_path / "existing.AppImage"
    app_path.write_bytes(b"do-not-delete")
    monkeypatch.setattr(
        smoke,
        "run_command",
        lambda *args, **kwargs: pytest.fail("installer command should not run"),
    )

    with pytest.raises(smoke.SmokeTestError, match="refusing to overwrite"):
        smoke.smoke_test_installer(artifact, app_path, platform_value="linux", timeout=1)

    assert app_path.read_bytes() == b"do-not-delete"


def test_system_installer_requires_explicit_disposable_runner_opt_in(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path / "OpenAdapt.deb", b"!<arch>\n")

    with pytest.raises(smoke.SmokeTestError, match="--allow-system-install"):
        smoke.smoke_test_installer(
            artifact,
            tmp_path / "usr" / "bin" / "openadapt-desktop",
            platform_value="linux",
        )


def test_app_path_must_be_absolute(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path / "OpenAdapt.AppImage", b"\x7fELF")

    with pytest.raises(smoke.SmokeTestError, match="must be an absolute path"):
        smoke.smoke_test_installer(artifact, Path("OpenAdapt.AppImage"), platform_value="linux")


def _fake_launcher(tmp_path: Path, name: str, python_body: str) -> Path:
    """A tiny executable whose behavior stands in for the installed GUI app."""

    script = tmp_path / f"{name}_body.py"
    script.write_text(python_body)
    if os.name == "nt":
        launcher = tmp_path / f"{name}.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}"\r\nexit /b %errorlevel%\r\n'
        )
    else:
        launcher = tmp_path / name
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
        launcher.chmod(0o755)
    return launcher


def test_launch_probe_accepts_a_process_that_stays_alive(tmp_path: Path) -> None:
    launcher = _fake_launcher(tmp_path, "healthy", "import time\ntime.sleep(60)\n")

    smoke._launch_probe(launcher, run_seconds=1.0, timeout=30.0)


def test_launch_probe_rejects_a_startup_crash_and_retries_once(tmp_path: Path) -> None:
    attempts = tmp_path / "attempts.log"
    body = (
        "import pathlib, sys\n"
        f"path = pathlib.Path({os.fspath(attempts)!r})\n"
        "with path.open('a') as stream:\n"
        "    stream.write('attempt\\n')\n"
        "sys.stderr.write('PluginInitialization updater invalid type: null')\n"
        "sys.exit(101)\n"
    )
    launcher = _fake_launcher(tmp_path, "crashing", body)

    with pytest.raises(smoke.SmokeTestError) as excinfo:
        smoke._launch_probe(launcher, run_seconds=1.0, timeout=30.0)

    message = str(excinfo.value)
    assert "exited with status 101" in message
    assert "PluginInitialization" in message
    assert attempts.read_text().count("attempt") == 2


def test_launch_probe_reports_an_unlaunchable_executable(tmp_path: Path) -> None:
    with pytest.raises(smoke.SmokeTestError, match="could not launch"):
        smoke._launch_probe(tmp_path / "absent", run_seconds=1.0, timeout=5.0)


def test_launch_seconds_probe_targets_the_installed_bundle_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _dmg(tmp_path / "OpenAdapt.dmg")
    installed_app = tmp_path / "installed" / "OpenAdapt Desktop.app"
    launched: list[Path] = []

    def fake_run(
        args: object, *, timeout: float, ok_returncodes: object = (0,)
    ) -> subprocess.CompletedProcess[str]:
        command = [os.fspath(item) for item in args]  # type: ignore[union-attr]
        if command[:2] == ["hdiutil", "attach"]:
            _macos_app(Path(command[-1]) / installed_app.name)
        elif command[0] == "ditto":
            shutil.copytree(command[1], command[2])
        elif command[0].endswith("openadapt-engine"):
            return _completed(command, stdout="openadapt-flow 1.19.0\n")
        return _completed(command)

    def fake_probe(
        executable: Path, *, run_seconds: float, timeout: float, attempts: int = 2
    ) -> None:
        assert run_seconds == 15.0
        launched.append(executable)

    monkeypatch.setattr(smoke, "run_command", fake_run)
    monkeypatch.setattr(smoke, "_launch_probe", fake_probe)

    smoke.smoke_test_installer(
        artifact,
        installed_app,
        platform_value="darwin",
        timeout=1,
        launch_seconds=15.0,
    )

    assert launched == [installed_app / "Contents" / "MacOS" / "openadapt-desktop"]


def test_negative_launch_seconds_is_rejected(tmp_path: Path) -> None:
    artifact = _dmg(tmp_path / "OpenAdapt.dmg")

    with pytest.raises(smoke.SmokeTestError, match="launch seconds"):
        smoke.smoke_test_installer(
            artifact,
            tmp_path / "installed" / "OpenAdapt Desktop.app",
            platform_value="darwin",
            launch_seconds=-1.0,
        )
