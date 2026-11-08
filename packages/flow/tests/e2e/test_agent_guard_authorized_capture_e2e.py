"""A guarded save is captured and signed inside pre-authorized JIT recording."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright")

from benchmark.agent_guard import run as bench  # noqa: E402
from benchmark.agent_guard.app import serve  # noqa: E402
from openadapt_flow.agent_guard import GuardedCapture, load_config  # noqa: E402
from openadapt_flow.compile_agent.pipeline import FirstRunCompiler  # noqa: E402
from openadapt_flow.guard import load_task  # noqa: E402
from openadapt_flow.jit import lifecycle  # noqa: E402
from tests import _teach_authorization_support as support  # noqa: E402

pytestmark = pytest.mark.timeout(900)
NOTE = bench.NOTE


class _ScriptedDispatcherAgent:
    """A synthetic external agent that receives only the recording dispatcher."""

    dialect = "playwright_mcp"
    requires_model_key = False

    def __init__(self, cfg: Any, task: Any, out_dir: Path) -> None:
        self.cfg = cfg
        self.task = task
        self.out_dir = out_dir
        self.held_save: Any = None

    def drive(self, request: Any, harness: Any) -> None:
        dispatcher = harness.computer.dispatcher
        assert dispatcher is not None
        self.held_save = GuardedCapture(
            dispatcher,
            cfg=self.cfg,
            task=self.task,
            out_dir=self.out_dir,
        )
        assert "triage" in request.instruction.lower()
        for target in (
            "#open-p1",
            "#new-encounter",
            "#type-triage",
            "#note",
        ):
            harness.act({"name": "browser_click", "arguments": {"target": target}})
        harness.act(
            {
                "name": "browser_type",
                "arguments": {"target": "#note", "text": NOTE},
            }
        )
        harness.act(
            {
                "name": "browser_click",
                "arguments": {"target": "#save-encounter"},
            }
        )


def test_slow_held_save_uses_authorized_recording_dispatcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("playwright")
    from openadapt_flow.agent_guard import commit_guard

    hold_s = 2.0
    send = commit_guard.CommitGuard._send

    def slow_send(self: Any, guard: Any, request: Any, held_at: float) -> None:
        import time

        time.sleep(hold_s)
        send(self, guard, request, held_at)

    monkeypatch.setattr(commit_guard.CommitGuard, "_send", slow_send)

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

        # This person-signed statement is issued before the app opens and
        # binds the exact task, origin, record, fields, and typed note.
        authorization_spec = support.spec(
            task.task_id,
            origins=[cfg.app_origin],
            record={"patient_id": "p1"},
            fields={"type": "Triage", "note": NOTE},
            inputs={"note": NOTE},
            record_kind="encounter",
        )
        authorization_spec["writes"]["write_adapter"] = (
            "tests._teach_scope:EncounterWrites"
        )
        authorization_path = support.authorizer(key_dir, monkeypatch).issue(
            tmp_path / "authorization", authorization_spec
        )
        authority = support.authorizer(key_dir, monkeypatch).authority(
            authorization_path
        )
        compiler = FirstRunCompiler(
            "Add a Triage note to patient p1: {note}",
            params=["note"],
            url=cfg.app_url,
            verify=cfg.verify,
            # The benchmark confirms ``note`` as a run-time input during
            # admission. Keep record identity and fixed type in ``expect``;
            # the proposed check binds note to that confirmed input.
            expect={"patient_id": "p1", "type": "Triage"},
            on_screen=["Jane Sample", "p1"],
            task_id=task.task_id,
            cache_dir=teach_dir,
            key_dir=key_dir,
            teach_authorization=authority,
        )
        agent = _ScriptedDispatcherAgent(cfg, task, tmp_path / "guarded-capture")
        result = compiler.record(agent, {"note": NOTE})
        assert result.status == "draft", result
        assert agent.held_save is not None
        assert agent.held_save.summary["verdict"] == "VERIFIED", agent.held_save.summary
        assert agent.held_save.summary["hold_ms"]
        assert agent.held_save.summary["hold_ms"][0] >= hold_s * 1000 * 0.9
        compiled = compiler._jit()
        candidate = compiled.candidate()
        assert candidate is not None
        assert candidate["teach_scope"]["readback"]["status"] == "verified"
        assert candidate["teach_authorization"]["authorizer"] == support.AUTHORIZER
        assert candidate["recorder"]["teach_scope"] == candidate["teach_scope"]
        assert (
            candidate["recorder"]["teach_authorization"]
            == candidate["teach_authorization"]
        )
        status = lifecycle.admission_status(compiled.program_dir, key_dir=key_dir)
        assert status["requirements"]["teach_authorization"]["met"] is True

        # Removing the captured authorization and re-signing the local
        # recorder block still leaves the draft ineligible for admission.
        unsigned_program = tmp_path / "unsigned-program"
        shutil.copytree(compiled.program_dir, unsigned_program)
        unsigned_candidate_path = unsigned_program / lifecycle.CANDIDATE_FILE
        unsigned_candidate = json.loads(unsigned_candidate_path.read_text())
        unsigned_candidate.pop("teach_authorization", None)
        unsigned_candidate.pop("teach_scope", None)
        unsigned_candidate["recorder"] = lifecycle.sign_recorder(
            unsigned_program, unsigned_candidate, key_dir=key_dir
        )["recorder"]
        unsigned_candidate_path.write_text(
            json.dumps(unsigned_candidate, indent=2) + "\n", encoding="utf-8"
        )
        unsigned_status = lifecycle.admission_status(unsigned_program, key_dir=key_dir)
        assert unsigned_status["requirements"]["teach_authorization"]["met"] is False

        # Keep the complete attended run-2 path: a person reviews and admits
        # this exact captured draft, then the taught program verifies its save.
        second = bench.run2(
            app,
            compiler.dir,
            files["verify"],
            teach_dir,
            tmp_path / "taught-runs",
        )
        assert second["admitted"], second
        by_name = {r["scenario"]: r for r in second["runs"]}
        assert by_name["ok"]["outcome"] == "clean"

    finally:
        app.stop()
