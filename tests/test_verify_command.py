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
