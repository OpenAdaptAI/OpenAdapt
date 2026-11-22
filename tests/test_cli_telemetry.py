"""Tests for unified zero-dependency CLI telemetry (openadapt.telemetry)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from openadapt import telemetry
from openadapt.cli import main


@pytest.fixture
def clean_env(monkeypatch, tmp_path: Path) -> Path:
    for var in (
        "DO_NOT_TRACK",
        "OPENADAPT_TELEMETRY",
        "OPENADAPT_TELEMETRY_ENABLED",
        "OPENADAPT_POSTHOG_KEY",
        "OPENADAPT_POSTHOG_HOST",
        "OPENADAPT_INTERNAL",
        "OPENADAPT_DEV",
        "CI",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "JENKINS_URL",
        "BUILDKITE",
        "CIRCLECI",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("OPENADAPT_CONFIG_TOML", str(cfg))
    return cfg


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        telemetry, "_post", lambda url, payload: calls.append((url, payload))
    )
    monkeypatch.setattr(telemetry, "_start", lambda target: target())
    return calls


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("DO_NOT_TRACK", "1"),
        ("OPENADAPT_TELEMETRY", "0"),
        ("OPENADAPT_TELEMETRY", "false"),
        ("OPENADAPT_TELEMETRY_ENABLED", "false"),
    ],
)
def test_opt_out_env_vars_send_zero_requests(
    clean_env: Path,
    sent: list[tuple[str, dict[str, Any]]],
    monkeypatch,
    var: str,
    value: str,
) -> None:
    monkeypatch.setenv(var, value)
    assert telemetry.telemetry_enabled() is False
    assert (
        telemetry.capture_cli_command("quickstart", exit_code=0, duration_ms=120.0)
        is False
    )
    assert telemetry.capture_quickstart_completed(ok=True, duration_ms=120.0) is False
    assert sent == []
    assert not (clean_env.parent / "telemetry_id").exists()


def test_config_toml_off_disables_cli_telemetry(
    clean_env: Path, sent: list[tuple[str, dict[str, Any]]]
) -> None:
    clean_env.write_text('[privacy]\nusage_stats = "off"\n', encoding="utf-8")
    assert telemetry.telemetry_enabled() is False
    assert telemetry.capture_cli_command("doctor", exit_code=0, duration_ms=50) is False
    assert sent == []


def test_sanitizer_strips_non_allowlisted_and_pii_keys_fail_closed(
    clean_env: Path, sent: list[tuple[str, dict[str, Any]]]
) -> None:
    ok = telemetry.capture(
        "cli_command_run",
        {
            "command": "quickstart",
            "exit_code": 0,
            "duration_ms_bucket": "100ms_1s",
            # Disallowed / PII / path / URL keys must be dropped
            "args": "--url https://secret.internal/patient/123",
            "path": "/Users/alice/private.json",
            "email": "alice@example.com",
            "window_title": "Epic EHR - John Doe",
            # Disallowed free-text value on an allowed key must also be dropped
            "outcome": "has spaces / invalid token!",
        },
    )
    assert ok is True
    assert len(sent) == 1
    url, payload = sent[0]
    assert url == "https://us.i.posthog.com/capture/"
    assert payload["api_key"] == telemetry.POSTHOG_PROJECT_KEY
    props = payload["properties"]
    assert props["command"] == "quickstart"
    assert props["exit_code"] == 0
    assert props["duration_ms_bucket"] == "100ms_1s"
    assert props["$process_person_profile"] is False
    assert props["$geoip_disable"] is True
    assert "python_minor" in props
    assert "version" in props
    assert "os" in props
    assert "arch" in props
    assert "is_ci" in props
    assert "is_internal" in props
    for forbidden in ("args", "path", "email", "window_title", "outcome"):
        assert forbidden not in props


def test_unknown_event_and_unknown_command_fail_closed(
    clean_env: Path, sent: list[tuple[str, dict[str, Any]]]
) -> None:
    assert telemetry.capture("arbitrary_event", {"command": "doctor"}) is False
    assert sent == []

    assert (
        telemetry.capture_cli_command(
            "/secret/path/cmd", exit_code=2, duration_ms=1500.0
        )
        is True
    )
    assert sent[0][1]["properties"]["command"] == "other"
    assert sent[0][1]["properties"]["duration_ms_bucket"] == "1s_10s"


def test_cli_wrapper_never_blocks_or_raises_on_network_failure(
    clean_env: Path, monkeypatch
) -> None:
    called: list[str] = []

    def _boom(url: str, payload: dict[str, Any]) -> None:
        called.append(payload["event"])
        raise OSError("network unreachable")

    monkeypatch.setattr(telemetry, "_post", _boom)
    monkeypatch.setattr(telemetry, "_start", lambda target: target())

    runner = CliRunner()
    res = runner.invoke(main, ["version"])
    assert res.exit_code == 0
    assert "cli_command_run" in called


def test_cli_wrapper_emits_quickstart_completed_event(
    clean_env: Path, sent: list[tuple[str, dict[str, Any]]], monkeypatch
) -> None:
    from openadapt import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_quickstart_more", lambda out, app: None)
    runner = CliRunner()
    res = runner.invoke(main, ["quickstart", "--more"])
    assert res.exit_code == 0
    events = [payload["event"] for _url, payload in sent]
    assert "cli_command_run" in events
    assert "cli_quickstart_completed" in events
    qs_payload = next(
        p for _u, p in sent if p["event"] == "cli_quickstart_completed"
    )
    assert qs_payload["properties"]["outcome"] == "ok"
