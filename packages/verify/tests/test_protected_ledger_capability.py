"""The candidate facade must not fall back with an older Flow installed."""

import json

import pytest
from openadapt_flow import write_ledger

from openadapt_verify.cli import main


@pytest.mark.parametrize(
    "args",
    [
        [
            "run",
            "--contract",
            "unused",
            "--params",
            "unused",
            "--state",
            "unused",
            "--",
            "unused",
        ],
        [
            "recheck",
            "--contract",
            "unused",
            "--params",
            "unused",
            "--recheck-state",
            "unused",
        ],
        ["serve", "--contracts", "unused", "--state", "unused"],
    ],
    ids=["run", "recheck", "serve"],
)
def test_configured_protection_refuses_flow_without_support(args, monkeypatch, capsys):
    monkeypatch.setenv("OPENADAPT_LEDGER_AUTHORITY_CONFIG", "/unused-config.json")
    monkeypatch.delattr(write_ledger, "PROTECTED_LEDGER_CONFIG_ENV", raising=False)

    assert main(args) == 3
    captured = capsys.readouterr()
    if args[0] == "serve":
        assert captured.out == ""
        assert captured.err == "openadapt-verify: ledger_authority_unsupported\n"
    else:
        expected = {
            "verified": False,
            "outcome": "UNCERTAIN",
            "hold_reason": "ledger_authority_unsupported",
        }
        if args[0] == "run":
            expected["written"] = False
        assert json.loads(captured.out) == expected
        assert captured.err == ""
