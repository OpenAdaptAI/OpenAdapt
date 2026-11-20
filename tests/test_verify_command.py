"""`openadapt verify serve` starts the verify MCP server of the installed engine."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from openadapt.cli import main


def test_verify_serve_passes_its_options_through(monkeypatch, tmp_path) -> None:
    agent_cli = pytest.importorskip("openadapt_flow.agent.cli")
    seen: list[list[str]] = []
    monkeypatch.setattr(agent_cli, "main", lambda argv: seen.append(argv) or 0)
    result = CliRunner().invoke(
        main,
        [
            "verify",
            "serve",
            "--contracts",
            str(tmp_path),
            "--state",
            str(tmp_path / "s"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == [
        ["serve", "--verify", str(tmp_path), "--verify-state", str(tmp_path / "s")]
    ]


def test_verify_serve_needs_contracts() -> None:
    result = CliRunner().invoke(main, ["verify", "serve"])
    assert result.exit_code == 2
    assert "--contracts" in result.output


def test_verify_ledger_lists_and_releases_a_held_write(tmp_path) -> None:
    ledger_mod = pytest.importorskip("openadapt_flow.write_ledger")
    folder = tmp_path / "ledger"
    ledger = ledger_mod.Ledger(ledger_mod.FileLedger(folder))
    key = "b" * 64
    ledger.begun(key, "write-1", contract="c" * 64, actor_kind="agent", actor_id="bot")
    ledger.resolved(key, "write-1", outcome="REFUTED", retry_safe=False)
    runner = CliRunner()

    listed = runner.invoke(main, ["verify", "ledger", "list", "--ledger", str(folder)])
    assert listed.exit_code == 0, listed.output
    assert key in listed.output and "held" in listed.output

    # Its contract isn't in the folder: reconcile says so and changes nothing.
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    rec = runner.invoke(
        main,
        [
            "verify",
            "ledger",
            "reconcile",
            "--contracts",
            str(contracts),
            "--ledger",
            str(folder),
        ],
    )
    assert rec.exit_code == 0 and "skipped" in rec.output

    refused = runner.invoke(
        main,
        [
            "verify",
            "ledger",
            "release",
            key,
            "--by",
            " ",
            "--reason",
            "x",
            "--found",
            "absent",
            "--ledger",
            str(folder),
        ],
    )
    assert refused.exit_code == 2
    released = runner.invoke(
        main,
        [
            "verify",
            "ledger",
            "release",
            key,
            "--by",
            "nurse-lead",
            "--reason",
            "checked",
            "--found",
            "absent",
            "--ledger",
            str(folder),
        ],
    )
    assert released.exit_code == 0, released.output
    listed = runner.invoke(main, ["verify", "ledger", "list", "--ledger", str(folder)])
    assert "No held or open writes." in listed.output


def test_verify_ledger_lists_a_recheck_owed_until_it_runs(tmp_path) -> None:
    ledger_mod = pytest.importorskip("openadapt_flow.write_ledger")
    folder = tmp_path / "ledger"
    ledger = ledger_mod.Ledger(ledger_mod.FileLedger(folder))
    key = "d" * 64
    due = "2026-01-01T00:02:00Z"
    ledger.begun(key, "write-2", contract="c" * 64, actor_kind="agent", actor_id="bot")
    ledger.resolved(
        key,
        "write-2",
        outcome="CONFIRMED",
        retry_safe=False,
        delivered=True,
        result={"outcome": "CONFIRMED", "provisional": True, "recheck_due_at": due},
    )
    runner = CliRunner()
    listed = runner.invoke(main, ["verify", "ledger", "list", "--ledger", str(folder)])
    assert listed.exit_code == 0, listed.output
    assert key in listed.output and "recheck-owed" in listed.output
    assert f"due {due}" in listed.output

    ledger.rechecked(key, "write-2", outcome="CONFIRMED")
    listed = runner.invoke(main, ["verify", "ledger", "list", "--ledger", str(folder)])
    assert "recheck-owed" not in listed.output
    assert "No rechecks owed." in listed.output


def test_verify_serve_refuses_configured_protection_without_flow_support(
    tmp_path, monkeypatch
) -> None:
    from openadapt_flow import write_ledger
    from openadapt_flow.agent import cli as agent_cli

    monkeypatch.setenv("OPENADAPT_LEDGER_AUTHORITY_CONFIG", "/unused-config.json")
    monkeypatch.delattr(write_ledger, "PROTECTED_LEDGER_CONFIG_ENV", raising=False)
    seen = []
    monkeypatch.setattr(agent_cli, "main", lambda argv: seen.append(argv) or 0)

    result = CliRunner().invoke(main, ["verify", "serve", "--contracts", str(tmp_path)])

    assert result.exit_code != 0
    assert "ledger_authority_unsupported" in result.output
    assert seen == []
