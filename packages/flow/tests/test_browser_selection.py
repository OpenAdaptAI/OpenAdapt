"""Browser selection: installed Chrome or Edge first, a small download last."""

from __future__ import annotations

from typing import Any

import pytest

from openadapt_flow import _browser_setup as bs


@pytest.fixture(autouse=True)
def _driver_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bs, "require_browser_support", lambda: None)
    monkeypatch.delenv(bs.BROWSER_ENV, raising=False)
    monkeypatch.delenv(bs.NO_AUTO_INSTALL_ENV, raising=False)
    bs._bundled_ready.clear()


def test_auto_prefers_installed_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bs, "installed_browser_channel", lambda: "chrome")
    monkeypatch.setattr(
        bs, "ensure_bundled_chromium", lambda **_: pytest.fail("no download")
    )
    assert bs.chromium_launch_options(headless=True) == {"channel": "chrome"}


def test_auto_without_installed_browser_gets_headless_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bs, "installed_browser_channel", lambda: None)
    monkeypatch.setattr(bs, "_bundled_present", lambda *, only_shell: False)
    monkeypatch.setattr(bs, "_install_headless_shell", lambda: calls.append("shell"))
    monkeypatch.setattr(bs, "ensure_chromium_installed", lambda: calls.append("full"))
    assert bs.chromium_launch_options(headless=True) == {}
    assert bs.chromium_launch_options(headless=True) == {}
    assert calls == ["shell"]


def test_headed_needs_full_build(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bs, "installed_browser_channel", lambda: None)
    monkeypatch.setattr(bs, "ensure_chromium_installed", lambda: calls.append("full"))
    assert bs.chromium_launch_options(headless=False) == {}
    assert calls == ["full"]


def test_bundled_env_skips_installed_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(bs.BROWSER_ENV, "bundled")
    monkeypatch.setattr(bs, "installed_browser_channel", lambda: "chrome")
    monkeypatch.setattr(bs, "ensure_bundled_chromium", lambda **_: None)
    assert bs.chromium_launch_options(headless=True) == {}


def test_unknown_choice_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(bs.BROWSER_ENV, "firefox")
    with pytest.raises(RuntimeError, match="not one of"):
        bs.chromium_launch_options(headless=True)


class _Chromium:
    def __init__(self, fail_channel: bool) -> None:
        self.fail_channel = fail_channel
        self.calls: list[dict[str, Any]] = []

    def launch(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if "channel" in kwargs and self.fail_channel:
            raise RuntimeError("policy blocked")
        return "browser"


def test_broken_channel_falls_back_to_bundled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bs, "installed_browser_channel", lambda: "msedge")
    monkeypatch.setattr(bs, "ensure_bundled_chromium", lambda **_: None)
    chromium = _Chromium(fail_channel=True)
    assert bs.launch_chromium(chromium, headless=True) == "browser"
    assert chromium.calls == [
        {"headless": True, "channel": "msedge"},
        {"headless": True},
    ]


def test_explicit_channel_failure_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(bs.BROWSER_ENV, "chrome")
    with pytest.raises(RuntimeError, match="policy blocked"):
        bs.launch_chromium(_Chromium(fail_channel=True), headless=True)


def test_installed_channel_probe_uses_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    app = tmp_path / "Edge"
    app.mkdir()
    monkeypatch.setattr(
        bs,
        "_INSTALLED_BROWSER_PATHS",
        {
            "chrome": {"darwin": (), "linux": (), "win32": ()},
            "msedge": {
                "darwin": (str(app),),
                "linux": (str(app),),
                "win32": (str(app),),
            },
        },
    )
    assert bs.installed_browser_channel() == "msedge"
