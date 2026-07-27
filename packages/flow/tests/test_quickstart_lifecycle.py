"""Fast structural tests for the clean-wheel lifecycle harness."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "quickstart_lifecycle.py"


def _module():
    spec = importlib.util.spec_from_file_location("quickstart_lifecycle", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_tutorial(artifacts: Path, **report_overrides) -> Path:
    """A minimal VERIFIED tutorial run with its receipt, for the inspector."""
    run = artifacts / "tutorial" / "run"
    run.mkdir(parents=True, exist_ok=True)
    report = {
        "execution_outcome": "VERIFIED",
        "execution_profile": "standard",
        "transaction_outcome": "VERIFIED",
        "transaction_billable": True,
        "model_calls": 0,
        "outcome_envelope": {
            "required_contracts": {"effect": 2},
            "passed_contracts": {"effect": 2},
        },
        "results": [
            {
                "effect_evidence": [
                    {"final_verdict": "confirmed", "verification_tier": 1}
                ]
            }
        ],
    }
    report.update(report_overrides)
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (run / "receipt.json").write_text(
        json.dumps(
            {
                "outcome": "VERIFIED",
                "provenance": "synthetic-tutorial",
                "bundle_digest": "a" * 64,
                "receipt_digest": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    (run / "receipt.png").write_bytes(b"png")
    (run / "receipt.md").write_text("# VERIFIED\n", encoding="utf-8")
    return run


def test_resolve_wheel_requires_exactly_one_match(tmp_path):
    lifecycle = _module()
    with pytest.raises(ValueError, match="exactly one"):
        lifecycle._resolve_wheel(str(tmp_path / "*.whl"))
    (tmp_path / "one.whl").write_bytes(b"wheel")
    assert lifecycle._resolve_wheel(str(tmp_path / "*.whl")).name == "one.whl"
    (tmp_path / "two.whl").write_bytes(b"wheel")
    with pytest.raises(ValueError, match="matched 2"):
        lifecycle._resolve_wheel(str(tmp_path / "*.whl"))


def test_run_forces_utf8_for_child_cli_and_log(tmp_path, monkeypatch):
    lifecycle = _module()
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="✓ UTF-8\n")

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)
    log = tmp_path / "child.log"

    lifecycle._run(
        ["openadapt-flow", "--help"],
        cwd=tmp_path,
        env={"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"},
        log=log,
    )

    assert captured["env"]["PYTHONUTF8"] == "1"
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert captured["encoding"] == "utf-8"
    assert log.read_bytes().decode("utf-8").endswith("✓ UTF-8\n")


def test_inspect_artifacts_requires_reports_repairs_and_healed_bundle(tmp_path):
    lifecycle = _module()
    artifacts = tmp_path / "artifacts"
    for name, report in {
        "baseline-run": {"success": True, "model_calls": 0, "heal_count": 0},
        "theme-drift-run": {
            "success": True,
            "model_calls": 0,
            "heal_count": 1,
            "results": [{"heal": {"applied": True}}],
        },
    }.items():
        run = artifacts / name
        run.mkdir(parents=True)
        (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (run / "REPORT.md").write_text("# report\n", encoding="utf-8")
    patch = artifacts / "theme-drift-run" / "heals" / "step_001" / "patch.json"
    patch.parent.mkdir(parents=True)
    patch.write_text("{}", encoding="utf-8")
    healed = artifacts / "healed-bundle"
    healed.mkdir()
    (healed / "workflow.json").write_text("{}", encoding="utf-8")
    (healed / "manifest.json").write_text("{}", encoding="utf-8")
    _write_tutorial(artifacts)

    summary = lifecycle._inspect_artifacts(artifacts)

    assert summary["drift_heals"] == 1
    assert summary["repair_patches"] == 1
    assert summary["tutorial_outcome"] == "VERIFIED"
    assert summary["tutorial_receipt_emitted"] is True


def test_inspect_artifacts_rejects_missing_patch(tmp_path):
    lifecycle = _module()
    artifacts = tmp_path / "artifacts"
    for name, report in {
        "baseline-run": {"success": True, "model_calls": 0, "heal_count": 0},
        "theme-drift-run": {
            "success": True,
            "model_calls": 0,
            "heal_count": 1,
            "results": [{"heal": {"applied": True}}],
        },
    }.items():
        run = artifacts / name
        run.mkdir(parents=True)
        (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (run / "REPORT.md").write_text("# report\n", encoding="utf-8")
    healed = artifacts / "healed-bundle"
    healed.mkdir()
    (healed / "workflow.json").write_text("{}", encoding="utf-8")
    (healed / "manifest.json").write_text("{}", encoding="utf-8")
    _write_tutorial(artifacts)

    with pytest.raises(AssertionError, match="heal evidence is incomplete"):
        lifecycle._inspect_artifacts(artifacts)


def test_inspect_tutorial_refuses_an_unverified_free_path(tmp_path):
    """The regression this whole gate exists for."""
    lifecycle = _module()
    artifacts = tmp_path / "artifacts"
    _write_tutorial(
        artifacts,
        execution_outcome="COMPLETED_UNVERIFIED",
        execution_profile="demo",
        transaction_outcome="COMPLETED_UNVERIFIED",
        transaction_billable=False,
    )
    with pytest.raises(AssertionError, match="did not verify"):
        lifecycle._inspect_tutorial(artifacts / "tutorial")


def test_inspect_tutorial_refuses_a_verified_claim_without_effect_evidence(tmp_path):
    lifecycle = _module()
    artifacts = tmp_path / "artifacts"
    _write_tutorial(artifacts, results=[])
    with pytest.raises(AssertionError, match="no confirmed effect evidence"):
        lifecycle._inspect_tutorial(artifacts / "tutorial")


def test_inspect_tutorial_refuses_a_missing_receipt(tmp_path):
    lifecycle = _module()
    artifacts = tmp_path / "artifacts"
    run = _write_tutorial(artifacts)
    (run / "receipt.json").unlink()
    with pytest.raises(AssertionError, match="no shareable receipt"):
        lifecycle._inspect_tutorial(artifacts / "tutorial")
