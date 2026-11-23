"""Tests for ``openadapt quickstart --task night-shift`` and ``--task calendar-block``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from openadapt_flow.runtime.effects.local_state import LocalStateVerifier
from openadapt_flow.verify_run import VerifyConfig

from openadapt.cli import main
from openadapt.quickstart.everyday import (
    EverydayTaskError,
    build_calendar_block_contracts,
    build_night_shift_contracts,
    run_everyday_task,
)


def test_build_night_shift_contracts_macos_and_windows_with_revert() -> None:
    fwd_mac, rev_mac, params_mac = build_night_shift_contracts(
        platform="darwin", baseline_enabled=False, revert=True
    )
    assert fwd_mac["verifier"]["local_state"][0]["type"] == "macos_night_shift"
    assert fwd_mac["effects"][0]["value"] == "true"
    assert rev_mac is not None
    assert rev_mac["effects"][0]["value"] == "false"
    assert params_mac["target_enabled"] == "true"
    assert params_mac["restore_enabled"] == "false"
    VerifyConfig.model_validate(fwd_mac)
    VerifyConfig.model_validate(rev_mac)

    fwd_win, rev_win, params_win = build_night_shift_contracts(
        platform="win32", baseline_enabled=True, revert=False
    )
    assert fwd_win["verifier"]["local_state"][0]["type"] == "windows_night_light"
    assert fwd_win["effects"][0]["value"] == "false"
    assert rev_win is None
    assert params_win["revert"] is False
    VerifyConfig.model_validate(fwd_win)

    with pytest.raises(EverydayTaskError, match="unsupported platform"):
        build_night_shift_contracts(platform="linux")


def test_build_calendar_block_contracts_parameterized_and_revert() -> None:
    fwd, rev, params = build_calendar_block_contracts(
        title="Weekly Architecture Review",
        calendar_name="Engineering",
        start_iso="2026-11-24T14:00:00",
        duration_minutes=45,
        recurrence="weekly",
        platform="darwin",
        revert=True,
    )
    src = fwd["verifier"]["local_state"][0]
    assert src["type"] == "macos_calendar_event"
    assert src["calendar_name"] == "Engineering"
    assert src["title"] == "Weekly Architecture Review"
    assert src["start_iso"] == "2026-11-24T14:00:00"
    assert fwd["effects"][0]["value"] == "true"
    assert rev is not None
    assert rev["effects"][0]["value"] == "false"
    assert params["end_iso"] == "2026-11-24T14:45:00"
    assert params["duration_minutes"] == 45
    assert params["recurrence"] == "weekly"
    VerifyConfig.model_validate(fwd)
    VerifyConfig.model_validate(rev)

    with pytest.raises(EverydayTaskError, match="duration"):
        build_calendar_block_contracts(duration_minutes=0, platform="darwin")
    with pytest.raises(EverydayTaskError, match="recurrence"):
        build_calendar_block_contracts(recurrence="yearly", platform="darwin")
    with pytest.raises(EverydayTaskError, match="unsupported platform"):
        build_calendar_block_contracts(platform="linux")


def test_run_everyday_task_night_shift_writes_contracts_and_probes_baseline(
    tmp_path: Path,
) -> None:
    lines: list[str] = []
    bundle = run_everyday_task(
        "night-shift",
        tmp_path / "ns",
        platform="darwin",
        revert=True,
        say=lines.append,
        verifier_factory=lambda sources, platform: LocalStateVerifier(
            sources, platform=platform, night_shift_reader=lambda: False
        ),
    )
    assert bundle.baseline_state is not None
    assert bundle.baseline_state["enabled"] is False
    assert bundle.verify_path.is_file()
    assert bundle.revert_verify_path is not None and bundle.revert_verify_path.is_file()
    assert bundle.params_path.is_file()

    fwd = yaml.safe_load(bundle.verify_path.read_text(encoding="utf-8"))
    rev = yaml.safe_load(bundle.revert_verify_path.read_text(encoding="utf-8"))
    assert fwd["effects"][0]["value"] == "true"
    assert rev["effects"][0]["value"] == "false"
    assert any("Baseline probe (read-only)" in line for line in lines)


def test_run_everyday_task_calendar_block_writes_parameterized_bundle(
    tmp_path: Path,
) -> None:
    bundle = run_everyday_task(
        "calendar-block",
        tmp_path / "cal",
        title="Focus Sprint",
        calendar_name="Work",
        start_iso="2026-11-24T11:00:00",
        duration_minutes=60,
        recurrence="weekdays",
        revert=True,
        platform="darwin",
        say=lambda _msg: None,
        verifier_factory=lambda sources, platform: LocalStateVerifier(
            sources,
            platform=platform,
            runner=lambda argv: subprocess.CompletedProcess(
                argv, 0, b"NOT_FOUND\n", b""
            ),
        ),
    )
    assert bundle.baseline_state is not None
    assert bundle.baseline_state["exists"] is False
    params = json.loads(bundle.params_path.read_text(encoding="utf-8"))
    assert params["title"] == "Focus Sprint"
    assert params["calendar_name"] == "Work"
    assert params["end_iso"] == "2026-11-24T12:00:00"
    assert params["recurrence"] == "weekdays"
    assert bundle.revert_verify_path is not None and bundle.revert_verify_path.is_file()


def test_cli_quickstart_task_validation_and_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()

    # Calendar flags without --task calendar-block are rejected
    bad = runner.invoke(main, ["quickstart", "--title", "Standup"])
    assert bad.exit_code != 0
    assert "--task calendar-block" in bad.output

    # --task combined with --own is rejected
    bad_own = runner.invoke(main, ["quickstart", "--own", "--task", "night-shift"])
    assert bad_own.exit_code != 0
    assert "cannot be combined" in bad_own.output

    # Stub baseline probe so CLI invocation is deterministic on any OS
    monkeypatch.setattr(
        "openadapt.quickstart.everyday._probe_baseline",
        lambda doc, platform, verifier_factory=None: (
            {"type": "macos_calendar_event", "value": "false", "exists": False},
            None,
        ),
    )
    monkeypatch.setattr("openadapt.quickstart.everyday.sys.platform", "darwin")

    out_dir = tmp_path / "cli-cal"
    ok = runner.invoke(
        main,
        [
            "quickstart",
            "--task",
            "calendar-block",
            "--title",
            "Deep Work",
            "--duration",
            "45",
            "--recurrence",
            "daily",
            "--start",
            "2026-11-24T15:00:00",
            "--out",
            str(out_dir),
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert (out_dir / "verify.yaml").is_file()
    assert (out_dir / "revert_verify.yaml").is_file()
    params = json.loads((out_dir / "task.json").read_text(encoding="utf-8"))
    assert params["title"] == "Deep Work"
    assert params["end_iso"] == "2026-11-24T15:45:00"
    assert params["recurrence"] == "daily"
