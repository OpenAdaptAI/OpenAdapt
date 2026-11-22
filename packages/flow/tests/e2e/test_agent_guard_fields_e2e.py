"""The guard's ledger row and private diff, end to end in a real Chromium.

A scripted Playwright agent (``benchmark/agent_guard/agent.py``) drives its
own headless browser; ``openadapt-flow guard`` runs in another process,
attached over CDP. A clean save is read back VERIFIED with a row that holds
no values; a save the agent makes with another note than the task's shows
that field as differing in the 0600 diff and is caught, not verified.

Synthetic MockMed in front of the ``effect_e2e`` record store; localhost only.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from benchmark.agent_guard import run as bench  # noqa: E402
from benchmark.agent_guard.app import serve  # noqa: E402

pytestmark = pytest.mark.timeout(600)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _scenario(name: str) -> bench.Scenario:
    return next(s for s in bench.SCENARIOS if s.name == name)


def _chromium_or_skip() -> None:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium unavailable: {exc}")


def _no_values(path: Path) -> None:
    data = path.read_bytes()
    for value in (bench.NOTE, bench.OTHER_NOTE, "Jane Sample", "Triage"):
        assert value.encode("utf-8") not in data, value


def test_the_ledger_row_and_diff_from_a_real_browser(tmp_path: Path) -> None:
    _chromium_or_skip()
    app = serve(tmp_path / "app.sqlite")
    try:
        files = bench.write_setup(tmp_path / "setup", app.base_url, teach_dir=None)

        ok = bench.run_one(
            app,
            _scenario("ok"),
            "guard",
            tmp_path / "run-ok",
            _port(),
            think_s=0.5,
            files=files,
            teach=False,
        )
        assert ok["verdict"] == "VERIFIED", ok
        row_path = tmp_path / "run-ok" / "guard_ledger_row.json"
        row = json.loads(row_path.read_text())
        assert row["schema"] == "openadapt.guard-ledger-row/v1"
        assert row["verdict"] == "VERIFIED"
        assert row["fields_checked"] == ["note", "type"]
        assert row["fields_differing"] == []
        assert row["readback_tier"] == "independent_readback"
        _no_values(row_path)
        diff = tmp_path / "run-ok" / "guard_diff.private.json"
        assert diff.stat().st_mode & 0o777 == 0o600

        wrong = bench.run_one(
            app,
            _scenario("agent_wrong_value"),
            "guard",
            tmp_path / "run-wrong",
            _port(),
            think_s=0.5,
            files=files,
            teach=False,
        )
        assert wrong["verdict"] != "VERIFIED", wrong
        assert wrong["verdict"] == "REFUTED"
        assert wrong["guard_exit"] == 4
        row_path = tmp_path / "run-wrong" / "guard_ledger_row.json"
        row = json.loads(row_path.read_text())
        assert row["verdict"] == "CAUGHT_ERROR"
        assert row["fields_differing"] == ["note"]
        _no_values(row_path)
        diff = tmp_path / "run-wrong" / "guard_diff.private.json"
        assert diff.stat().st_mode & 0o777 == 0o600
        by_field = {f["field"]: f for f in json.loads(diff.read_text())["fields"]}
        assert by_field["note"]["intended"] == bench.NOTE
        assert by_field["note"]["saved"] == bench.OTHER_NOTE
        assert by_field["note"]["status"] == "differs"
        assert by_field["type"]["status"] == "same"
    finally:
        app.stop()
