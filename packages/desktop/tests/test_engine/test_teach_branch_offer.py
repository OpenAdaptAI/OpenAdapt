"""Teach offers a branch once two halts on one step were resolved."""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_desktop.flow_bridge import FlowBridge, FlowResult

from .test_dispatch import deps  # noqa: F401  (pytest fixture)


def _halted_run(db, tmp_path: Path, run_id: str, intent: str) -> None:
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps({"halt": {"step_intent": intent, "reason": "dialog"}})
    )
    db.insert_run(run_id, str(run_dir), bundle_id="bnd1", status="HALTED")


class _Bridge:
    def __init__(self) -> None:
        self.branch_calls: list[list[tuple[Path, Path]]] = []

    def teach(self, _run, _bundle, out_dir, *, fix):
        return FlowResult(ok=True, returncode=0, out_dir=out_dir)

    def teach_branch(self, examples, _bundle, out_dir):
        self.branch_calls.append(list(examples))
        return FlowResult(ok=True, returncode=0, out_dir=out_dir)


def _teach(disp, run_id: str) -> dict:
    return disp.dispatch(
        "teach_fix",
        {"workflow_id": "bnd1", "run_id": run_id, "mode": "describe", "note": "dismiss it"},
    )


def test_second_resolution_on_the_same_step_offers_a_branch(deps, tmp_path):  # noqa: F811
    disp, db, _events = deps
    (tmp_path / "bundle").mkdir()
    db.insert_bundle("bnd1", str(tmp_path / "bundle"), capture_id="cap1")
    bridge = _Bridge()
    disp.services._flow_bridge = bridge

    _halted_run(db, tmp_path, "run-a", "Click Save")
    first = _teach(disp, "run-a")
    assert "branch_offer" not in first

    _halted_run(db, tmp_path, "run-b", "Click Save")
    second = _teach(disp, "run-b")
    offer = second["branch_offer"]
    assert offer == {"examples": 2, "step_key": "step_intent:Click Save"}

    result = disp.dispatch("teach_branch", {"workflow_id": "bnd1", **offer})
    assert result["ok"] is True
    assert "approve" in result["message"]
    assert [run.name for run, _fix in bridge.branch_calls[0]] == ["run-a", "run-b"]


def test_resolutions_on_different_steps_do_not_offer_a_branch(deps, tmp_path):  # noqa: F811
    disp, db, _events = deps
    (tmp_path / "bundle").mkdir()
    db.insert_bundle("bnd1", str(tmp_path / "bundle"), capture_id="cap1")
    bridge = _Bridge()
    disp.services._flow_bridge = bridge

    _halted_run(db, tmp_path, "run-a", "Click Save")
    _teach(disp, "run-a")
    _halted_run(db, tmp_path, "run-b", "Open chart")
    assert "branch_offer" not in _teach(disp, "run-b")

    refused = disp.dispatch(
        "teach_branch", {"workflow_id": "bnd1", "step_key": "step_intent:Open chart"}
    )
    assert refused["ok"] is False
    assert bridge.branch_calls == []


def test_bridge_builds_the_teach_branch_command(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(command)
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(
        "openadapt_desktop.flow_bridge._pinned_flow_command",
        lambda: ["/usr/bin/openadapt-flow"],
    )
    bridge = FlowBridge(runner=runner)
    bridge.teach_branch(
        [(tmp_path / "r1", tmp_path / "f1.json"), (tmp_path / "r2", tmp_path / "f2.json")],
        tmp_path / "bundle",
        tmp_path / "out",
    )
    command = calls[0]
    assert command[1] == "teach-branch"
    assert command.count("--example") == 2
    assert command[command.index("--bundle") + 1] == str(tmp_path / "bundle")
