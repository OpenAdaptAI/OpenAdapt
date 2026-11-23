"""``openadapt handoff list`` reaches Flow's ``handoff list`` unchanged."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

pytest.importorskip("openadapt_flow.handoffs")

from openadapt import cli  # noqa: E402


def test_root_handoff_list_runs_flow_handoff_list(tmp_path):
    from openadapt_flow.handoffs.ledger import HandoffLedger

    ledger = HandoffLedger(tmp_path / "handoffs")
    ledger.send(
        "REF-1",
        {"birth_date": ("2001-02-03", "date")},
        counterparty="receiving clinic",
        follow_up_hours=1,
    )
    result = CliRunner().invoke(
        cli.main, ["handoff", "list", "--ledger", str(ledger.directory)]
    )
    assert result.exit_code == 0, result.output
    assert "Waiting (1)" in result.output
    assert "receiving clinic: sent, no answer yet" in result.output
