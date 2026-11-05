"""Doctor checks setup without performing a workflow or asking for consent."""

from __future__ import annotations

import shlex
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest
from click.testing import CliRunner
from openadapt_flow import _browser_setup

from openadapt import cli

CORE = {"openadapt_flow", "openadapt_agent", "playwright"}
MAC = {"AppKit", "Quartz", "ApplicationServices"}


def _forbidden(*args, **kwargs):
    pytest.fail("doctor performed an unrequested action")


@pytest.fixture(autouse=True)
def diagnostic_host(monkeypatch):
    monkeypatch.setattr(cli, "_python_supported", lambda: True)
    monkeypatch.setattr(cli, "_host_platform", lambda: "linux")
    monkeypatch.setattr(cli, "_warn_about_launcher_conflicts", lambda: None)
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in CORE | {"requests"} else None,
    )
    monkeypatch.setattr(_browser_setup, "_missing_chromium_system_libs", lambda: [])
    monkeypatch.setattr(_browser_setup, "_chromium_present", lambda: False)
    monkeypatch.setattr(subprocess, "run", _forbidden)


@pytest.mark.parametrize("options", [[], ["--backend", "web"]])
def test_plain_doctor_does_not_install_or_launch_a_browser(options):
    result = CliRunner().invoke(cli.main, ["doctor", *options])

    assert result.exit_code == 0, result.output
    assert "will try to download" in result.output
    assert "[INSTALL]" not in result.output


def test_explicit_install_uses_the_active_python_and_rechecks_presence(monkeypatch):
    installed = False
    calls = []

    def install(argv, **kwargs):
        nonlocal installed
        calls.append((argv, kwargs))
        installed = True

    monkeypatch.setattr(subprocess, "run", install)
    monkeypatch.setattr(_browser_setup, "_chromium_present", lambda: installed)

    result = CliRunner().invoke(cli.main, ["doctor", "--install-browser"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv == [sys.executable, "-m", "playwright", "install", "chromium"]
    assert options["check"] is True
    assert 0 < options["timeout"] <= 300
    assert not options.get("shell")
    assert "[OK] Browser" in result.output
    assert "[READY] Browser" not in result.output


def test_explicit_install_does_not_download_an_existing_browser(monkeypatch):
    monkeypatch.setattr(_browser_setup, "_chromium_present", lambda: True)

    result = CliRunner().invoke(cli.main, ["doctor", "--install-browser"])

    assert result.exit_code == 0, result.output
    assert "[OK] Browser" in result.output
    assert "[INSTALL]" not in result.output


@pytest.mark.parametrize("failure", ["exit", "timeout", "unrunnable", "still-missing"])
def test_failed_or_unconfirmed_download_is_not_reported_ready(monkeypatch, failure):
    def install(argv, **kwargs):
        if failure == "exit":
            raise subprocess.CalledProcessError(1, argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if failure == "unrunnable":
            raise OSError("cannot start installer")

    monkeypatch.setattr(subprocess, "run", install)
    result = CliRunner().invoke(cli.main, ["doctor", "--install-browser"])

    assert result.exit_code != 0
    assert "[ERROR] Browser" in result.output
    assert "-m playwright install chromium" in result.output
    assert "[READY] Browser" not in result.output
    assert "[OK] Browser" not in result.output


def test_browser_install_refuses_missing_libraries_before_any_download(monkeypatch):
    monkeypatch.setattr(
        _browser_setup, "_missing_chromium_system_libs", lambda: ["nss3"]
    )
    monkeypatch.setattr(_browser_setup, "_chromium_present", _forbidden)
    monkeypatch.setattr(sys, "executable", "/opt/Python Tools/bin/python")

    result = CliRunner().invoke(cli.main, ["doctor", "--install-browser"])

    assert result.exit_code != 0
    assert "nss3" in result.output
    command = result.output.split("Install them once: ", 1)[1].splitlines()[0]
    assert shlex.split(command) == [
        sys.executable,
        "-m",
        "playwright",
        "install-deps",
        "chromium",
    ]
    assert "[INSTALL]" not in result.output


@pytest.mark.parametrize(
    "options",
    [
        ["--backend", "macos", "--install-browser"],
        ["--recording"],
        ["--backend", "web", "--recording"],
        ["--backend", "windows", "--recording", "--install-browser"],
    ],
)
def test_invalid_options_refuse_before_any_diagnostics(monkeypatch, options):
    monkeypatch.setattr("importlib.util.find_spec", _forbidden)
    result = CliRunner().invoke(cli.main, ["doctor", *options])

    assert result.exit_code == 2, result.output
    assert "OpenAdapt System Check" not in result.output


def _mac_consent(monkeypatch, input_grant, capture_grant):
    calls = []

    def check(name, value):
        calls.append(name)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(cli, "_host_platform", lambda: "darwin")
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in CORE | MAC else None,
    )
    monkeypatch.setattr(
        "openadapt_flow.backends.remote_display.MacWindowClient",
        lambda: SimpleNamespace(
            input_trusted=lambda: check("input", input_grant),
            capture_trusted=lambda: check("capture", capture_grant),
            request_input_access=_forbidden,
            request_capture_access=_forbidden,
            capture=_forbidden,
        ),
    )
    return calls


def test_mac_permission_checks_are_read_only_and_both_grants_are_required(monkeypatch):
    calls = _mac_consent(monkeypatch, True, True)

    result = CliRunner().invoke(cli.main, ["doctor", "--backend", "macos"])

    assert result.exit_code == 0, result.output
    assert calls == ["input", "capture"]
    assert "[OK] Accessibility: permission is granted" in result.output
    assert "[OK] Screen Recording: permission is granted" in result.output


@pytest.mark.parametrize(
    "input_grant,capture_grant,missing",
    [
        (False, True, "Accessibility"),
        (True, False, "Screen Recording"),
        (None, True, "Accessibility"),
    ],
)
def test_missing_mac_consent_fails_with_settings_steps(
    monkeypatch, input_grant, capture_grant, missing
):
    calls = _mac_consent(monkeypatch, input_grant, capture_grant)

    result = CliRunner().invoke(cli.main, ["doctor", "--backend", "macos"])

    assert result.exit_code != 0
    assert calls == ["input", "capture"]
    assert f"[MISSING] {missing}" in result.output
    assert "System Settings > Privacy & Security >" in result.output
    assert "restart that app" in result.output


def test_unknown_mac_permission_state_fails_without_claiming_consent(monkeypatch):
    calls = _mac_consent(monkeypatch, RuntimeError("API unavailable"), True)

    result = CliRunner().invoke(cli.main, ["doctor", "--backend", "macos"])

    assert result.exit_code != 0
    assert calls == ["input", "capture"]
    assert "[ERROR] Accessibility: permission could not be checked" in result.output
    assert "[OK] Accessibility" not in result.output


def test_missing_mac_bindings_do_not_masquerade_as_a_permission_denial(monkeypatch):
    monkeypatch.setattr(cli, "_host_platform", lambda: "darwin")
    monkeypatch.setattr(cli, "_doctor_macos_permissions", _forbidden)

    result = CliRunner().invoke(cli.main, ["doctor", "--backend", "macos"])

    assert result.exit_code != 0
    assert "AppKit, Quartz, ApplicationServices not installed" in result.output
    assert "permission is not granted" not in result.output


def test_recording_reports_missing_capture_for_the_active_environment(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/opt/Python Tools/bin/python")

    result = CliRunner().invoke(
        cli.main, ["doctor", "--backend", "windows", "--recording"]
    )

    assert result.exit_code != 0
    assert "[MISSING] Capture" in result.output
    command = result.output.split("Capture: not installed. Run: ", 1)[1].splitlines()[0]
    assert shlex.split(command) == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "openadapt[capture,windows]",
    ]


def test_recording_prints_copyable_powershell_and_cmd_commands(monkeypatch):
    monkeypatch.setattr(cli, "_host_platform", lambda: "win32")
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Python312\python.exe")

    result = CliRunner().invoke(
        cli.main, ["doctor", "--backend", "windows", "--recording"]
    )

    assert result.exit_code != 0
    assert (
        f"PowerShell: & '{sys.executable}' -m pip install \"openadapt[capture,windows]\""
        in result.output
    )
    assert (
        f'Command Prompt: "{sys.executable}" -m pip install "openadapt[capture,windows]"'
        in result.output
    )


def test_optional_package_commands_do_not_include_trailing_prose(monkeypatch):
    monkeypatch.setattr(sys, "executable", "/opt/Python Tools/bin/python")

    result = CliRunner().invoke(cli.main, ["doctor", "--backend", "windows"])

    assert result.exit_code == 0, result.output
    commands = [
        line.split("Install with: ", 1)[1]
        for line in result.output.splitlines()
        if "(optional). Install with: " in line
    ]
    assert commands
    for command in commands:
        argv = shlex.split(command)
        assert argv[:4] == [sys.executable, "-m", "pip", "install"]
        assert argv[4].startswith("openadapt[") and argv[4].endswith("]")


class MissingFFmpeg(RuntimeError):
    pass


def _capture_modules(monkeypatch, resolve):
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: (
            object() if name in CORE | {"requests", "openadapt_capture"} else None
        ),
    )
    config = ModuleType("openadapt_capture.config")
    config.config = SimpleNamespace(
        VIDEO_FFMPEG_PATH="chosen-encoder", VIDEO_FFPROBE_PATH="chosen-probe"
    )
    video = ModuleType("openadapt_capture.video")
    video.FFmpegUnavailableError = MissingFFmpeg
    video.resolve_ffmpeg = resolve
    video.require_video_encoder = _forbidden
    monkeypatch.setitem(sys.modules, "openadapt_capture.config", config)
    monkeypatch.setitem(sys.modules, "openadapt_capture.video", video)


def test_recording_resolves_configured_encoder_without_claiming_it_works(monkeypatch):
    calls = []

    def resolve(*args):
        calls.append(args)
        return SimpleNamespace(source="explicit path")

    _capture_modules(monkeypatch, resolve)
    result = CliRunner().invoke(
        cli.main, ["doctor", "--backend", "windows", "--recording"]
    )

    assert result.exit_code == 0, result.output
    assert calls == [("chosen-encoder", "chosen-probe")]
    assert "[FOUND] FFmpeg" in result.output
    assert (
        "encoder is checked when recording starts; it was not tested here"
        in result.output
    )
    assert "[OK] FFmpeg" not in result.output


@pytest.mark.parametrize(
    "failure", [MissingFFmpeg("missing"), RuntimeError("inspection failed")]
)
def test_recording_refuses_missing_or_uninspectable_ffmpeg(monkeypatch, failure):
    def resolve(*args):
        raise failure

    _capture_modules(monkeypatch, resolve)
    result = CliRunner().invoke(
        cli.main, ["doctor", "--backend", "windows", "--recording"]
    )

    assert result.exit_code != 0
    assert "FFmpeg:" in result.output
    assert "[FOUND] FFmpeg" not in result.output
    if isinstance(failure, MissingFFmpeg):
        assert "-m openadapt_capture.cli install-ffmpeg" in result.output
    else:
        assert "could not be inspected" in result.output


def test_recording_refuses_a_broken_capture_install(monkeypatch):
    _capture_modules(monkeypatch, _forbidden)
    monkeypatch.setitem(sys.modules, "openadapt_capture.video", None)

    result = CliRunner().invoke(
        cli.main, ["doctor", "--backend", "windows", "--recording"]
    )

    assert result.exit_code != 0
    assert "[ERROR] Capture" in result.output
    assert "[FOUND] FFmpeg" not in result.output


def test_recording_on_the_wrong_native_host_does_not_import_capture(monkeypatch):
    monkeypatch.setattr(cli, "_doctor_recording_prerequisites", _forbidden)

    result = CliRunner().invoke(
        cli.main, ["doctor", "--backend", "macos", "--recording"]
    )

    assert result.exit_code != 0
    assert "[UNSUPPORTED] macos" in result.output
