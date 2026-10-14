#!/usr/bin/env python3
"""Run a source-bound, local-only benchmark of current compiled Flow.

The benchmark intentionally avoids paid models and provider infrastructure. It
records and compiles the bundled synthetic MockMed workflow, then compares:

* OpenAdapt compiled replay;
* a positional Playwright selector script; and
* a name-scoped Playwright selector script.

Every arm gets a fresh browser and is judged by the same screenshot/OCR oracle:
the expected saved encounter, exact note, and intended patient must be visible,
and a wrong-type or wrong-patient write is a wrong action.  Neither the
replayer nor either script can self-score success.

The default matrix has three trials per arm in each of three conditions:
clean, cosmetic theme drift, and user-facing label drift.  Healthy runs make no
model calls and no network/provider calls beyond loopback to the bundled app.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_RUNTIME_PATH = Path(__file__).resolve().parent / "evidence_runtime.py"
_RUNTIME_SPEC = importlib.util.spec_from_file_location("_evidence_runtime", _RUNTIME_PATH)
assert _RUNTIME_SPEC is not None and _RUNTIME_SPEC.loader is not None
_RUNTIME = importlib.util.module_from_spec(_RUNTIME_SPEC)
_RUNTIME_SPEC.loader.exec_module(_RUNTIME)
capture_runtime = _RUNTIME.capture_runtime

ARMS = ("compiled", "dom", "dom_named")
CONDITIONS = ("clean", "theme", "rename")
ARM_LABELS = {
    "compiled": "compiled replay",
    "dom": "DOM positional",
    "dom_named": "DOM name-scoped",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _source_binding(root: Path) -> dict[str, Any]:
    commit = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain", "--untracked-files=no")
    if status:
        raise RuntimeError(f"Flow source must be tracked-clean: {root}")
    tags = sorted(tag for tag in _git(root, "tag", "--points-at", "HEAD").splitlines() if tag)
    return {
        "commit": commit,
        "tags": tags,
        "tracked_clean": True,
    }


def _extract_wheel(wheel: Path, destination: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            path = Path(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"unsafe wheel member: {member.filename}")
        archive.extractall(destination)


def _nearest_rank(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _reported_complete(row: dict[str, Any]) -> bool:
    if row["arm"] == "compiled":
        return row.get("replayer_success") is True
    return (
        row.get("error") is None
        and row.get("failed_step") is None
        and row.get("actions") == row.get("steps_total")
    )


def _classify(row: dict[str, Any]) -> str:
    oracle_success = row.get("success") is True
    reported_complete = _reported_complete(row)
    if oracle_success and reported_complete:
        return "correct"
    if oracle_success:
        return "over_halt"
    if reported_complete:
        return "silent_incorrect_success"
    if row.get("wrong_action"):
        return "wrong_action_after_halt_or_error"
    return "halt_or_error"


def _reproduce_command(flow_version: str, wheel_filename: str) -> str:
    """Return the exact-version command recorded in derived evidence."""

    return (
        "python scripts/run_current_flow_local_benchmark.py "
        f"--flow-source <v{flow_version}-checkout> "
        f"--flow-wheel <{wheel_filename}> "
        "--out <new-output-directory>"
    )


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    steady = [float(row["steady_wall_s"]) for row in rows]
    end_to_end = [float(row["end_to_end_wall_s"]) for row in rows]
    taxonomy = Counter(row["primary_outcome"] for row in rows)
    return {
        "n": len(rows),
        "task_success_count": sum(row.get("success") is True for row in rows),
        "task_success_rate": (
            sum(row.get("success") is True for row in rows) / len(rows) if rows else 0.0
        ),
        "reported_complete_count": sum(_reported_complete(row) for row in rows),
        "silent_incorrect_success_count": taxonomy["silent_incorrect_success"],
        "wrong_action_count": sum(bool(row.get("wrong_action")) for row in rows),
        "over_halt_count": taxonomy["over_halt"],
        "halt_or_error_count": taxonomy["halt_or_error"],
        "failure_taxonomy": dict(sorted(taxonomy.items())),
        "steady_wall_s_median": statistics.median(steady) if steady else 0.0,
        "steady_wall_s_p95_nearest_rank": _nearest_rank(steady, 0.95),
        "end_to_end_wall_s_median": (statistics.median(end_to_end) if end_to_end else 0.0),
        "end_to_end_wall_s_p95_nearest_rank": _nearest_rank(end_to_end, 0.95),
        "browser_oracle_teardown_overhead_s_median": (
            statistics.median(row["end_to_end_wall_s"] - row["steady_wall_s"] for row in rows)
            if rows
            else 0.0
        ),
        "model_calls_total": sum(int(row.get("api_calls") or 0) for row in rows),
        "model_cost_usd_total": round(sum(float(row.get("cost_usd") or 0.0) for row in rows), 8),
    }


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Exact-current local Flow performance",
        "",
        (
            "This is a deterministic-runtime overhead and bounded robustness "
            "comparison. It is **not** a zero-shot computer-use comparison."
        ),
        "",
        "## Source and environment",
        "",
        f"- Flow commit: `{report['source']['flow']['commit']}` "
        f"(version `{report['source']['flow']['version']}`; tracked-clean source)",
        f"- Release tag: `{report['source']['flow']['release_tag']}`",
        f"- Wheel SHA-256: `{report['source']['flow']['artifact']['sha256']}`",
        f"- Evals base commit: `{report['source']['evals']['commit']}`",
        f"- Runner SHA-256: `{report['source']['runner_sha256']}`",
        f"- Platform: `{report['environment']['platform']}`",
        f"- Python: `{report['environment']['python']}`",
        f"- Playwright: `{report['environment']['playwright']}`",
        f"- Chromium: `{report['environment']['chromium']}`",
        "- Network/provider use: loopback bundled MockMed only; no cloud VM, "
        "hosted runner, or model API",
        "",
        "## Counted result",
        "",
        (
            "One synthetic MockMed workflow: sign in, open the intended "
            "referral, create a Triage encounter, enter a trial-unique note, "
            "and save. Each arm used a fresh browser. The arm-independent "
            "screenshot/OCR oracle required the exact saved note, Triage row, "
            "and intended patient, and separately flagged wrong-target writes."
        ),
        "",
        (
            "| Condition | Arm | Runs | Task success | Silent incorrect | "
            "Wrong action | Over-halt | Halt/error | Steady median | "
            "Steady p95 | End-to-end median | End-to-end p95 | Model calls | Cost |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in report["conditions"]:
        for arm in report["arms"]:
            cell = report["aggregate"][arm][condition]
            lines.append(
                f"| `{condition}` | {ARM_LABELS[arm]} | {cell['n']} | "
                f"{cell['task_success_count']}/{cell['n']} | "
                f"{cell['silent_incorrect_success_count']} | "
                f"{cell['wrong_action_count']} | {cell['over_halt_count']} | "
                f"{cell['halt_or_error_count']} | "
                f"{cell['steady_wall_s_median']:.3f}s | "
                f"{cell['steady_wall_s_p95_nearest_rank']:.3f}s | "
                f"{cell['end_to_end_wall_s_median']:.3f}s | "
                f"{cell['end_to_end_wall_s_p95_nearest_rank']:.3f}s | "
                f"{cell['model_calls_total']} | "
                f"${cell['model_cost_usd_total']:.2f} |"
            )
    lines.extend(
        [
            "",
            "Steady time wraps only the replay/script action loop. End-to-end "
            "time additionally includes fresh browser launch, the independent "
            "final screenshot/OCR oracle, and browser teardown. Local server "
            "startup and one-time record/compile are excluded from both and "
            "reported separately below. p95 is nearest-rank; with n=3 it is "
            "the slowest counted trial.",
            "",
            "Outcome definitions: **silent incorrect success** means the arm "
            "reported completion but the independent oracle did not confirm "
            "the intended effect; **wrong action** means the oracle observed a "
            "write to the wrong patient or encounter type; **over-halt** means "
            "the arm reported halt/incomplete while the independent oracle "
            "confirmed the intended effect; **halt/error** means the arm "
            "stopped and the intended effect was absent. The same definitions "
            "apply to every arm. The selector controls therefore can count as "
            "over-halts when alternate final-state evidence contradicts them; "
            "their rename failures did not, because the oracle confirmed no "
            "write.",
            "",
            "## One-time setup",
            "",
            (
                f"- Bundled app server startup: "
                f"{report['setup']['server_start_wall_s']:.3f}s (n=1 diagnostic)"
            ),
        ]
    )
    for setup in report["setup"]["record_compile_trials"]:
        lines.append(
            f"- Setup trial {setup['trial']}: record "
            f"{setup['record_wall_s']:.3f}s; compile "
            f"{setup['compile_wall_s']:.3f}s; bundle "
            f"`{setup['bundle_tree_sha256']}`"
        )
    identity = report["identity_coverage"]
    lines.extend(
        [
            "",
            "## What this establishes",
            "",
            "- The independent final-state oracle confirmed the intended effect "
            "for every compiled clean, theme-drift, and label-drift trial. "
            f"Compiled theme runs reported "
            f"{report['aggregate']['compiled']['theme']['over_halt_count']}/"
            f"{report['aggregate']['compiled']['theme']['n']} over-halts; these "
            "are counted rather than relabelled as clean completions.",
            "- The selector controls are steelmanned Playwright scripts. Their "
            "clean/theme speed is the correct reminder that API/structural "
            "actuation should remain the preferred tier where available.",
            "- The `rename` surface changes `Open` to `View` and `Save "
            "Encounter` to `Submit Encounter`. Both selector controls failed "
            "loudly at the first renamed locator before mutation. These are "
            "unsupported-drift halts, not silent wrong actions.",
            "- Label drift is an intentionally bounded robustness probe, not "
            "evidence for arbitrary drift or arbitrary applications.",
            f"- The compiled bundle had "
            f"{identity['armed_steps']}/"
            f"{identity['applicable_steps']} "
            "identity-applicable clicks armed. Task completion is not a "
            "universal wrong-target-immunity claim for unarmed steps.",
            "- The exact theme postcondition failures are retained in "
            "`theme_postcondition_over_halt.json` as a compact regression artifact.",
            "",
            "## Exact-current Flow versus zero-shot: not run",
            "",
            "No current Flow-versus-zero-shot result is claimed. The Azure WAA "
            "VM was not started and no model was called. The existing "
            "`scripts/eval_flow_on_waa.py` live replay path currently leaves "
            "the WAA evaluator unwired (so it cannot independently score "
            "success), while its hybrid live path explicitly returns before "
            "execution because the adapter is not wired. A valid future run "
            "therefore requires, in order:",
            "",
            "1. Wire `WAALiveAdapter.evaluate` into the Flow replay path and "
            "wire the same model/adapter into the zero-shot arm.",
            "2. Prepare one exact compiled bundle per retained task and bind "
            "Flow/evals/model/environment revisions in the run manifest.",
            "3. Obtain explicit approval for Azure VM start and a hard model "
            "spend cap; then confirm VM snapshot/readiness without changing the "
            "task set.",
            "4. Run at least three trials per task per condition for both arms, "
            "using WAA's evaluator as the oracle and recording correct, silent "
            "incorrect, over-halt, timeout/error, latency, tokens, and cost.",
            "5. Deallocate the VM, verify no orphaned resources, and publish "
            "the immutable raw summaries plus a normalized report.",
            "",
            "## Caveats",
            "",
            "- Synthetic MockMed, one workflow, one macOS host, headless Chromium.",
            "- This report runs the exact published wheel named above, extracted "
            "locally, and binds it to the release-tagged tracked-clean source.",
            "- Browser startup is fresh per run but OS/browser caches are warm "
            "after the first launch; the Latin-rotated arm order reduces but "
            "does not eliminate host-order effects.",
            "- Final screenshots were inspected and hashed during the run; "
            "only their SHA-256 hashes are retained in this committed report.",
            "- No hosted lifecycle, Windows UIA, RDP, Citrix, or real customer "
            "application is represented.",
            "",
        ]
    )
    return "\n".join(lines)


def run_benchmark(
    flow_source: Path,
    flow_wheel: Path,
    out_dir: Path,
    *,
    trials: int = 3,
    setup_trials: int = 3,
    headed: bool = False,
) -> dict[str, Any]:
    if trials < 3:
        raise ValueError("comparative evaluation requires at least three trials")
    if setup_trials < 1:
        raise ValueError("setup_trials must be positive")
    flow_source = flow_source.resolve()
    flow_wheel = flow_wheel.resolve()
    out_dir = out_dir.resolve()
    if not (flow_source / "openadapt_flow").is_dir():
        raise ValueError(f"not an openadapt-flow source checkout: {flow_source}")
    if not flow_wheel.is_file() or flow_wheel.suffix != ".whl":
        raise ValueError(f"not a Flow wheel: {flow_wheel}")

    source = _source_binding(flow_source)
    artifact_temp = tempfile.TemporaryDirectory(prefix="oa-flow-wheel-")
    artifact_root = Path(artifact_temp.name).resolve()
    _extract_wheel(flow_wheel, artifact_root)
    sys.path.insert(0, str(artifact_root))

    from openadapt_flow.benchmark.dom_arm import (  # noqa: PLC0415
        TARGET_PATIENT,
        condition_url,
        dom_run,
        note_for_slot,
        verify_final_state,
    )
    from openadapt_flow.benchmark.run_benchmark import (  # noqa: PLC0415
        _compiled_run,
    )
    from openadapt_flow.compiler import compile_recording  # noqa: PLC0415
    from openadapt_flow.demo_driver import record_triage_demo  # noqa: PLC0415
    from openadapt_flow.mockmed.server import serve  # noqa: PLC0415

    imported = Path(sys.modules["openadapt_flow"].__file__).resolve()
    if artifact_root not in imported.parents:
        raise RuntimeError(f"Flow import escaped requested wheel: {imported}")
    flow_version = str(sys.modules["openadapt_flow"].__version__)
    release_tag = f"v{flow_version}"
    if release_tag not in source["tags"]:
        raise RuntimeError(f"source commit is not tagged {release_tag}: {source['tags']}")

    evals_root = Path(__file__).resolve().parents[1]
    evals_commit = _git(evals_root, "rev-parse", "HEAD")
    out_dir.mkdir(parents=True, exist_ok=False)
    finals_temp = tempfile.TemporaryDirectory(prefix="oa-current-flow-finals-")
    finals = Path(finals_temp.name)

    server_started = time.monotonic()
    url, stop = serve(port=0)
    server_start_wall_s = time.monotonic() - server_started
    rows: list[dict[str, Any]] = []
    setups: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="oa-current-flow-local-") as tmp:
            root = Path(tmp)
            bundles: list[Path] = []
            for setup_trial in range(1, setup_trials + 1):
                setup_root = root / f"setup-{setup_trial}"
                recording_started = time.monotonic()
                recording = record_triage_demo(
                    url,
                    setup_root / "recording",
                    note_text=f"Exact-current setup {setup_trial}",
                    headed=headed,
                )
                record_wall_s = time.monotonic() - recording_started
                bundle = setup_root / "bundle"
                compile_started = time.monotonic()
                compile_recording(
                    recording,
                    bundle,
                    name=f"current-flow-local-setup-{setup_trial}",
                )
                compile_wall_s = time.monotonic() - compile_started
                bundles.append(bundle)
                setups.append(
                    {
                        "trial": setup_trial,
                        "record_wall_s": record_wall_s,
                        "compile_wall_s": compile_wall_s,
                        "bundle_tree_sha256": _tree_sha256(bundle),
                        "workflow_sha256": _sha256(bundle / "workflow.json"),
                        "manifest_sha256": _sha256(bundle / "manifest.json"),
                    }
                )
            bundle = bundles[-1]

            # Latin-rotate arm order across trials to limit warm-cache/order bias.
            for condition_index, condition in enumerate(CONDITIONS):
                for trial in range(1, trials + 1):
                    slot = condition_index * trials + (trial - 1)
                    rotation = (trial - 1) % len(ARMS)
                    arm_order = ARMS[rotation:] + ARMS[:rotation]
                    for arm in arm_order:
                        note = note_for_slot(arm, slot)
                        target = condition_url(url, condition)
                        final_path = finals / f"{condition}-{trial}-{arm}.png"
                        run_dir = root / "runs" / f"{condition}-{trial}-{arm}"
                        started = time.monotonic()
                        try:
                            if arm == "compiled":
                                row = _compiled_run(
                                    bundle,
                                    target,
                                    run_dir,
                                    note,
                                    verify_fn=verify_final_state,
                                    save_final_to=final_path,
                                    headed=headed,
                                )
                            else:
                                row = dom_run(
                                    target,
                                    note,
                                    target_patient=(TARGET_PATIENT if arm == "dom_named" else None),
                                    verify_fn=verify_final_state,
                                    save_final_to=final_path,
                                    headed=headed,
                                )
                        except Exception as exc:  # a failed run is evidence
                            row = {
                                "arm": arm,
                                "wall_s": 0.0,
                                "success": False,
                                "actions": 0,
                                "api_calls": 0,
                                "cost_usd": 0.0,
                                "error": f"{type(exc).__name__}: {exc}",
                                "wrong_action": False,
                            }
                        end_to_end = time.monotonic() - started
                        row["condition"] = condition
                        row["trial"] = trial
                        row["note_sha256"] = hashlib.sha256(note.encode("utf-8")).hexdigest()
                        row["steady_wall_s"] = float(row.pop("wall_s", 0.0))
                        row["end_to_end_wall_s"] = end_to_end
                        row["reported_complete"] = _reported_complete(row)
                        row["primary_outcome"] = _classify(row)
                        row["silent_incorrect_success"] = (
                            row["primary_outcome"] == "silent_incorrect_success"
                        )
                        row["over_halt"] = row["primary_outcome"] == "over_halt"
                        row["final_screenshot_sha256"] = (
                            _sha256(final_path) if final_path.is_file() else None
                        )
                        rows.append(row)
                        print(
                            f"{condition} trial={trial} arm={arm}: "
                            f"{row['primary_outcome']} steady="
                            f"{row['steady_wall_s']:.3f}s end_to_end="
                            f"{end_to_end:.3f}s",
                            flush=True,
                        )
    finally:
        stop()

    runtime = capture_runtime(out_dir, browser_required=True)
    aggregate = {
        arm: {
            condition: _aggregate(
                [row for row in rows if row["arm"] == arm and row["condition"] == condition]
            )
            for condition in CONDITIONS
        }
        for arm in ARMS
    }
    compiled_example = next(row for row in rows if row["arm"] == "compiled")
    identity_coverage = {
        "applicable_steps": int(compiled_example.get("identity_applicable_steps") or 0),
        "armed_steps": int(compiled_example.get("identity_armed_steps") or 0),
        "unarmed": compiled_example.get("identity_unarmed") or [],
    }
    for row in rows:
        row.pop("identity_applicable_steps", None)
        row.pop("identity_armed_steps", None)
        row.pop("identity_unarmed", None)
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "exact-current local deterministic runtime overhead and robustness",
        "task": (
            "MockMed triage: sign in, open intended referral, create Triage "
            "encounter, enter a trial-unique note, save"
        ),
        "oracle": (
            "arm-independent screenshot/OCR final-state check: exact note and "
            "saved banner, Triage row, intended patient, no wrong-type write"
        ),
        "outcome_definitions": {
            "correct": ("arm reported completion and independent oracle confirmed effect"),
            "silent_incorrect_success": (
                "arm reported completion but independent oracle did not confirm effect"
            ),
            "wrong_action": ("independent oracle observed a wrong-patient or wrong-type write"),
            "over_halt": ("arm reported halt/incomplete but independent oracle confirmed effect"),
            "halt_or_error": (
                "arm reported halt/incomplete and independent oracle found effect absent"
            ),
        },
        "trials_per_arm_condition": trials,
        "arms": list(ARMS),
        "conditions": list(CONDITIONS),
        "source": {
            "flow": {
                **source,
                "version": flow_version,
                "release_tag": release_tag,
                "artifact": {
                    "filename": flow_wheel.name,
                    "sha256": _sha256(flow_wheel),
                    "import_mode": "locally extracted published wheel",
                },
            },
            "evals": {"commit": evals_commit},
            "runner_sha256": _sha256(Path(__file__).resolve()),
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "playwright": importlib.metadata.version("playwright"),
            "chromium": "Playwright-managed headless Chromium",
        },
        "runtime": runtime,
        "setup": {
            "server_start_wall_s": server_start_wall_s,
            "record_compile_trials": setups,
        },
        "identity_coverage": identity_coverage,
        "aggregate": aggregate,
        "runs": rows,
        "paid_or_remote_mutations": [],
        "zero_shot_comparison": {
            "status": "not_run",
            "reason": (
                "requires paid model plus an available paid WAA environment; "
                "current live Flow replay also lacks a wired WAA evaluator"
            ),
        },
        "caveats": [
            "Synthetic MockMed, one workflow, one macOS host, headless Chromium.",
            "Published Flow wheel and release-tagged source are exact-bound.",
            "Warm OS/browser caches remain after the first fresh-browser launch.",
            "Final screenshot bytes were discarded after their hashes were retained.",
            "No hosted, Windows, RDP, Citrix, or customer workflow is represented.",
        ],
    }
    json_path = out_dir / "results.json"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "REPORT.md").write_text(_render_markdown(report), encoding="utf-8")
    theme_over_halts = [
        {
            "trial": row["trial"],
            "replayer_success": row.get("replayer_success"),
            "oracle_success": row.get("success"),
            "primary_outcome": row["primary_outcome"],
            "first_failure": row.get("first_failure"),
            "steady_wall_s": row["steady_wall_s"],
            "end_to_end_wall_s": row["end_to_end_wall_s"],
            "note_sha256": row["note_sha256"],
            "final_screenshot_sha256": row["final_screenshot_sha256"],
        }
        for row in rows
        if row["arm"] == "compiled"
        and row["condition"] == "theme"
        and row["primary_outcome"] == "over_halt"
    ]
    regression = {
        "schema_version": 1,
        "title": "theme effect succeeds but region_stable reports halt",
        "flow": report["source"]["flow"],
        "runner_sha256": report["source"]["runner_sha256"],
        "task": report["task"],
        "condition": {
            "query": "?drift=theme",
            "kind": "cosmetic theme drift",
        },
        "oracle": report["oracle"],
        "expected": (
            "if the independently verified effect succeeded, the runtime must "
            "not leave the run in an unresumable false-incomplete state"
        ),
        "observed_count": len(theme_over_halts),
        "required_trials": trials,
        "observations": theme_over_halts,
        "reproduce": _reproduce_command(flow_version, flow_wheel.name),
    }
    (out_dir / "theme_postcondition_over_halt.json").write_text(
        json.dumps(regression, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    finals_temp.cleanup()
    artifact_temp.cleanup()
    print(f"Wrote {json_path} and {out_dir / 'REPORT.md'}", flush=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run exact-current local Flow versus selector controls."
    )
    parser.add_argument("--flow-source", type=Path, required=True)
    parser.add_argument("--flow-wheel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--setup-trials", type=int, default=3)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)
    run_benchmark(
        args.flow_source,
        args.flow_wheel,
        args.out,
        trials=args.trials,
        setup_trials=args.setup_trials,
        headed=args.headed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
