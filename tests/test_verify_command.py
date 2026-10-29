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
