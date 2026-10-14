#!/usr/bin/env python3
"""Build an evidence-bounded OpenAdapt performance report.

The report intentionally keeps two different studies separate:

* current ``openadapt-flow`` compiled execution versus a direct API control;
* historical demo-conditioned (DC) versus zero-shot (ZS) computer use.

The latter is not the current compiler/runtime and must never be relabelled as
such.  Inputs are existing result artifacts; this script performs no GUI work,
network calls, model calls, or cloud mutations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


LEGACY_TASKS = ("04d9aeaf", "0bf05a7d", "0e763496", "70745df8")
LEGACY_TRIALS = (2, 3, 4)
LEGACY_RUNSTAMP = "20260306_124032"
CONDITIONS = ("baseline", "ui_cosmetic_v1")
ARMS = ("compiled", "api")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CURRENT_REQUIRED_FIELDS = (
    "arm",
    "condition",
    "trial",
    "success",
    "silent_incorrect_success",
    "over_halt",
    "wall_s",
    "model_calls",
    "cost_usd",
    "primary_outcome",
    "metadata",
)
LEGACY_REQUIRED_FIELDS = (
    "trial",
    "condition",
    "task",
    "success",
    "reported_success",
    "silent_incorrect_success",
    "over_halt",
    "wall_s",
    "steps",
    "model_calls",
    "cost_usd",
    "primary_outcome",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_fields(row: dict[str, Any], fields: tuple[str, ...], context: str) -> None:
    missing = [field for field in fields if field not in row]
    if missing:
        raise ValueError(f"{context}: missing required fields: {', '.join(missing)}")


def _require_bool(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{context}: expected boolean")
    return value


def _require_nonnegative_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context}: expected number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{context}: expected finite non-negative number")
    return number


def _require_sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{context}: expected lowercase SHA-256")
    return value


def _validate_outcome_flags(row: dict[str, Any], context: str) -> None:
    success = _require_bool(row["success"], f"{context}/success")
    silent = _require_bool(row["silent_incorrect_success"], f"{context}/silent_incorrect_success")
    over_halt = _require_bool(row["over_halt"], f"{context}/over_halt")
    outcome = row["primary_outcome"]
    if not isinstance(outcome, str) or not outcome:
        raise ValueError(f"{context}/primary_outcome: expected non-empty string")
    if success and (silent or outcome != "correct"):
        raise ValueError(f"{context}: successful row has contradictory outcome flags")
    if not success and outcome == "correct":
        raise ValueError(f"{context}: unsuccessful row cannot have primary_outcome=correct")
    if silent and over_halt:
        raise ValueError(f"{context}: row cannot be both silent incorrect and over-halt")


def _nearest_rank(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    walls = [float(row["wall_s"]) for row in rows]
    return {
        "n": len(rows),
        "success_count": sum(bool(row["success"]) for row in rows),
        "success_rate": sum(bool(row["success"]) for row in rows) / len(rows),
        "silent_incorrect_success_count": sum(
            bool(row["silent_incorrect_success"]) for row in rows
        ),
        "over_halt_count": sum(bool(row["over_halt"]) for row in rows),
        "model_calls_total": sum(int(row["model_calls"]) for row in rows),
        "cost_usd_total": round(sum(float(row["cost_usd"]) for row in rows), 6),
        "wall_s_mean": statistics.mean(walls),
        "wall_s_p50": statistics.median(walls),
        "wall_s_p95_nearest_rank": _nearest_rank(walls, 0.95),
        "failure_taxonomy": dict(Counter(row["primary_outcome"] for row in rows)),
    }


def _load_current(app: str, path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("selected_subset_complete") is not True:
        raise ValueError(f"{app}: selected model-free subset is not complete")
    if payload.get("required_per_cell") != 3:
        raise ValueError(f"{app}: expected required_per_cell=3")
    if set(payload.get("arms", [])) != set(ARMS):
        raise ValueError(f"{app}: expected exactly compiled and api arms")
    if tuple(payload.get("conditions", [])) != CONDITIONS:
        raise ValueError(f"{app}: unexpected conditions")

    rows = payload.get("runs", [])
    if not isinstance(rows, list) or len(rows) != len(ARMS) * len(CONDITIONS) * 3:
        raise ValueError(f"{app}: expected exactly 12 selected rows")
    for index, row in enumerate(rows):
        context = f"{app}/row[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{context}: expected object")
        _require_fields(row, CURRENT_REQUIRED_FIELDS, context)
        if row["arm"] not in ARMS or row["condition"] not in CONDITIONS:
            raise ValueError(f"{context}: unexpected arm or condition")
        if row["trial"] not in (1, 2, 3):
            raise ValueError(f"{context}: expected trial 1, 2, or 3")
        _validate_outcome_flags(row, context)
        _require_nonnegative_number(row["wall_s"], f"{context}/wall_s")
        model_calls = _require_nonnegative_number(row["model_calls"], f"{context}/model_calls")
        if not model_calls.is_integer():
            raise ValueError(f"{context}/model_calls: expected integer")
        _require_nonnegative_number(row["cost_usd"], f"{context}/cost_usd")
        metadata = row["metadata"]
        if not isinstance(metadata, dict):
            raise ValueError(f"{context}/metadata: expected object")
        _require_sha256(metadata.get("evidence_sha256"), f"{context}/evidence_sha256")
        evidence_path = metadata.get("evidence_relative_path")
        if (
            not isinstance(evidence_path, str)
            or Path(evidence_path).is_absolute()
            or ".." in Path(evidence_path).parts
        ):
            raise ValueError(f"{context}/evidence_relative_path: expected safe relative path")
        if row["arm"] == "compiled":
            _require_bool(metadata.get("replayer_success"), f"{context}/replayer_success")
    for arm in ARMS:
        for condition in CONDITIONS:
            cell = [
                row for row in rows if row.get("arm") == arm and row.get("condition") == condition
            ]
            if len(cell) != 3 or sorted(row.get("trial") for row in cell) != [1, 2, 3]:
                raise ValueError(f"{app}/{arm}/{condition}: expected trials 1,2,3")

    return {
        "application": app,
        "source_sha256": _sha256(path),
        "generated_at": payload.get("generated_at"),
        "status": payload.get("status"),
        "selected_subset_complete": payload.get("selected_subset_complete"),
        "full_matrix_complete": payload.get("full_matrix_complete"),
        "publication_ready": payload.get("publication_ready"),
        "omitted_arms": payload.get("omitted_arms", []),
        "incomplete_reasons": payload.get("incomplete_reasons", []),
        "environment": payload.get("environment", {}),
        "rows": rows,
    }


def _load_legacy(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("legacy study: expected schema_version=1")
    if payload.get("source_runstamp") != LEGACY_RUNSTAMP:
        raise ValueError("legacy study: unexpected source runstamp")
    if payload.get("trials_per_task_condition") != 3:
        raise ValueError("legacy study: expected three trials per task and condition")

    tasks = payload.get("tasks")
    if (
        not isinstance(tasks, list)
        or not all(isinstance(task, dict) for task in tasks)
        or [task.get("id") for task in tasks] != list(LEGACY_TASKS)
    ):
        raise ValueError("legacy study: unexpected task inventory")
    for task in tasks:
        if not isinstance(task.get("instruction"), str) or not task["instruction"]:
            raise ValueError("legacy study: each task requires its retained instruction")

    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) != 24:
        raise ValueError("legacy study: expected exactly 24 rows")
    for index, row in enumerate(rows):
        context = f"legacy/row[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{context}: expected object")
        _require_fields(row, LEGACY_REQUIRED_FIELDS, context)
        if row["task"] not in LEGACY_TASKS or row["condition"] not in ("zs", "dc"):
            raise ValueError(f"{context}: unexpected task or condition")
        if row["trial"] not in LEGACY_TRIALS:
            raise ValueError(f"{context}: unexpected trial")
        _validate_outcome_flags(row, context)
        _require_bool(row["reported_success"], f"{context}/reported_success")
        expected_silent = row["reported_success"] and not row["success"]
        expected_over_halt = (not row["reported_success"]) and row["success"]
        if row["silent_incorrect_success"] is not expected_silent:
            raise ValueError(f"{context}: inconsistent silent incorrect flag")
        if row["over_halt"] is not expected_over_halt:
            raise ValueError(f"{context}: inconsistent over-halt flag")
        _require_nonnegative_number(row["wall_s"], f"{context}/wall_s")
        steps = _require_nonnegative_number(row["steps"], f"{context}/steps")
        if not steps.is_integer():
            raise ValueError(f"{context}/steps: expected integer")
        if row["model_calls"] is not None or row["cost_usd"] is not None:
            raise ValueError(f"{context}: unavailable model usage and cost must remain null")

    for condition in ("zs", "dc"):
        for task in LEGACY_TASKS:
            cell = [row for row in rows if row["condition"] == condition and row["task"] == task]
            if len(cell) != 3 or sorted(row["trial"] for row in cell) != list(LEGACY_TRIALS):
                raise ValueError(f"legacy/{condition}/{task}: expected retained trials 2,3,4")

    manifest = payload.get("raw_input_manifest")
    if not isinstance(manifest, list) or len(manifest) != 48:
        raise ValueError("legacy study: expected 48 raw input manifest entries")
    for index, item in enumerate(manifest):
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError(f"legacy/input_manifest[{index}]: unexpected entry")
        path_value = item["path"]
        if (
            not isinstance(path_value, str)
            or Path(path_value).is_absolute()
            or ".." in Path(path_value).parts
        ):
            raise ValueError(f"legacy/input_manifest[{index}]: unsafe path")
        _require_sha256(item["sha256"], f"legacy/input_manifest[{index}]/sha256")

    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("legacy study: missing provenance")
    if provenance.get("runner_revision") is not None:
        raise ValueError("legacy study: retained runner revision must remain unavailable")
    if provenance.get("model_id") != "unknown":
        raise ValueError("legacy study: retained model id must remain unknown")
    if provenance.get("raw_inputs_committed") is not False:
        raise ValueError("legacy study: raw inputs must not be represented as committed")

    return {
        "rows": rows,
        "input_manifest": manifest,
        "tasks": tasks,
        "provenance": provenance,
        "source_sha256": _sha256(path),
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    current_apps = [
        _load_current("frappe_lending", args.frappe_results),
        _load_current("openemr_local", args.openemr_results),
    ]
    current_rows = [
        {"application": app["application"], **row} for app in current_apps for row in app["rows"]
    ]
    current_by_cell: dict[str, Any] = {}
    for app in current_apps:
        current_by_cell[app["application"]] = {}
        for arm in ARMS:
            current_by_cell[app["application"]][arm] = {}
            for condition in CONDITIONS:
                current_by_cell[app["application"]][arm][condition] = _aggregate(
                    [
                        row
                        for row in app["rows"]
                        if row["arm"] == arm and row["condition"] == condition
                    ]
                )
    current_overall = {
        arm: _aggregate([row for row in current_rows if row["arm"] == arm]) for arm in ARMS
    }
    current_overall["compiled_vs_api_mean_latency_ratio"] = (
        current_overall["compiled"]["wall_s_mean"] / current_overall["api"]["wall_s_mean"]
    )

    legacy = _load_legacy(args.legacy_input)
    legacy_by_condition: dict[str, Any] = {}
    for condition in ("zs", "dc"):
        rows = [row for row in legacy["rows"] if row["condition"] == condition]
        legacy_by_condition[condition] = _aggregate(
            [
                {
                    **row,
                    "model_calls": 0,
                    "cost_usd": 0.0,
                }
                for row in rows
            ]
        )
        legacy_by_condition[condition]["model_calls_total"] = None
        legacy_by_condition[condition]["cost_usd_total"] = None
        legacy_by_condition[condition]["steps_mean"] = statistics.mean(row["steps"] for row in rows)
    legacy_by_task: dict[str, Any] = {}
    for task in LEGACY_TASKS:
        legacy_by_task[task] = {}
        for condition in ("zs", "dc"):
            rows = [
                row
                for row in legacy["rows"]
                if row["task"] == task and row["condition"] == condition
            ]
            legacy_by_task[task][condition] = {
                "n": len(rows),
                "success_count": sum(row["success"] for row in rows),
                "silent_incorrect_success_count": sum(
                    row["silent_incorrect_success"] for row in rows
                ),
                "wall_s_mean": statistics.mean(row["wall_s"] for row in rows),
                "steps_mean": statistics.mean(row["steps"] for row in rows),
            }

    return {
        "schema_version": 2,
        "evidence_date": args.evidence_date,
        "report_provenance": {
            "openadapt_evals_report_base_revision": args.evals_report_base_commit,
            "report_generator_sha256": args.generator_sha256,
            "committed_input_sha256": {
                "frappe_results": current_apps[0]["source_sha256"],
                "openemr_results": current_apps[1]["source_sha256"],
                "legacy_compact": legacy["source_sha256"],
            },
        },
        "runtime_provenance": {
            "openadapt_flow_candidate_association": {
                "revision": args.flow_candidate_association,
                "binding": "post_hoc_worktree_association",
                "exact_runtime_revision_verified": False,
            },
            "openadapt_flow_current_unmeasured": args.flow_current_commit,
        },
        "retained_model_free_compiler_runtime_evidence": {
            "scope": (
                "model-free compiled execution versus direct API control; pinned synthetic "
                "Frappe Lending and pinned local OpenEMR; baseline and cosmetic drift; "
                "runtime revision is not independently bound by the source summaries"
            ),
            "oracle": "independent read-only REST plus direct SQL delta audit",
            "trials_per_application_arm_condition": 3,
            "applications": [
                {key: value for key, value in app.items() if key != "rows"} for app in current_apps
            ],
            "by_cell": current_by_cell,
            "overall": current_overall,
            "rows": current_rows,
            "verdict": (
                "The complete model-free subset passes 12/12 compiled and 12/12 API runs "
                "with zero silent incorrect success, zero over-halt, zero model calls, and "
                "zero model cost. Direct API execution is materially faster. The zero-shot "
                "agent arm was omitted, so this is not the complete comparative matrix."
            ),
        },
        "historical_legacy_dc_vs_zs": {
            "scope": (
                "legacy computer-use agent with serialized demonstrations versus "
                "the same agent zero-shot; this is not openadapt-flow compiled replay"
            ),
            "environment": "WindowsAgentArena Azure VM, runstamp 20260306_124032",
            "oracle": "WAA native task evaluator",
            "trials_per_task_condition": 3,
            "tasks": legacy["tasks"],
            "provenance": {
                **legacy["provenance"],
                "compact_source_sha256": legacy["source_sha256"],
            },
            "by_condition": legacy_by_condition,
            "by_task": legacy_by_task,
            "success_delta_dc_minus_zs_pp": 100
            * (
                legacy_by_condition["dc"]["success_rate"]
                - legacy_by_condition["zs"]["success_rate"]
            ),
            "rows": legacy["rows"],
            "input_manifest": legacy["input_manifest"],
            "verdict": (
                "Both arms pass 3/12 runs (25%); DC-ZS success delta is 0 pp. DC is faster "
                "on this set but produces no reliability lift and one additional silent "
                "incorrect success. Model usage and cost were not retained in these artifacts."
            ),
        },
        "unmeasured": {
            "exact_current_flow_requalification": (
                f"No valid result on {args.flow_current_commit}. The retained summaries came from "
                f"a worktree later committed as {args.flow_candidate_association}, but the summaries "
                "do not embed a runtime revision. That association is post-hoc rather than an exact "
                "source binding, and current requalification remains pending."
            ),
            "current_flow_vs_zero_shot": (
                "No valid result. The Flow-on-WAA live CLI has no compiled bundles or live "
                "runner available in the current Azure subscription, and its current live "
                "path does not wire task reset plus WAALiveAdapter ground-truth evaluation."
            ),
            "current_flow_agent_arm": (
                "The matched Frappe/OpenEMR artifacts intentionally omit the paid agent arm; "
                "their own publication_ready and full_matrix_complete fields are false."
            ),
        },
        "caveats": [
            "Frappe and OpenEMR tasks use synthetic records on one macOS host.",
            "The current study measures one workflow per application and cosmetic drift only.",
            "Direct API controls are expected to be faster and are the preferred actuation tier when available.",
            "The historical WAA study used a legacy computer-use architecture, not the current compiler/runtime.",
            "The retained model-free summaries came from a worktree later committed as 84c7a94, but do not embed a runtime revision; that revision is a post-hoc association, not an exact binding.",
            "Primary screenshots and oracle-evidence files remain outside Git; the committed summaries retain per-run evidence and artifact hashes, not the raw files.",
            "The committed legacy input is a compact non-sensitive derivative; it retains normalized rows, task instructions, and hashes for 48 raw summary/execution inputs, not the raw inputs themselves.",
            "Legacy model ID, token usage, and dollar cost are unavailable in the retained artifacts.",
            "No new GUI, cloud, or model runs were performed for this report.",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    current = report["retained_model_free_compiler_runtime_evidence"]
    legacy = report["historical_legacy_dc_vs_zs"]
    lines = [
        "# OpenAdapt performance evidence — 2026-07-17",
        "",
        "This report separates retained model-free compiler/runtime evidence from historical demo-conditioned agent evidence.",
        "",
        "## Retained model-free compiler/runtime evidence: compiled versus API",
        "",
        "| Application | Arm | Condition | Runs | Success | Silent incorrect | Over-halt | Mean | p50 | p95 | Model calls | Cost |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for app in ("frappe_lending", "openemr_local"):
        for arm in ARMS:
            for condition in CONDITIONS:
                cell = current["by_cell"][app][arm][condition]
                lines.append(
                    f"| {app} | {arm} | {condition} | {cell['n']} | "
                    f"{cell['success_count']}/{cell['n']} | "
                    f"{cell['silent_incorrect_success_count']} | {cell['over_halt_count']} | "
                    f"{cell['wall_s_mean']:.2f}s | {cell['wall_s_p50']:.2f}s | "
                    f"{cell['wall_s_p95_nearest_rank']:.2f}s | {cell['model_calls_total']} | "
                    f"${cell['cost_usd_total']:.2f} |"
                )
    ratio = current["overall"]["compiled_vs_api_mean_latency_ratio"]
    lines.extend(
        [
            "",
            f"Compiled execution passed 12/12 and averaged {current['overall']['compiled']['wall_s_mean']:.2f}s; "
            f"API control passed 12/12 and averaged {current['overall']['api']['wall_s_mean']:.2f}s. "
            f"The compiled GUI path was {ratio:.1f}× slower. Both had zero measured silent incorrect "
            "success, over-halt, model calls, and model cost.",
            "",
            "This is a complete model-free subset, not a complete comparative matrix: the paid/zero-shot "
            "agent arm was intentionally omitted, and both source artifacts mark `publication_ready=false`.",
            "The summaries came from a worktree later committed as `84c7a94`, but they do not embed "
            "a runtime revision. This is a post-hoc association, not exact source binding; exact "
            "current Flow 1.12.1 requalification remains pending.",
            "",
            "The current benchmark's silent-incorrect and over-halt counters are retained from its "
            "source rows. Historical counters are derived from the agent's `done` claim and the WAA "
            "oracle. p95 uses nearest rank.",
            "",
            "## Historical legacy agent: DC versus zero-shot",
            "",
            "| Task ID | Retained instruction |",
            "|---|---|",
        ]
    )
    for task in legacy["tasks"]:
        instruction = task["instruction"].replace("|", "\\|")
        lines.append(f"| `{task['id']}` | {instruction} |")
    lines.extend(
        [
            "",
            "| Condition | Runs | Success | Silent incorrect | Over-halt | Mean | p50 | p95 | Mean steps |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for condition in ("zs", "dc"):
        cell = legacy["by_condition"][condition]
        lines.append(
            f"| {condition.upper()} | {cell['n']} | {cell['success_count']}/{cell['n']} | "
            f"{cell['silent_incorrect_success_count']} | {cell['over_halt_count']} | "
            f"{cell['wall_s_mean']:.2f}s | {cell['wall_s_p50']:.2f}s | "
            f"{cell['wall_s_p95_nearest_rank']:.2f}s | {cell['steps_mean']:.2f} |"
        )
    lines.extend(
        [
            "",
            "Both historical arms passed 3/12 (25%), so DC − ZS was 0 percentage points. "
            "DC reduced mean wall time from 84.37s to 71.37s, but silent incorrect successes "
            "rose from 5/12 to 6/12. This is legacy computer use with an unknown retained "
            "model ID, not current compiled Flow.",
            "",
            "## What remains unmeasured",
            "",
            "- Current compiled Flow versus zero-shot on WAA: no valid result.",
            "- Exact current Flow 1.12.1 on the matched Frappe/OpenEMR matrix: requalification pending.",
            "- Current paid agent arm on the matched Frappe/OpenEMR matrix: intentionally omitted.",
            "- A real design-partner Windows/Citrix workflow: not represented here.",
            "",
            "## Caveats",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in report["caveats"])
    lines.extend(
        [
            "",
            "The JSON companion contains every normalized row, hashes of the three committed compact "
            "inputs, per-run current evidence hashes retained by those inputs, and hashes for the 48 "
            "uncommitted historical summary/execution inputs.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frappe-results", type=Path, required=True)
    parser.add_argument("--openemr-results", type=Path, required=True)
    parser.add_argument("--legacy-input", type=Path, required=True)
    parser.add_argument("--flow-candidate-association", required=True)
    parser.add_argument("--flow-current-commit", required=True)
    parser.add_argument("--evals-report-base-commit", required=True)
    parser.add_argument("--evidence-date", default="2026-07-17")
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.generator_sha256 = _sha256(Path(__file__))
    report = build_report(args)
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.out_md.write_text(render_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
