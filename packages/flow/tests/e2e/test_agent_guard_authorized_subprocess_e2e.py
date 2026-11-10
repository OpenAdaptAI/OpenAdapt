"""A pre-authorized child capture keeps the held save through admission.

This exercises the recording-owned dispatcher, not the legacy passive CDP
handoff. The guard and capture run in a real child with the original slow-hold
injection; the independent store judge and attended run 2 stay in the parent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
pytest.importorskip("mcp")

from benchmark.agent_guard import run as bench  # noqa: E402
from benchmark.agent_guard.app import serve  # noqa: E402
from benchmark.effect_e2e import ground_truth  # noqa: E402
from openadapt_flow.agent_guard import load_config  # noqa: E402
from openadapt_flow.compile_agent.pipeline import FirstRunCompiler  # noqa: E402
from openadapt_flow.guard import load_task  # noqa: E402
from openadapt_flow.jit import lifecycle  # noqa: E402
from openadapt_flow.teach_authorization import TeachAuthority  # noqa: E402
from tests import _teach_authorization_support as support  # noqa: E402

from .test_agent_guard_authorized_capture_e2e import (  # noqa: E402
    _ScriptedDispatcherAgent,
)
from .test_agent_guard_slow_hold_e2e import _SLOW_HOLD, HOLD_S  # noqa: E402

pytestmark = pytest.mark.timeout(900)


def _capture_in_child(config: Path, result_path: Path) -> None:
    """Test-only process entry point; receives no authorizer signing key."""
    inputs = json.loads(config.read_text(encoding="utf-8"))
    cfg = load_config(inputs["guard"])
    task = load_task(inputs["task"])
    assert cfg.teach is not None and cfg.teach.key_dir is not None
    authority = TeachAuthority(
        Path(inputs["statement"]),
        key_dir=cfg.teach.key_dir,
        approver_root=inputs["approver_root"],
    )
    # The real verifier must accept the parent's signed statement before
    # FirstRunCompiler can start capture or open its browser.
    authority.verify(purpose="teach", task_id=task.task_id)
    compiler = FirstRunCompiler(
        "Add a Triage note to patient p1: {note}",
        params=["note"],
        url=cfg.app_url,
        verify=cfg.verify,
        expect={"patient_id": "p1", "type": "Triage"},
        on_screen=["Jane Sample", "p1"],
        task_id=task.task_id,
        cache_dir=cfg.teach.dir,
        key_dir=cfg.teach.key_dir,
        teach_authorization=authority,
    )
    agent = _ScriptedDispatcherAgent(cfg, task, Path(inputs["capture_out"]))
    result = compiler.record(
        agent, {"note": bench.NOTE}, principal_id="subprocess-agent"
    )
    assert result.status == "draft", result
    assert agent.held_save is not None
    result_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "status": result.status,
                "summary": agent.held_save.summary,
                "handoff": {"task_dir": str(compiler.dir)},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_slow_held_save_in_authorized_subprocess_keeps_its_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = serve(tmp_path / "app.sqlite")
    try:
        teach_dir = tmp_path / "teach"
        files = bench.write_setup(
            tmp_path / "setup", app.base_url, teach_dir=teach_dir, db_path=app.db_path
        )
        cfg = load_config(files["guard"])
        task = load_task(files["task"])
        assert cfg.teach is not None and cfg.teach.key_dir is not None
        key_dir = cfg.teach.key_dir
        person = support.authorizer(key_dir, monkeypatch)
        spec = support.spec(
            task.task_id,
            origins=[cfg.app_origin],
            record={"patient_id": "p1"},
            fields={"type": "Triage", "note": bench.NOTE},
            inputs={"note": bench.NOTE},
            record_kind="encounter",
        )
        spec["writes"]["write_adapter"] = "tests._teach_scope:EncounterWrites"
        statement = person.issue(tmp_path / "authorization", spec)
        assert statement.is_file()
        assert statement.with_name(statement.name + ".sig").is_file()
        # The approver's private key stays in this process. The child gets
        # only the statement, signed public roster and pinned public root.
        child_config = tmp_path / "child-config.json"
        child_config.write_text(
            json.dumps(
                {
                    "guard": str(files["guard"]),
                    "task": str(files["task"]),
                    "statement": str(statement),
                    "approver_root": person.root,
                    "capture_out": str(tmp_path / "guarded-capture"),
                }
            ),
            encoding="utf-8",
        )
        patch = tmp_path / "slow_hold"
        patch.mkdir()
        (patch / "sitecustomize.py").write_text(_SLOW_HOLD, encoding="utf-8")
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(patch), child_env.get("PYTHONPATH")])
        )
        result_path = tmp_path / "child-result.json"
        stdout_path = tmp_path / "child-stdout.log"
        stderr_path = tmp_path / "child-stderr.log"
        before = ground_truth.capture(app.db_path)
        with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests.e2e.test_agent_guard_authorized_subprocess_e2e",
                    str(child_config),
                    str(result_path),
                ],
                env=child_env,
                cwd=Path(__file__).resolve().parents[2],
                stdout=stdout,
                stderr=stderr,
                timeout=300,
                check=False,
            )
        assert process.returncode == 0, (
            stdout_path.read_text()[-6000:],
            stderr_path.read_text()[-6000:],
        )
        captured = json.loads(result_path.read_text())
        assert captured["pid"] != os.getpid()
        summary = captured["summary"]
        truth = ground_truth.judge(
            before, ground_truth.capture(app.db_path), intended_note=bench.NOTE
        )
        ok = {
            "arm": "guard",
            "truth": {"correct": truth.correct, "fault_class": truth.fault_class},
            "blocked": summary.get("blocked") or [],
            "writes": app.writes,
            "verdict": summary.get("verdict"),
            "hold_ms": summary.get("hold_ms"),
            "handoff": captured["handoff"],
        }
        ok["outcome"] = bench.outcome(ok)
        assert ok["outcome"] == "clean", ok
        assert ok["hold_ms"] and ok["hold_ms"][0] >= HOLD_S * 1000 * 0.9, ok
        handoff = ok["handoff"]
        assert handoff and handoff.get("task_dir"), ok
        task_dir = Path(handoff["task_dir"])
        events_path = task_dir / "program" / "recording" / "events.jsonl"
        assert events_path.is_file(), ok
        events = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records = events[-1].get("sor_after")
        assert records and records[0]["patient_id"] == "p1", records
        program = task_dir / "program"
        candidate = json.loads((program / lifecycle.CANDIDATE_FILE).read_text())
        assert candidate["recorder"]["teach_authorization"]["authorizer"] == (
            support.AUTHORIZER
        )
        assert candidate["recorder"]["agent_principal_id"] == "subprocess-agent"
        assert candidate["recorder"]["teach_scope"]["readback"]["status"] == "verified"
        status = lifecycle.admission_status(program, key_dir=key_dir)
        assert status["requirements"]["teach_authorization"]["met"] is True
        # PYTHONPATH was changed only for the child. The canary has no slow
        # hold, while the parent still pins the real fixture approver root.
        second = bench.run2(
            app, task_dir, files["verify"], teach_dir, tmp_path / "taught-runs"
        )
        assert second["admitted"], second
        by_name = {r["scenario"]: r for r in second["runs"]}
        assert by_name["ok"]["outcome"] == "clean"
    finally:
        app.stop()


if __name__ == "__main__":
    _capture_in_child(Path(sys.argv[1]), Path(sys.argv[2]))
