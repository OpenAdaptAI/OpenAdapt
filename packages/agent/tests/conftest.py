"""Shared fixtures: a real (tiny) openadapt-flow bundle + a stubbed flow CLI.

The bundle is written with openadapt-flow's own IR (``Workflow.save``), so
schema drift in flow surfaces here instead of being masked by hand-rolled
JSON. Execution is NEVER real in unit tests: ``subprocess.run`` is
monkeypatched with a stub that emulates the ``openadapt-flow run``
exit-code contract (0 success / 1 halt / 2 governed refusal).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_agent.runner import RunnerConfig


@pytest.fixture()
def bundle_dir(tmp_path: Path) -> Path:
    """A minimal real bundle compiled via flow's IR."""
    from openadapt_flow.ir import ActionKind, Step, Workflow

    bundle = tmp_path / "bundles" / "demo-triage"
    workflow = Workflow(
        name="Demo Triage",
        params={"note": "Follow-up in 2 weeks"},
        steps=[
            Step(id="s1", intent="Open the patient chart", action=ActionKind.CLICK),
            Step(
                id="s2",
                intent="Type the triage note",
                action=ActionKind.TYPE,
                text="Follow-up in 2 weeks",
                param="note",
            ),
        ],
    )
    workflow.save(bundle)
    return bundle


@pytest.fixture()
def bundles_root(bundle_dir: Path) -> Path:
    return bundle_dir.parent


@pytest.fixture()
def runner_config(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig(
        flow_cli=("openadapt-flow-stub",),
        runs_dir=tmp_path / "runs",
        timeout_s=5.0,
    )


class FlowCliStub:
    """Emulates `openadapt-flow run` process behaviour for FlowRunner tests.

    Configure ``exit_code`` and ``report`` (dict written to the run dir's
    report.json, or None to write nothing). Records the exact command it
    was invoked with.
    """

    def __init__(self, exit_code: int = 0, report: dict | None = None):
        self.exit_code = exit_code
        self.report = report
        self.calls: list[list[str]] = []
        self.stdout = "stub stdout"
        self.stderr = ""

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        import subprocess

        self.calls.append(list(cmd))
        if "--run-dir" in cmd:
            run_dir = Path(cmd[cmd.index("--run-dir") + 1])
            run_dir.mkdir(parents=True, exist_ok=True)
            if self.report is not None:
                (run_dir / "report.json").write_text(json.dumps(self.report))
        return subprocess.CompletedProcess(
            cmd, self.exit_code, stdout=self.stdout, stderr=self.stderr
        )


@pytest.fixture()
def success_report() -> dict:
    return {
        "workflow_name": "Demo Triage",
        "success": True,
        "results": [
            {"step_id": "s1", "intent": "Open the patient chart", "ok": True},
            {"step_id": "s2", "intent": "Type the triage note", "ok": True},
        ],
        "heal_count": 0,
        "model_calls": 0,
        "total_ms": 1234.5,
    }


@pytest.fixture()
def halt_report() -> dict:
    return {
        "workflow_name": "Demo Triage",
        "success": False,
        "results": [
            {"step_id": "s1", "intent": "Open the patient chart", "ok": True},
            {
                "step_id": "s2",
                "intent": "Type the triage note",
                "ok": False,
                "error": "postcondition failed",
                "safety_halt": True,
            },
        ],
        "halt": {
            "state_id": "s2",
            "intent": "Type the triage note",
            "reason": "unmet postcondition",
            "outcome": "halt",
            "observed_texts": ["Unexpected dialog: Save changes?"],
            "completed_intents": ["Open the patient chart"],
        },
        "heal_count": 1,
        "model_calls": 0,
        "total_ms": 2222.0,
    }
