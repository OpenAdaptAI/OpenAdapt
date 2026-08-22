"""Unit tests for lazy Chromium auto-provisioning (`_browser_setup`).

These verify the developer-experience contract without ever downloading a
browser: the install subprocess is always mocked. Covered:

* no-op when the browser is already present (subprocess NOT called),
* installs exactly once when the browser is missing (subprocess called once,
  even across repeated calls),
* the ``OPENADAPT_FLOW_NO_AUTO_INSTALL`` opt-out skips the install entirely,
* a failing install surfaces an actionable error,
* importing the package triggers no install (import stays side-effect-free),
* on Linux, missing Chromium system libraries abort BEFORE any download with
  the exact remedy (probes are monkeypatched; no network, no host state).
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

import openadapt_flow._browser_setup as bs


def test_playwright_is_browser_extra_not_base_dependency():
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())[
        "project"
    ]
    assert not any(item.startswith("playwright") for item in project["dependencies"])
    assert project["optional-dependencies"]["browser"] == ["playwright>=1.44"]


@pytest.fixture(autouse=True)
def _reset_guard(monkeypatch):
    """Reset the once-per-process guard and clear the opt-out env var so each
    test starts from a clean slate."""
    monkeypatch.setattr(bs, "_ensured", False)
    monkeypatch.delenv(bs.NO_AUTO_INSTALL_ENV, raising=False)
    yield


def test_noop_when_browser_present(monkeypatch):
    """Present browser -> probe returns True -> install NEVER runs."""
    monkeypatch.setattr(bs, "_chromium_present", lambda: True)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))

    bs.ensure_chromium_installed()

    assert calls == []


def test_presence_probe_uses_non_actuating_cli_and_completion_markers(
    monkeypatch, tmp_path
):
    """The readiness probe must not start a Playwright driver connection."""
    chromium = tmp_path / "chromium-1234"
    ffmpeg = tmp_path / "ffmpeg-1011"
    shell = tmp_path / "chromium_headless_shell-1234"
    for location in (chromium, ffmpeg, shell):
        location.mkdir()
        (location / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                f"Install location: {chromium}\n"
                f"  Install location:    {ffmpeg}\n"
                f"Install location: {shell}\n"
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert bs._chromium_present() is True
    assert calls[0][0][1:] == [
        "-m",
        "playwright",
        "install",
        "--dry-run",
        "chromium",
    ]
    assert calls[0][1]["stdout"] is subprocess.PIPE
    assert calls[0][1]["stderr"] is subprocess.STDOUT


def test_presence_probe_refuses_a_partial_install(monkeypatch, tmp_path):
    chromium = tmp_path / "chromium-1234"
    chromium.mkdir()

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 0, stdout=f"Install location: {chromium}\n"
        ),
    )

    assert bs._chromium_present() is False


def test_missing_browser_extra_refuses_before_network_or_subprocess(monkeypatch):
    """A non-browser base install gets one exact install action, not an import trace."""
    monkeypatch.setattr(bs, "browser_support_installed", lambda: False)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))

    with pytest.raises(bs.BrowserSupportMissing) as exc:
        bs.ensure_chromium_installed()

    assert "openadapt[browser]" in str(exc.value)
    assert "RDP" in str(exc.value)
    assert calls == []


def test_installs_once_when_missing(monkeypatch):
    """Missing browser -> install runs exactly once, even on repeat calls."""
    monkeypatch.setattr(bs, "_chromium_present", lambda: False)
    monkeypatch.setattr(bs, "_missing_chromium_system_libs", lambda: [])
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)

    bs.ensure_chromium_installed()
    bs.ensure_chromium_installed()  # second call is a no-op (guarded)
    bs.ensure_chromium_installed()

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[1:] == ["-m", "playwright", "install", "chromium"]


def test_opt_out_skips_install(monkeypatch):
    """OPENADAPT_FLOW_NO_AUTO_INSTALL set -> neither probe nor install runs."""
    monkeypatch.setenv(bs.NO_AUTO_INSTALL_ENV, "1")

    def _boom():
        raise AssertionError("probe must not run when opted out")

    monkeypatch.setattr(bs, "_chromium_present", _boom)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))

    bs.ensure_chromium_installed()

    assert calls == []


def test_failed_install_raises_actionable_error(monkeypatch):
    """A failing install subprocess surfaces a clear, actionable RuntimeError."""
    monkeypatch.setattr(bs, "_chromium_present", lambda: False)
    monkeypatch.setattr(bs, "_missing_chromium_system_libs", lambda: [])

    def fake_run(cmd, *a, **k):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc:
        bs.ensure_chromium_installed()

    msg = str(exc.value)
    assert "playwright install chromium" in msg
    assert bs.NO_AUTO_INSTALL_ENV in msg
    # Proxy guidance for CDN-blocked / offline machines.
    assert "HTTPS_PROXY" in msg


def test_probe_failure_falls_back_to_install(monkeypatch):
    """If the probe raises (unexpected Playwright state), install still runs."""

    def _raise():
        raise RuntimeError("driver blew up")

    monkeypatch.setattr(bs, "_chromium_present", _raise)
    monkeypatch.setattr(bs, "_missing_chromium_system_libs", lambda: [])
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: calls.append(cmd))

    bs.ensure_chromium_installed()

    assert len(calls) == 1


def test_import_is_side_effect_free(monkeypatch):
    """Importing the package must NOT trigger an install (no import-time work)."""
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append((a, k)))

    importlib.reload(importlib.import_module("openadapt_flow"))

    assert called == []


# --- Linux shared-library gate ---------------------------------------------


def test_lib_probe_is_empty_off_linux(monkeypatch):
    """Non-Linux platforms never report missing libraries."""
    monkeypatch.setattr(bs.sys, "platform", "darwin")

    def _boom(name):
        raise AssertionError("find_library must not run off Linux")

    monkeypatch.setattr(bs.ctypes.util, "find_library", _boom)

    assert bs._missing_chromium_system_libs() == []


def test_lib_probe_reports_only_missing_sonames(monkeypatch):
    """On Linux, exactly the sonames find_library cannot resolve are listed."""
    monkeypatch.setattr(bs.sys, "platform", "linux")
    present = {"nss3", "gbm"}

    def fake_find_library(name):
        return "lib{}.so.9".format(name) if name in present else None

    monkeypatch.setattr(bs.ctypes.util, "find_library", fake_find_library)

    missing = bs._missing_chromium_system_libs()

    assert set(missing) == set(bs._LINUX_CHROMIUM_SONAMES) - present
    # Deterministic order for stable error messages.
    assert missing == [s for s in bs._LINUX_CHROMIUM_SONAMES if s not in present]


def test_missing_system_libs_abort_before_any_download(monkeypatch):
    """Missing libraries -> remedy raised and NO download is attempted."""
    monkeypatch.setattr(bs, "_missing_chromium_system_libs", lambda: ["nss3", "gbm"])
    # Presence is checked before the library gate; report "missing" so the
    # install path (and therefore the gate) is reached.
    monkeypatch.setattr(bs, "_chromium_present", lambda: False)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))

    with pytest.raises(RuntimeError) as exc:
        bs.ensure_chromium_installed()

    msg = str(exc.value)
    assert "nss3" in msg
    assert "playwright install-deps chromium" in msg  # exact primary remedy
    assert "apt-get install" in msg  # apt alternative line
    assert "Nothing was downloaded" in msg
    assert calls == []


def test_present_system_libs_do_not_block_install(monkeypatch):
    """Empty probe result -> the normal download path proceeds unchanged."""
    monkeypatch.setattr(bs, "_chromium_present", lambda: False)
    monkeypatch.setattr(bs, "_missing_chromium_system_libs", lambda: [])
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **k: calls.append(cmd))

    bs.ensure_chromium_installed()

    assert len(calls) == 1
    assert calls[0][1:] == ["-m", "playwright", "install", "chromium"]


def test_opt_out_bypasses_the_library_gate(monkeypatch):
    """OPENADAPT_FLOW_NO_AUTO_INSTALL skips both the lib probe and download."""
    monkeypatch.setenv(bs.NO_AUTO_INSTALL_ENV, "1")

    def _boom():
        raise AssertionError("probe must not run when opted out")

    monkeypatch.setattr(bs, "_missing_chromium_system_libs", _boom)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))

    bs.ensure_chromium_installed()

    assert calls == []
