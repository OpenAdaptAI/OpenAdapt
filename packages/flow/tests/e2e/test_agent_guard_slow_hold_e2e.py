"""A save the guard holds for seconds still teaches a program a person can admit.

On a loaded CI runner the guard's checks held the save longer than the
recorder took to call the screen settled. The recording then showed no new
record for the save step, compile emitted a placeholder effect, and admission
refused the program: "no canary ran: a write step lacks an independent
read-back: step_005: record_written is placeholder". This test holds every
guarded save for two seconds, in the guard's own process, and requires the
recording, the admission and the canary to come out as they do with a fast
hold. tests/test_recorder_waits_for_writes.py checks the recorder's wait alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("playwright")
pytest.importorskip("mcp")

from benchmark.agent_guard import run as bench  # noqa: E402
from benchmark.agent_guard.app import serve  # noqa: E402

from .test_agent_guard_e2e import _chromium_or_skip, _port, _scenario  # noqa: E402

pytestmark = pytest.mark.timeout(900)

HOLD_S = 2.0
_SLOW_HOLD = f"""
import time
from openadapt_flow.agent_guard import commit_guard as _cg
_send = _cg.CommitGuard._send
def _held(self, guard, request, held_at):
    time.sleep({HOLD_S})
    return _send(self, guard, request, held_at)
_cg.CommitGuard._send = _held
"""


def test_a_slowly_held_save_is_recorded_with_its_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _chromium_or_skip()
    # The guard runs as a subprocess with this environment, so only its
    # process loads the slow hold; the canary below runs in this one.
    patch = tmp_path / "slow_hold"
    patch.mkdir()
    (patch / "sitecustomize.py").write_text(_SLOW_HOLD, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(patch))
    app = serve(tmp_path / "app.sqlite")
    try:
        teach_dir = tmp_path / "teach"
        files = bench.write_setup(
            tmp_path / "setup", app.base_url, teach_dir=teach_dir, db_path=app.db_path
        )
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
        assert ok["hold_ms"] and ok["hold_ms"][0] >= HOLD_S * 1000 * 0.9, ok["hold_ms"]
        events = [
            json.loads(line)
            for line in (tmp_path / "run-ok" / "recording" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        save = events[-1]
        # The record read after the save click holds the new record: the
        # recorder waited for the held save to go through.
        records = save.get("sor_after")
        assert records and records[0]["patient_id"] == "p1", records
        handoff = ok["handoff"]
        monkeypatch.delenv("PYTHONPATH")
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
    finally:
        app.stop()
