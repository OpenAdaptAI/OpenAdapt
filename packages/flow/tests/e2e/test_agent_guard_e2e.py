"""Guard an agent you already run, end to end in a real Chromium.

A scripted Playwright agent (``benchmark/agent_guard/agent.py``) drives its
own browser; ``openadapt-flow guard`` runs in another process, attached over
CDP. The guard blocks the agent's save on the wrong patient and tells it
why, lets the right one through and reads it back, and hands that run to the
teach dir. A person confirms the check and admits after a canary; the program
is then a ``taught_*`` tool that saves with no model calls.

Synthetic MockMed in front of the ``effect_e2e`` record store; localhost only.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

pytest.importorskip("playwright")
pytest.importorskip("mcp")

from benchmark.agent_guard import run as bench  # noqa: E402
from benchmark.agent_guard.app import serve  # noqa: E402

pytestmark = pytest.mark.timeout(900)


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


def test_guard_blocks_wrong_record_then_run_two_is_a_tool(tmp_path: Path) -> None:
    _chromium_or_skip()
    app = serve(tmp_path / "app.sqlite")
    try:
        teach_dir = tmp_path / "teach"
        files = bench.write_setup(
            tmp_path / "setup", app.base_url, teach_dir=teach_dir, db_path=app.db_path
        )
        plain = bench.write_setup(tmp_path / "plain", app.base_url, teach_dir=None)

        wrong = bench.run_one(
            app,
            _scenario("agent_wrong_patient"),
            "guard",
            tmp_path / "run-wrong",
            _port(),
            think_s=1.0,
            files=plain,
            teach=False,
        )
        assert wrong["outcome"] == "blocked", wrong
        assert wrong["blocked"] == ["identity_mismatch"]
        assert wrong["writes"] == 0
        assert wrong["screen_after"] == "blocked"
        assert "patient_id" in (wrong["agent_notice"] or "")

        ok = bench.run_one(
            app,
            _scenario("ok"),
            "guard",
            tmp_path / "run-ok",
            _port(),
            think_s=1.0,
            files=files,
            teach=True,
        )
        assert ok["outcome"] == "clean", ok
        assert ok["verdict"] == "VERIFIED"
        assert ok["guard_exit"] == 0
        handoff = ok["handoff"]
        assert handoff["status"] == "draft", handoff
        assert handoff["inputs"] == ["note"]
        assert handoff["expect"] == {"patient_id": "p1", "type": "Triage"}

        second = bench.run2(
            app,
            Path(handoff["task_dir"]),
            files["verify"],
            teach_dir,
            tmp_path / "taught-runs",
        )
        assert second["admitted"], second
        by_name = {r["scenario"]: r for r in second["runs"]}
        assert by_name["ok"]["outcome"] == "clean"
        assert by_name["ok"]["model_calls"] == 0
        for fault in ("no_persist", "partial", "duplicate", "wrong_record", "stale"):
            assert by_name[fault]["outcome"] == "caught_by_readback", by_name[fault]
    finally:
        app.stop()
