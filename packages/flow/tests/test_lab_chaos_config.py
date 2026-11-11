"""URL-only benchmark configuration controls; no fixture or network calls."""

from __future__ import annotations

from typing import Any

import pytest

from benchmark.lab_chaos import chaos
from benchmark.ui_second_read.common import base


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(chaos.ENV, raising=False)
    monkeypatch.delenv("OPENADAPT_LAB_HOST", raising=False)


@pytest.mark.parametrize("profile", [None, "none"])
def test_direct_profile_does_not_need_host(
    monkeypatch: pytest.MonkeyPatch, profile: Any
) -> None:
    if profile is not None:
        monkeypatch.setenv(chaos.ENV, profile)
    assert chaos.active_profile() is None
    assert (
        chaos.chaos_url("http://fixture.example:9635/a?q=1")
        == "http://fixture.example:9635/a?q=1"
    )


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "lab-host.invalid",
        "lab-host.invalid.",
        "LAB-HOST.INVALID.",
        "http://fixture.example",
        "user@fixture.example",
        "fixture.example:80",
        "fixture.example/path",
        "fixture.example?x=1",
        "fixture.example#x",
        "fixture example",
    ],
)
def test_requested_chaos_refuses_unconfigured_host(
    monkeypatch: pytest.MonkeyPatch, host: Any
) -> None:
    monkeypatch.setenv(chaos.ENV, "vpn-bad")
    if host is not None:
        monkeypatch.setenv("OPENADAPT_LAB_HOST", host)
    with pytest.raises(ValueError, match="OPENADAPT_LAB_HOST"):
        chaos.active_profile()
    with pytest.raises(ValueError, match="OPENADAPT_LAB_HOST"):
        chaos.chaos_url("http://fixture.example:9635/a")
    with pytest.raises(ValueError, match="OPENADAPT_LAB_HOST"):
        chaos.chaos_url("http://fixture.example:9635/a", profile="vpn-bad")


@pytest.mark.parametrize("host", ["fixture.example", "FIXTURE.EXAMPLE."])
def test_configured_host_rewrites_only_matching_app(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("OPENADAPT_LAB_HOST", host)
    monkeypatch.setenv(chaos.ENV, "vpn-bad")
    assert chaos.active_profile() == "vpn-bad"
    assert (
        chaos.chaos_url("http://fixture.example:9635/a?q=1#tab")
        == "http://fixture.example:9651/a?q=1#tab"
    )
    assert (
        chaos.chaos_url("http://other.example:9635/a") == "http://other.example:9635/a"
    )
    assert (
        chaos.chaos_url("http://fixture.example:9999/a")
        == "http://fixture.example:9999/a"
    )


def test_unknown_explicit_profile_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LAB_HOST", "fixture.example")
    with pytest.raises(ValueError, match="not a profile"):
        chaos.chaos_url("http://fixture.example:9635/a", profile="unknown")


def test_environment_chaos_refuses_before_pytest_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(chaos.ENV, "vpn-bad")

    class Config:
        def getoption(self, name: str, default: Any = None) -> Any:
            return default

    with pytest.raises(ValueError, match="OPENADAPT_LAB_HOST"):
        chaos.pytest_configure(Config())


@pytest.mark.parametrize(
    "url",
    [
        "http://lab-host.invalid:9635",
        "http://lab-host.invalid.:9635",
        "http://LAB-HOST.INVALID.:9635",
    ],
)
def test_live_benchmark_refuses_placeholder_without_network(url: str) -> None:
    with pytest.raises(ValueError, match="configure the synthetic fixture URL"):
        base(url)
