from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.report_openadapt_performance import (
    CONDITIONS,
    LEGACY_RUNSTAMP,
    LEGACY_TASKS,
    LEGACY_TRIALS,
    _load_current,
    _sha256,
    build_report,
    render_markdown,
)

ROOT = Path(__file__).resolve().parents[1]
INPUT_ROOT = ROOT / "docs/eval_results/inputs/openadapt_performance_20260717"
REPORT_JSON = ROOT / "docs/eval_results/openadapt_performance_20260717.json"
REPORT_MD = ROOT / "docs/eval_results/openadapt_performance_20260717.md"
SCRIPT = ROOT / "scripts/report_openadapt_performance.py"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _current_payload() -> dict:
    runs = []
    for arm in ("compiled", "api"):
        for condition in CONDITIONS:
            for trial in (1, 2, 3):
                metadata = {
                    "evidence_relative_path": f"evidence/{arm}-{condition}-{trial}.json",
                    "evidence_sha256": "a" * 64,
                }
                if arm == "compiled":
                    metadata["replayer_success"] = True
                runs.append(
                    {
                        "arm": arm,
                        "condition": condition,
                        "trial": trial,
                        "success": True,
                        "silent_incorrect_success": False,
                        "over_halt": False,
                        "wall_s": float(trial),
                        "model_calls": 0,
                        "cost_usd": 0.0,
                        "primary_outcome": "correct",
                        "metadata": metadata,
                    }
                )
    return {
        "selected_subset_complete": True,
        "required_per_cell": 3,
        "arms": ["compiled", "api"],
        "conditions": list(CONDITIONS),
        "runs": runs,
        "full_matrix_complete": False,
        "publication_ready": False,
        "omitted_arms": ["agent"],
        "incomplete_reasons": ["agent omitted"],
        "status": "model_free_subset_complete",
        "environment": {},
    }


def _legacy_payload() -> dict:
    rows = []
    for trial in LEGACY_TRIALS:
        for condition in ("zs", "dc"):
            for task in LEGACY_TASKS:
                success = task == "0e763496"
                rows.append(
                    {
                        "trial": trial,
                        "condition": condition,
                        "task": task,
                        "success": success,
                        "reported_success": True,
                        "silent_incorrect_success": not success,
                        "over_halt": False,
                        "wall_s": 2.0,
                        "steps": 3,
                        "model_calls": None,
                        "cost_usd": None,
                        "primary_outcome": ("correct" if success else "silent_incorrect_success"),
                    }
                )
    return {
        "schema_version": 1,
        "source_runstamp": LEGACY_RUNSTAMP,
        "provenance": {
            "source_environment": "fixture",
            "runner_revision": None,
            "runner_revision_status": "unavailable in retained artifacts",
            "model_id": "unknown",
            "raw_inputs_committed": False,
            "raw_input_files": 48,
            "sanitization": "fixture",
        },
        "oracle": "fixture oracle",
        "trials_per_task_condition": 3,
        "tasks": [
            {"id": task, "instruction": f"Retained instruction for {task}"} for task in LEGACY_TASKS
        ],
        "rows": rows,
        "raw_input_manifest": [
            {"path": f"fixture/input-{index}.json", "sha256": f"{index:064x}"}
            for index in range(48)
        ],
    }


def _report_args(frappe: Path, openemr: Path, legacy: Path) -> SimpleNamespace:
    return SimpleNamespace(
        frappe_results=frappe,
        openemr_results=openemr,
        legacy_input=legacy,
        flow_candidate_association="flow-associated-sha",
        flow_current_commit="flow-current-sha",
        evals_report_base_commit="evals-base-sha",
        generator_sha256="b" * 64,
        evidence_date="2026-07-17",
    )


def test_build_report_keeps_current_and_legacy_evidence_separate(tmp_path: Path) -> None:
    frappe = tmp_path / "frappe.json"
    openemr = tmp_path / "openemr.json"
    legacy = tmp_path / "legacy.json"
    _write_json(frappe, _current_payload())
    _write_json(openemr, _current_payload())
    _write_json(legacy, _legacy_payload())

    report = build_report(_report_args(frappe, openemr, legacy))

    current = report["retained_model_free_compiler_runtime_evidence"]
    historical = report["historical_legacy_dc_vs_zs"]
    assert current["overall"]["compiled"]["n"] == 12
    assert current["overall"]["compiled"]["success_count"] == 12
    assert historical["by_condition"]["zs"]["n"] == 12
    assert historical["by_condition"]["dc"]["success_count"] == 3
    assert "not openadapt-flow compiled replay" in historical["scope"]
    assert report["unmeasured"]["current_flow_vs_zero_shot"].startswith("No valid result")
    assert report["runtime_provenance"]["openadapt_flow_candidate_association"] == {
        "revision": "flow-associated-sha",
        "binding": "post_hoc_worktree_association",
        "exact_runtime_revision_verified": False,
    }


def test_current_loader_rejects_incomplete_three_trial_cell(tmp_path: Path) -> None:
    payload = _current_payload()
    payload["runs"].pop()
    path = tmp_path / "incomplete.json"
    _write_json(path, payload)

    with pytest.raises(ValueError, match="expected exactly 12 selected rows"):
        _load_current("fixture", path)


def test_current_loader_requires_explicit_usage_and_cost(tmp_path: Path) -> None:
    payload = _current_payload()
    del payload["runs"][0]["model_calls"]
    path = tmp_path / "missing-usage.json"
    _write_json(path, payload)

    with pytest.raises(ValueError, match="missing required fields: model_calls"):
        _load_current("fixture", path)


def test_current_loader_rejects_contradictory_outcome_flags(tmp_path: Path) -> None:
    payload = _current_payload()
    payload["runs"][0]["silent_incorrect_success"] = True
    path = tmp_path / "contradictory.json"
    _write_json(path, payload)

    with pytest.raises(ValueError, match="contradictory outcome flags"):
        _load_current("fixture", path)


def test_committed_inputs_regenerate_published_report_byte_for_byte() -> None:
    report = build_report(
        SimpleNamespace(
            frappe_results=INPUT_ROOT / "frappe_results.json",
            openemr_results=INPUT_ROOT / "openemr_results.json",
            legacy_input=INPUT_ROOT / "legacy_compact.json",
            flow_candidate_association=("84c7a94f2d2ca9e183799394d1952ae32fa6bf92"),
            flow_current_commit="db87e3ffe802a94046f0f131da6094dac9a0fbd7",
            evals_report_base_commit="24a3108dc4a2c301895881d06172a2d280518dfc",
            generator_sha256=_sha256(SCRIPT),
            evidence_date="2026-07-17",
        )
    )
    generated_json = json.dumps(report, indent=2, sort_keys=True) + "\n"
    generated_markdown = render_markdown(report)

    assert generated_json.encode() == REPORT_JSON.read_bytes()
    assert generated_markdown.encode() == REPORT_MD.read_bytes()
