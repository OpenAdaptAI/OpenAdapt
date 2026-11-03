"""Desktop reads Flow's plaintext-at-rest ledger for its notice."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_desktop import at_rest_notice


def test_status_counts_ledger_lines(tmp_path: Path) -> None:
    env = {"OPENADAPT_FLOW_AT_REST_HOME": str(tmp_path)}
    assert at_rest_notice.status(env)["unencrypted_writes"] == 0
    ledger = tmp_path / "plaintext-at-rest.jsonl"
    ledger.write_text(
        json.dumps({"purpose": "a run directory at /r1", "recorded_at": 1})
        + "\nnot json\n"
        + json.dumps({"purpose": "a windows recording at /rec", "recorded_at": 2})
        + "\n"
    )
    got = at_rest_notice.status(env)
    assert got["unencrypted_writes"] == 2
    assert got["latest_purpose"] == "a windows recording at /rec"
    assert got["ledger"] == str(ledger)
    assert got["strict"] is False


def test_flow_env_adds_the_recorded_opt_out_except_in_strict_mode() -> None:
    assert at_rest_notice.flow_env({})[at_rest_notice.OPT_OUT_ENV]
    mine = {at_rest_notice.OPT_OUT_ENV: "my reason"}
    assert at_rest_notice.flow_env(mine)[at_rest_notice.OPT_OUT_ENV] == "my reason"
    strict = at_rest_notice.flow_env({at_rest_notice.MODE_ENV: "strict"})
    assert at_rest_notice.OPT_OUT_ENV not in strict


def test_flow_subprocess_env_carries_the_opt_out(monkeypatch) -> None:
    from openadapt_desktop.flow_bridge import _subprocess_env

    monkeypatch.delenv(at_rest_notice.OPT_OUT_ENV, raising=False)
    monkeypatch.delenv(at_rest_notice.MODE_ENV, raising=False)
    assert _subprocess_env()[at_rest_notice.OPT_OUT_ENV] == (
        at_rest_notice.DESKTOP_OPT_OUT_REASON
    )
