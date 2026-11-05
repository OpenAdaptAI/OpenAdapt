"""Opt-in desktop telemetry stays off by default and sanitizes payloads."""

from __future__ import annotations

import json
from pathlib import Path

import openadapt_desktop.telemetry as telemetry


def test_telemetry_disabled_by_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENADAPT_TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setenv("OPENADAPT_TELEMETRY_CONFIG", str(tmp_path / "missing.json"))
    assert telemetry.telemetry_enabled() is False


def test_dnt_overrides_opt_in(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENADAPT_TELEMETRY_ENABLED", "true")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("OPENADAPT_TELEMETRY_CONFIG", str(tmp_path / "missing.json"))
    assert telemetry.telemetry_enabled() is False


def test_file_config_can_enable(monkeypatch, tmp_path: Path) -> None:
    cfg = tmp_path / "telemetry.json"
    cfg.write_text(json.dumps({"enabled": True, "posthog_key": "phc_test"}))
    monkeypatch.delenv("OPENADAPT_TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setenv("OPENADAPT_TELEMETRY_CONFIG", str(cfg))
    assert telemetry.telemetry_enabled() is True


def test_sanitize_drops_unknown_and_long_values() -> None:
    cleaned = telemetry._sanitize(
        {
            "outcome": "completed",
            "task_description": "should never ship",
            "error_code": "x" * 100,
            "mode": "replay",
        }
    )
    assert cleaned["outcome"] == "completed"
    assert cleaned["mode"] == "replay"
    assert "task_description" not in cleaned
    assert "error_code" not in cleaned
    assert cleaned["package"] == "openadapt-desktop"


def test_capture_noops_when_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENADAPT_TELEMETRY_ENABLED", raising=False)
    monkeypatch.setenv("OPENADAPT_TELEMETRY_CONFIG", str(tmp_path / "missing.json"))
    # Must not raise even without httpx traffic.
    telemetry.capture("app_opened", {"surface": "test"})
    telemetry.capture("not_an_allowed_event", {"surface": "test"})
