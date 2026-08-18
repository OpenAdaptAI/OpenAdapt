from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "quickstart_lifecycle.py"
WORKFLOW = ROOT / ".github" / "workflows" / "quickstart-lifecycle.yml"


def _module():
    spec = importlib.util.spec_from_file_location("launcher_lifecycle", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_verified_quickstart(root: Path) -> None:
    run = root / "run"
    run.mkdir(parents=True)
    (run / "report.json").write_text(
        json.dumps(
            {
                "execution_outcome": "VERIFIED",
                "execution_profile": "standard",
                "transaction_outcome": "VERIFIED",
                "model_calls": 0,
                "outcome_envelope": {
                    "required_contracts": {"effect": 2},
                    "passed_contracts": {"effect": 2},
                },
            }
        ),
        encoding="utf-8",
    )
    (run / "receipt.json").write_text(
        json.dumps({"outcome": "VERIFIED", "provenance": "synthetic-tutorial"}),
        encoding="utf-8",
    )
    (run / "REPORT.md").write_text("# VERIFIED\n", encoding="utf-8")
    (run / "receipt.md").write_text("# VERIFIED\n", encoding="utf-8")
    (run / "receipt.png").write_bytes(b"png")


def test_inspector_requires_a_verified_standard_effect(tmp_path):
    lifecycle = _module()
    _write_verified_quickstart(tmp_path)

    summary = lifecycle._inspect_quickstart(tmp_path)

    assert summary["outcome"] == "VERIFIED"
    assert summary["effect_contracts"] == 2

    report = tmp_path / "run" / "report.json"
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["transaction_outcome"] = "COMPLETED_UNVERIFIED"
    report.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AssertionError, match="transaction outcome"):
        lifecycle._inspect_quickstart(tmp_path)


def test_zero_exit_with_an_unhandled_async_error_fails(tmp_path, monkeypatch):
    lifecycle = _module()
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="VERIFIED\nTask was destroyed but it is pending!\n"
        ),
    )

    with pytest.raises(RuntimeError, match="unhandled runtime error"):
        lifecycle._run(
            ["openadapt", "quickstart"],
            cwd=tmp_path,
            env={},
            log=tmp_path / "quickstart.log",
        )


def test_workflow_runs_the_public_command_in_one_bounded_weekly_job():
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = document[True]
    jobs = document["jobs"]

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert list(jobs) == ["quickstart"]
    steps = jobs["quickstart"]["steps"]
    run = next(step["run"] for step in steps if step.get("name", "").startswith("Run"))
    assert "scripts/quickstart_lifecycle.py" in run
    assert "--browser-with-deps" in run
