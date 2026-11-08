"""Usage stats are sent only after consent; the update check is anonymous."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from openadapt_desktop import __version__, privacy_settings, telemetry, updates
from openadapt_desktop.config import EngineConfig
from openadapt_desktop.dispatch import EngineDispatcher


@pytest.fixture
def clean_env(monkeypatch, tmp_path: Path) -> Path:
    for var in (
        "DO_NOT_TRACK",
        "OPENADAPT_TELEMETRY",
        "OPENADAPT_TELEMETRY_ENABLED",
        "OPENADAPT_UPDATE_CHECK",
        "OPENADAPT_POSTHOG_KEY",
        "OPENADAPT_POSTHOG_HOST",
    ):
        monkeypatch.delenv(var, raising=False)
    config = tmp_path / "config.toml"
    monkeypatch.setenv("OPENADAPT_CONFIG_TOML", str(config))
    return config


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    """Capture what would go to PostHog, sending inline instead of on a thread."""
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(telemetry, "_post", lambda url, payload: calls.append((url, payload)))
    monkeypatch.setattr(telemetry, "_start", lambda target: target())
    return calls


# ------------------------------------------------------------ consent


def test_nothing_is_sent_before_consent(clean_env, sent) -> None:
    assert privacy_settings.should_ask_about_usage_stats() is True
    assert telemetry.telemetry_enabled() is False
    assert telemetry.capture("app_opened", {"surface": "engine"}) is False
    assert sent == []
    assert not (clean_env.parent / "telemetry_id").exists()


def test_declining_sends_nothing_and_stops_asking(clean_env, sent) -> None:
    privacy_settings.set_usage_stats(False)
    assert privacy_settings.should_ask_about_usage_stats() is False
    assert telemetry.capture("app_opened") is False
    assert sent == []


def test_consent_is_persisted_in_the_config_file(clean_env, sent) -> None:
    clean_env.write_text('[hosted]\nhost = "https://app.openadapt.ai"\n')
    privacy_settings.set_usage_stats(True)
    data = tomllib.loads(clean_env.read_text())
    assert data["privacy"]["usage_stats"] == "share"
    assert data["hosted"]["host"] == "https://app.openadapt.ai"
    assert privacy_settings.usage_stats_choice() == "share"
    assert telemetry.capture("app_opened", {"surface": "engine"}) is True
    assert len(sent) == 1


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("DO_NOT_TRACK", "1"),
        ("OPENADAPT_TELEMETRY_ENABLED", "false"),
        ("OPENADAPT_TELEMETRY", "0"),
    ],
)
def test_environment_opt_out_wins_and_suppresses_the_question(
    clean_env, sent, monkeypatch, var, value
) -> None:
    privacy_settings.set_usage_stats(True)
    monkeypatch.setenv(var, value)
    assert privacy_settings.should_ask_about_usage_stats() is False
    assert privacy_settings.status()["usage_stats_managed_by_env"] is True
    assert telemetry.capture("app_opened") is False
    assert sent == []


def test_dnt_also_beats_an_env_opt_in(clean_env, sent, monkeypatch) -> None:
    monkeypatch.setenv("OPENADAPT_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert telemetry.capture("app_opened") is False
    assert sent == []


# ------------------------------------------------------------ payload


def test_embedded_public_key_and_host_are_used(clean_env, sent) -> None:
    privacy_settings.set_usage_stats(True)
    telemetry.capture("recording_started", {"surface": "controller"})
    url, payload = sent[0]
    assert url == "https://us.i.posthog.com/capture/"
    assert payload["api_key"] == telemetry.POSTHOG_PROJECT_KEY
    assert payload["api_key"].startswith("phc_")


def test_env_key_overrides_the_embedded_key(clean_env, sent, monkeypatch) -> None:
    privacy_settings.set_usage_stats(True)
    monkeypatch.setenv("OPENADAPT_POSTHOG_KEY", "phc_override")
    telemetry.capture("app_opened")
    assert sent[0][1]["api_key"] == "phc_override"


def test_events_carry_only_closed_properties(clean_env, sent) -> None:
    privacy_settings.set_usage_stats(True)
    telemetry.capture(
        "replay_stopped",
        {
            "surface": "dispatch",
            "outcome": "PASSED",
            "task_description": "type the patient's name",
            "error_code": "C:\\Users\\someone\\file.txt",
            "mode": "has spaces in it",
        },
    )
    payload = sent[0][1]
    props = payload["properties"]
    assert props["app_version"] == __version__
    assert props["os"] in {"macos", "windows", "linux", "other"}
    assert props["arch"]
    assert props["outcome"] == "PASSED"
    assert "task_description" not in props
    assert "error_code" not in props
    assert "mode" not in props
    assert props["$process_person_profile"] is False
    assert props["$geoip_disable"] is True
    allowed = (
        set(telemetry.base_properties())
        | telemetry.ALLOWED_PROPERTIES
        | {"$lib", "$process_person_profile", "$geoip_disable"}
    )
    assert set(props) <= allowed
    assert set(payload) == {"api_key", "event", "distinct_id", "properties"}


def test_install_id_is_random_and_stable(clean_env, sent) -> None:
    privacy_settings.set_usage_stats(True)
    telemetry.capture("app_opened")
    telemetry.capture("app_opened")
    ids = {payload["distinct_id"] for _url, payload in sent}
    assert len(ids) == 1
    (install_id,) = ids
    assert len(install_id) == 32 and int(install_id, 16) >= 0
    import getpass
    import socket

    assert socket.gethostname().lower() not in install_id
    assert getpass.getuser().lower() not in install_id


def test_unknown_events_are_dropped(clean_env, sent) -> None:
    privacy_settings.set_usage_stats(True)
    assert telemetry.capture("custom_event") is False
    assert sent == []


def test_network_failure_is_silent(clean_env, monkeypatch) -> None:
    privacy_settings.set_usage_stats(True)

    def boom(url, payload):
        raise OSError("offline")

    monkeypatch.setattr(telemetry, "_post", boom)
    monkeypatch.setattr(telemetry, "_start", lambda target: target())
    assert telemetry.capture("app_opened") is True


# ------------------------------------------------------------ updates


def _release(tag: str, *, prerelease: bool = False, draft: bool = False) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "html_url": f"https://github.com/OpenAdaptAI/OpenAdapt/releases/tag/{tag}",
    }


def test_version_compare() -> None:
    assert updates.is_newer("0.26.0", "0.25.9")
    assert updates.is_newer("1.0.0", "0.99.99")
    assert updates.is_newer("0.10.0", "0.9.0")
    assert not updates.is_newer("0.25.0", "0.25.0")
    assert not updates.is_newer("0.24.1", "0.25.0")
    assert not updates.is_newer("0.26.0-rc1", "0.25.0")
    assert not updates.is_newer("garbage", "0.25.0")


def test_newest_release_skips_other_packages_prereleases_and_drafts() -> None:
    releases = [
        _release("v9.0.0"),
        _release("openadapt-flow-v9.0.0"),
        _release("openadapt-desktop-v0.30.0", prerelease=True),
        _release("openadapt-desktop-v0.29.0", draft=True),
        _release("openadapt-desktop-v0.26.0"),
        _release("openadapt-desktop-v0.25.1"),
    ]
    newest = updates.newest_release(releases)
    assert newest == {
        "version": "0.26.0",
        "url": "https://github.com/OpenAdaptAI/OpenAdapt/releases/tag/openadapt-desktop-v0.26.0",
    }


def test_untrusted_release_url_falls_back_to_the_releases_page() -> None:
    release = _release("openadapt-desktop-v0.26.0")
    release["html_url"] = "https://evil.example/download"
    newest = updates.newest_release([release])
    assert newest["url"].startswith(updates.RELEASES_PAGE + "/tag/")


def test_check_reports_an_available_update_and_caches_it(clean_env) -> None:
    result = updates.check_now(
        "0.25.0", fetch=lambda: [_release("openadapt-desktop-v0.26.0")], now=1000.0
    )
    assert result["available"] is True
    assert result["latest_version"] == "0.26.0"
    assert updates.status("0.25.0")["available"] is True
    assert updates.status("0.26.0")["available"] is False
    assert updates.is_due(now=1000.0 + 60) is False
    assert updates.is_due(now=1000.0 + updates.CHECK_INTERVAL_S) is True


def test_failed_check_waits_a_day_and_keeps_the_last_answer(clean_env) -> None:
    updates.check_now("0.25.0", fetch=lambda: [_release("openadapt-desktop-v0.26.0")], now=1.0)

    def offline():
        raise OSError("offline")

    result = updates.check_now("0.25.0", fetch=offline, now=2.0)
    assert result["latest_version"] == "0.26.0"
    assert updates.is_due(now=3.0) is False


@pytest.mark.parametrize("how", ["env", "setting"])
def test_update_check_can_be_turned_off(clean_env, monkeypatch, how) -> None:
    if how == "env":
        monkeypatch.setenv("OPENADAPT_UPDATE_CHECK", "false")
    else:
        privacy_settings.set_update_check(False)
    calls: list[int] = []

    def fetch():
        calls.append(1)
        return [_release("openadapt-desktop-v9.0.0")]

    assert updates.check_in_background("0.25.0", fetch=fetch, force=True) is False
    result = updates.check_now("0.25.0", fetch=fetch)
    assert calls == []
    assert result["enabled"] is False
    assert result["available"] is False


def test_dnt_does_not_turn_off_the_update_check(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert privacy_settings.update_check_enabled() is True


def test_update_request_carries_no_identifiers(clean_env, monkeypatch, sent) -> None:
    privacy_settings.set_usage_stats(True)
    install_id = telemetry.install_id()
    seen: dict[str, Any] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, Any]]:
            return [_release("openadapt-desktop-v0.26.0")]

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    updates.check_now("0.25.0")
    assert seen["url"] == "https://api.github.com/repos/OpenAdaptAI/OpenAdapt/releases"
    assert set(seen) == {"url", "params", "headers", "timeout", "follow_redirects"}
    assert seen["params"] == {"per_page": "50"}
    assert set(seen["headers"]) == {"Accept", "User-Agent"}
    blob = repr(seen)
    assert install_id not in blob
    assert __version__ not in blob
    assert "0.25.0" not in blob
    assert "Authorization" not in blob


def test_update_check_event_needs_consent(clean_env, sent) -> None:
    fetch = lambda: [_release("openadapt-desktop-v0.26.0")]  # noqa: E731
    updates.check_now("0.25.0", fetch=fetch, now=1.0)
    assert sent == []

    privacy_settings.set_usage_stats(True)
    updates.check_now("0.25.0", fetch=fetch, now=2.0 + updates.CHECK_INTERVAL_S)
    assert [payload["event"] for _url, payload in sent] == ["update_check"]
    props = sent[0][1]["properties"]
    assert props["update_available"] is True
    assert {"app_version", "os", "arch"} <= set(props)


# ------------------------------------------------------------ engine commands


def test_engine_commands_round_trip(clean_env, sent, tmp_path: Path) -> None:
    disp = EngineDispatcher(EngineConfig(data_dir=tmp_path / "data"))
    first = disp.dispatch("get_privacy_settings", {})
    assert first["ask_usage_stats"] is True and first["usage_stats"] is None
    assert disp.dispatch("set_usage_stats", {"share": "yes"})["ok"] is False

    answered = disp.dispatch("set_usage_stats", {"share": True})
    assert answered["ok"] is True and answered["ask_usage_stats"] is False
    # The launch event skipped before consent is counted once, on consent.
    assert [payload["event"] for _url, payload in sent] == ["app_opened"]

    off = disp.dispatch("set_update_check", {"enabled": False})
    assert off["ok"] is True and off["update_check"] is False
    status = disp.dispatch("get_update_status", {})
    assert status["enabled"] is False and status["available"] is False


def test_privacy_commands_stay_off_the_local_socket() -> None:
    from openadapt_desktop import socket_server

    exposed = (
        socket_server._TRAY_COMMANDS
        | socket_server._LOCAL_AGENT_COMMANDS
        | socket_server._SETUP_AGENT_COMMANDS
    )
    assert not exposed & {"set_usage_stats", "set_update_check"}


def test_openadapt_telemetry_zero_overrides_saved_share(clean_env, sent, monkeypatch) -> None:
    """OPENADAPT_TELEMETRY=0 must force disabled/zero send even with saved Share."""
    privacy_settings.set_usage_stats(True)
    monkeypatch.setenv("OPENADAPT_TELEMETRY", "0")
    enabled = telemetry.telemetry_enabled()
    queued = telemetry.capture("app_opened")
    assert (enabled, queued, len(sent)) == (False, False, 0)
