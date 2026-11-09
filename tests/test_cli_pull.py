"""`openadapt pull` forwards to `openadapt-flow pull`."""

from __future__ import annotations

from click.testing import CliRunner

from openadapt import cli


def test_pull_forwards_arguments(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(cli, "_flow_verbs", lambda: frozenset({"pull"}))
    monkeypatch.setattr(cli, "_require_supported_python", lambda: None)
    monkeypatch.setattr(cli, "_invoke_flow", lambda argv: seen.append(argv) or 0)
    result = CliRunner().invoke(cli.main, ["pull", "openemr/new-referral", "--out", "x"])
    assert result.exit_code == 0
    assert seen == [["pull", "openemr/new-referral", "--out", "x"]]


def test_pull_explains_old_engine(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_flow_verbs", lambda: frozenset({"run"}))
    monkeypatch.setattr(cli, "_require_supported_python", lambda: None)
    result = CliRunner().invoke(cli.main, ["pull", "openemr/new-referral"])
    assert result.exit_code == 1
    assert "upgrade" in result.output.lower()
