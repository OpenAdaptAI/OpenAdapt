"""Launcher ledger tests that use the in-tree Flow authority."""

from __future__ import annotations

import json

from click.testing import CliRunner

from openadapt.cli import main


def test_verify_ledger_explicit_root_must_match_protected_configuration(
    tmp_path, monkeypatch
) -> None:
    from openadapt_flow.write_ledger import FileLedger, Ledger, SQLiteJournalAuthority

    journal = tmp_path / "journal"
    authority = SQLiteJournalAuthority(
        tmp_path / "authority.sqlite", root=journal, ledger_id="test", generation="1"
    )
    FileLedger(journal).enroll(authority, authorized=True)
    ledger = Ledger(FileLedger(journal, authority=authority))
    key = "a" * 64
    ledger.begun(key, "write-1", contract="c" * 64, actor_kind="agent", actor_id="bot")
    ledger.resolved(key, "write-1", outcome="REFUTED", retry_safe=False)
    descriptor = tmp_path / "protected.json"
    descriptor.write_text(
        json.dumps(
            {
                "journal_root": str(journal),
                "authority_path": str(authority.path),
                "ledger_id": "test",
                "generation": "1",
            }
        )
    )
    monkeypatch.delenv("OPENADAPT_LEDGER_DIR", raising=False)
    monkeypatch.setenv("OPENADAPT_LEDGER_AUTHORITY_CONFIG", str(descriptor))
    runner = CliRunner()
    command = ["verify", "ledger", "list", "--ledger"]

    listed = runner.invoke(main, [*command, str(journal)])
    assert listed.exit_code == 0, listed.output
    assert key in listed.output and "held" in listed.output

    other = tmp_path / "other"
    refused = runner.invoke(main, [*command, str(other)])
    assert refused.exit_code != 0
    assert "ledger_authority_wrong_scope" in refused.output
    assert not other.exists()
    assert authority.snapshot().revision == 2

    monkeypatch.delenv("OPENADAPT_LEDGER_AUTHORITY_CONFIG")
    unconfigured = runner.invoke(main, [*command, str(journal)])
    assert unconfigured.exit_code == 1
    assert unconfigured.output == "Error: ledger_protected_requires_authority\n"
    assert authority.snapshot().revision == 2


def test_verify_ledger_list_reports_named_read_hold(tmp_path, monkeypatch) -> None:
    from openadapt_flow.write_ledger import Ledger, LedgerIntegrityHeld

    def unavailable(_ledger):
        raise LedgerIntegrityHeld("ledger_authority_unavailable")

    monkeypatch.setattr(Ledger, "unresolved", unavailable)
    command = ["verify", "ledger", "list", "--ledger", str(tmp_path / "journal")]
    result = CliRunner().invoke(main, command)
    assert result.exit_code == 1
    assert result.output == "Error: ledger_authority_unavailable\n"
