"""Unit tests for lazy Chromium auto-provisioning (`_browser_setup`).

These verify the developer-experience contract without ever downloading a
browser: the install subprocess is always mocked. Covered:

* no-op when the browser is already present (subprocess NOT called),
* installs exactly once when the browser is missing (subprocess called once,
  even across repeated calls),
* the ``OPENADAPT_FLOW_NO_AUTO_INSTALL`` opt-out skips the install entirely,
* a failing install surfaces an actionable error,
* importing the package triggers no install (import stays side-effect-free).
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

    def fake_run(cmd, *a, **k):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc:
        bs.ensure_chromium_installed()

    msg = str(exc.value)
    assert "playwright install chromium" in msg
    assert bs.NO_AUTO_INSTALL_ENV in msg


def test_probe_failure_falls_back_to_install(monkeypatch):
    """If the probe raises (unexpected Playwright state), install still runs."""

    def _raise():
        raise RuntimeError("driver blew up")

    monkeypatch.setattr(bs, "_chromium_present", _raise)
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
