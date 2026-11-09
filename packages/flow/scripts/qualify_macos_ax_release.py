#!/usr/bin/env python3
"""Release-lane macOS AX record/compile/replay qualification.

The fixed matrix uses one synthetic TextEdit workflow and the unmodified
Recorder -> compiler -> Replayer stack:

* 3 healthy trials must resolve the recorded AX target structurally, verify its
  structured identity, make zero model calls, and produce exact target-file
  bytes.
* 3 one-glyph wrong-identity trials must halt before input and preserve the
  exact original file bytes.
* 3 ambiguous-window trials must refuse before input and preserve both exact
  original files.

There are no automatic retries.  Every TextEdit process is launched under a
temporary qualification root and terminated by exact PID; unrelated TextEdit
processes are audited and preserved.  The run refuses before opening TextEdit
unless the candidate is a clean, exact Git commit and both macOS permissions
are available.

Usage:
    python scripts/qualify_macos_ax_release.py \
      --output benchmark/macos_native/ax_release_<candidate>_<date>.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from openadapt_flow import __version__  # noqa: E402
from openadapt_flow.backends.macos_backend import (  # noqa: E402
    MacOSBackend,
    MacOSBackendError,
    QuartzMacAXClient,
)
from openadapt_flow.backends.remote_display import (  # noqa: E402
    MacWindowClient,
    WindowInfo,
)
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.ir import StructuralLocator, Workflow  # noqa: E402
from openadapt_flow.recorder import Recorder  # noqa: E402
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402
from scripts.qualify_macos_textedit import (  # noqa: E402
    _cleanup_failures,
    _close_target,
    _open_isolated,
    _textedit_pids,
    _wait_for_matches,
    file_oracle,
)

TEXTEDIT_APP = "TextEdit"
HEALTHY_TRIALS = 3
WRONG_IDENTITY_TRIALS = 3
AMBIGUITY_TRIALS = 3
IDENTITY = "MG4408 Okafor, Philip DOB 1966-01-17"
WRONG_IDENTITY = "MG44O8 Okafor, Philip DOB 1966-01-17"
TEXT_VIEW_LOCATOR = StructuralLocator(automation_id="First Text View", role="textbox")


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _candidate_state() -> dict[str, Any]:
    """Bind the run to a clean, exact commit and tree."""
    try:
        sha = _git(["rev-parse", "HEAD"])
        tree = _git(["rev-parse", "HEAD^{tree}"])
        dirty = _git(["status", "--porcelain", "--untracked-files=all"])
    except Exception as error:  # noqa: BLE001
        return {
            "git_sha": None,
            "git_tree": None,
            "git_dirty": None,
            "git_state_error": str(error),
            "flow_version": __version__,
        }
    return {
        "git_sha": sha,
        "git_tree": tree,
        "git_dirty": bool(dirty),
        "dirty_paths": [line for line in dirty.splitlines() if line],
        "flow_version": __version__,
    }


def _permissions(client: MacWindowClient) -> dict[str, bool]:
    return {
        "screen_recording": client.capture_trusted(),
        "accessibility": client.input_trusted(),
    }


def _wait_for_ax_body(
    client: MacWindowClient,
    title: str,
    expected_identity: str,
    *,
    timeout_s: float = 8.0,
) -> tuple[WindowInfo, MacOSBackend, tuple[int, int]]:
    """Return only after one live AX text view exposes the exact file identity."""
    window = _wait_for_matches(client, title, count=1, timeout_s=20.0)[0]
    backend = MacOSBackend(
        client,
        app=TEXTEDIT_APP,
        window_title=title,
        settle_s=0.01,
        foreground_settle_s=0.05,
        ax_client=QuartzMacAXClient(),
    )
    backend.screenshot()
    ax = QuartzMacAXClient()
    deadline = time.monotonic() + timeout_s
    last_observed: str | None = None
    while time.monotonic() < deadline:
        candidates = ax.find_candidates(
            window.pid,
            window.title,
            TEXT_VIEW_LOCATOR,
            limit=4000,
        )
        if len(candidates.candidates) == 1 and not candidates.truncated:
            element = candidates.candidates[0]
            ex, ey, ew, eh = element.bounds
            wx, wy, _ww, _wh = window.bounds
            point = (
                int((ex + ew / 2 - wx) * backend._scale_x),
                int((ey + eh / 2 - wy) * backend._scale_y),
            )
            last_observed = backend.structured_text_at(*point)
            if last_observed == expected_identity:
                return window, backend, point
        time.sleep(0.15)
    raise RuntimeError(
        "live AX text view did not settle to the exact expected identity "
        f"(observed={last_observed!r})"
    )


def _identity_summary(report: Any) -> dict[str, Any]:
    """Extract the first identity-armed result without retaining PHI."""
    for result in report.results:
        if result.identity is not None:
            identity = result.identity
            return {
                "status": str(identity.status),
                "mode": identity.mode,
                "safety_halt": result.safety_halt,
            }
    return {"status": None, "mode": None, "safety_halt": False}


def _resolution_rungs(report: Any) -> list[str]:
    return [
        result.resolution.rung
        for result in report.results
        if result.resolution is not None
    ]


def _record_and_compile(
    client: MacWindowClient,
    root: Path,
    title: str,
    cleanup_warnings: list[str],
    cleanup_receipts: list[dict[str, Any]],
) -> tuple[Workflow, Path, dict[str, Any]]:
    demo_dir = root / "demonstration"
    demo_dir.mkdir()
    path = demo_dir / f"{title}.txt"
    path.write_text(IDENTITY + "\n", encoding="utf-8")
    pid: int | None = None
    try:
        _open_isolated(path)
        window, backend, point = _wait_for_ax_body(client, title, IDENTITY)
        pid = window.pid
        recording_dir = root / "recording"
        bundle_dir = root / "bundle"
        recorder = Recorder(
            backend,
            recording_dir,
            settle_interval_s=0.05,
            settle_timeout_s=3.0,
        )
        recorder.click(*point)
        recorder.press("ControlOrMeta+a")
        recorder.type_text("OpenAdapt AX demonstration\n", param="replacement")
        recorder.press("ControlOrMeta+s")
        recorder.finish()
        workflow = compile_recording(
            recording_dir,
            bundle_dir,
            name="macos-ax-release",
        )
        first = workflow.steps[0]
        if first.anchor is None or first.anchor.structural is None:
            raise RuntimeError("compiled click lost the recorded AX locator")
        if not (
            first.anchor.structured_identity is not None
            or (
                first.anchor.identity_template is not None
                and first.anchor.identity_template.structured
            )
        ):
            raise RuntimeError("compiled click lost the recorded AX identity")
        return (
            workflow,
            bundle_dir,
            {
                "step_count": len(workflow.steps),
                "click_structural_locator_recorded": True,
                "click_structured_identity_recorded": True,
                "recording_parameter": "replacement",
                "model_calls": 0,
            },
        )
    finally:
        if pid is not None:
            _close_target(
                client,
                title,
                pid,
                warnings=cleanup_warnings,
                receipts=cleanup_receipts,
            )


def _run_healthy_trial(
    client: MacWindowClient,
    root: Path,
    title: str,
    workflow: Workflow,
    bundle_dir: Path,
    trial: int,
    cleanup_warnings: list[str],
    cleanup_receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    case_dir = root / f"healthy-{trial}"
    case_dir.mkdir()
    path = case_dir / f"{title}.txt"
    baseline = (IDENTITY + "\n").encode()
    expected = f"OpenAdapt AX healthy trial {trial}\n".encode()
    path.write_bytes(baseline)
    pid: int | None = None
    started = time.monotonic()
    try:
        _open_isolated(path)
        window, backend, _point = _wait_for_ax_body(client, title, IDENTITY)
        pid = window.pid
        report = Replayer(backend, poll_interval_s=0.05).run(
            workflow,
            params={"replacement": expected.decode()},
            bundle_dir=bundle_dir,
            run_dir=root / "runs" / f"healthy-{trial}",
        )
        oracle = file_oracle(path, expected)
        identity = _identity_summary(report)
        rungs = _resolution_rungs(report)
        passed = (
            report.success
            and oracle["status"] == "confirmed"
            and "structural" in rungs
            and identity["status"] == "verified"
            and identity["mode"] == "structured"
            and report.model_calls == 0
        )
        return {
            "trial": trial,
            "status": "passed" if passed else "failed",
            "report_success": report.success,
            "all_steps_ok": all(result.ok for result in report.results),
            "oracle": oracle,
            "resolution_rungs": rungs,
            "identity": identity,
            "model_calls": report.model_calls,
            "duration_s": round(time.monotonic() - started, 3),
        }
    except Exception as error:  # noqa: BLE001
        return {
            "trial": trial,
            "status": "failed",
            "report_success": False,
            "oracle": file_oracle(path, expected),
            "failure_type": type(error).__name__,
            "error": str(error),
            "model_calls": 0,
            "duration_s": round(time.monotonic() - started, 3),
        }
    finally:
        if pid is not None:
            _close_target(
                client,
                title,
                pid,
                warnings=cleanup_warnings,
                receipts=cleanup_receipts,
            )


def _run_wrong_identity_trial(
    client: MacWindowClient,
    root: Path,
    title: str,
    workflow: Workflow,
    bundle_dir: Path,
    trial: int,
    cleanup_warnings: list[str],
    cleanup_receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    case_dir = root / f"wrong-identity-{trial}"
    case_dir.mkdir()
    path = case_dir / f"{title}.txt"
    baseline = (WRONG_IDENTITY + "\n").encode()
    attempted = f"MUST NOT WRITE {trial}\n"
    path.write_bytes(baseline)
    pid: int | None = None
    started = time.monotonic()
    try:
        _open_isolated(path)
        window, backend, _point = _wait_for_ax_body(
            client,
            title,
            WRONG_IDENTITY,
        )
        pid = window.pid
        report = Replayer(backend, poll_interval_s=0.05).run(
            workflow,
            params={"replacement": attempted},
            bundle_dir=bundle_dir,
            run_dir=root / "runs" / f"wrong-identity-{trial}",
        )
        oracle = file_oracle(path, baseline)
        identity = _identity_summary(report)
        passed = (
            not report.success
            and oracle["status"] == "confirmed"
            and identity["status"] == "mismatch"
            and identity["mode"] == "structured"
            and identity["safety_halt"]
            and report.model_calls == 0
        )
        return {
            "trial": trial,
            "status": "passed" if passed else "failed",
            "report_success": report.success,
            "pre_write_halt": identity["safety_halt"],
            "oracle": oracle,
            "identity": identity,
            "model_calls": report.model_calls,
            "duration_s": round(time.monotonic() - started, 3),
        }
    except Exception as error:  # noqa: BLE001
        return {
            "trial": trial,
            "status": "failed",
            "report_success": False,
            "pre_write_halt": False,
            "oracle": file_oracle(path, baseline),
            "failure_type": type(error).__name__,
            "error": str(error),
            "model_calls": 0,
            "duration_s": round(time.monotonic() - started, 3),
        }
    finally:
        if pid is not None:
            _close_target(
                client,
                title,
                pid,
                warnings=cleanup_warnings,
                receipts=cleanup_receipts,
            )


def _run_ambiguity_trial(
    client: MacWindowClient,
    root: Path,
    title: str,
    workflow: Workflow,
    bundle_dir: Path,
    trial: int,
    cleanup_warnings: list[str],
    cleanup_receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline = (IDENTITY + "\n").encode()
    paths: list[Path] = []
    pids: dict[int, str] = {}
    started = time.monotonic()
    try:
        for suffix in ("a", "b"):
            case_dir = root / f"ambiguity-{trial}-{suffix}"
            case_dir.mkdir()
            path = case_dir / f"{title}.txt"
            path.write_bytes(baseline)
            paths.append(path)
            _open_isolated(path)
            _wait_for_matches(client, title, count=len(paths))

        matches = _wait_for_matches(client, title, count=2)
        pids.update({window.pid: window.title for window in matches})
        backend = MacOSBackend(
            client,
            app=TEXTEDIT_APP,
            window_title=title,
            settle_s=0.01,
            foreground_settle_s=0.05,
            ax_client=QuartzMacAXClient(),
        )
        refused = False
        refusal_type: str | None = None
        report_success = False
        model_calls = 0
        try:
            report = Replayer(backend, poll_interval_s=0.05).run(
                workflow,
                params={"replacement": f"MUST NOT WRITE ambiguity {trial}\n"},
                bundle_dir=bundle_dir,
                run_dir=root / "runs" / f"ambiguity-{trial}",
            )
            report_success = report.success
            model_calls = report.model_calls
            errors = [result.error or "" for result in report.results]
            refused = not report.success and any(
                "ambiguous native macOS target" in error for error in errors
            )
            refusal_type = "run_report_halt" if refused else None
        except MacOSBackendError as error:
            refused = "ambiguous native macOS target" in str(error)
            refusal_type = type(error).__name__
        oracles = [file_oracle(path, baseline) for path in paths]
        passed = (
            refused
            and not report_success
            and len(matches) == 2
            and all(oracle["status"] == "confirmed" for oracle in oracles)
            and model_calls == 0
        )
        return {
            "trial": trial,
            "status": "passed" if passed else "failed",
            "matched_windows": len(matches),
            "refused": refused,
            "refusal_type": refusal_type,
            "report_success": report_success,
            "oracles": oracles,
            "model_calls": model_calls,
            "duration_s": round(time.monotonic() - started, 3),
        }
    except Exception as error:  # noqa: BLE001
        return {
            "trial": trial,
            "status": "failed",
            "refused": False,
            "report_success": False,
            "oracles": [file_oracle(path, baseline) for path in paths],
            "failure_type": type(error).__name__,
            "error": str(error),
            "model_calls": 0,
            "duration_s": round(time.monotonic() - started, 3),
        }
    finally:
        for pid, window_title in pids.items():
            _close_target(
                client,
                window_title,
                pid,
                warnings=cleanup_warnings,
                receipts=cleanup_receipts,
            )


def evaluate_report(report: dict[str, Any]) -> dict[str, Any]:
    """Apply the release acceptance contract to a bounded aggregate report."""
    healthy = report.get("healthy_trials", [])
    wrong = report.get("wrong_identity_trials", [])
    ambiguous = report.get("ambiguity_trials", [])
    candidate = report.get("environment", {}).get("candidate", {})

    silent_incorrect = sum(
        trial.get("report_success") is True
        and trial.get("oracle", {}).get("status") != "confirmed"
        for trial in healthy
    )
    over_halts = sum(trial.get("report_success") is not True for trial in healthy)
    false_completions = sum(
        trial.get("report_success") is True for trial in [*wrong, *ambiguous]
    )
    refusal_writes = sum(
        trial.get("oracle", {}).get("status") != "confirmed" for trial in wrong
    ) + sum(
        any(item.get("status") != "confirmed" for item in trial.get("oracles", []))
        for trial in ambiguous
    )
    model_calls = sum(
        int(trial.get("model_calls", 0)) for trial in [*healthy, *wrong, *ambiguous]
    )
    healthy_semantics = all(
        trial.get("status") == "passed"
        and trial.get("report_success") is True
        and trial.get("all_steps_ok") is True
        and trial.get("oracle", {}).get("status") == "confirmed"
        and "structural" in trial.get("resolution_rungs", [])
        and trial.get("identity", {}).get("status") == "verified"
        and trial.get("identity", {}).get("mode") == "structured"
        and trial.get("model_calls") == 0
        for trial in healthy
    )
    wrong_identity_semantics = all(
        trial.get("status") == "passed"
        and trial.get("report_success") is False
        and trial.get("pre_write_halt") is True
        and trial.get("oracle", {}).get("status") == "confirmed"
        and trial.get("identity", {}).get("status") == "mismatch"
        and trial.get("identity", {}).get("mode") == "structured"
        and trial.get("model_calls") == 0
        for trial in wrong
    )
    ambiguity_semantics = all(
        trial.get("status") == "passed"
        and trial.get("report_success") is False
        and trial.get("refused") is True
        and len(trial.get("oracles", [])) == 2
        and all(item.get("status") == "confirmed" for item in trial.get("oracles", []))
        and trial.get("model_calls") == 0
        for trial in ambiguous
    )
    exact_matrix = (
        len(healthy) == HEALTHY_TRIALS
        and len(wrong) == WRONG_IDENTITY_TRIALS
        and len(ambiguous) == AMBIGUITY_TRIALS
    )
    accepted = (
        report.get("lane") == "release"
        and report.get("automatic_retry") is False
        and candidate.get("git_dirty") is False
        and isinstance(candidate.get("git_sha"), str)
        and len(candidate["git_sha"]) == 40
        and isinstance(candidate.get("git_tree"), str)
        and len(candidate["git_tree"]) == 40
        and exact_matrix
        and healthy_semantics
        and wrong_identity_semantics
        and ambiguity_semantics
        and silent_incorrect == 0
        and over_halts == 0
        and false_completions == 0
        and refusal_writes == 0
        and model_calls == 0
        and not report.get("cleanup_errors")
    )
    return {
        "decision": "accepted" if accepted else "rejected",
        "accepted": accepted,
        "candidate": candidate,
        "matrix": {
            "healthy_exact_effect_trials": len(healthy),
            "one_glyph_wrong_identity_pre_write_halts": len(wrong),
            "ambiguous_window_pre_write_halts": len(ambiguous),
            "exact_fixed_matrix": exact_matrix,
            "healthy_semantics": healthy_semantics,
            "wrong_identity_semantics": wrong_identity_semantics,
            "ambiguity_semantics": ambiguity_semantics,
        },
        "metrics": {
            "silent_incorrect_successes": silent_incorrect,
            "over_halts": over_halts,
            "false_completions": false_completions,
            "writes_after_refusal": refusal_writes,
            "model_calls": model_calls,
        },
        "oracle": report.get("oracle"),
        "failure_taxonomy": report.get("failure_taxonomy"),
        "cleanup_errors": report.get("cleanup_errors", []),
    }


def qualify() -> tuple[int, dict[str, Any]]:
    client = MacWindowClient()
    candidate = _candidate_state()
    permissions = _permissions(client)
    base: dict[str, Any] = {
        "schema_version": 1,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "task": "macOS AX record-compile-replay with exact effects and refusal",
        "lane": "release",
        "evidence_classification": "acceptance_candidate",
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "permissions": permissions,
            "candidate": candidate,
        },
        "run_count": {
            "healthy_exact_effect_trials": HEALTHY_TRIALS,
            "one_glyph_wrong_identity_trials": WRONG_IDENTITY_TRIALS,
            "ambiguous_window_trials": AMBIGUITY_TRIALS,
        },
        "automatic_retry": False,
        "oracle": (
            "exact target-file bytes read independently after replay or refusal"
        ),
        "failure_taxonomy": [
            "permission_refusal",
            "dirty_or_unbound_candidate",
            "record_or_compile_failure",
            "target_absent",
            "target_ambiguous",
            "structured_resolution_failure",
            "identity_false_accept",
            "identity_false_reject",
            "input_delivery_failure",
            "file_effect_refuted",
            "file_effect_unverifiable",
            "false_completion",
            "cleanup_failure",
        ],
        "caveats": [
            "TextEdit workflow on one named macOS host and active user session",
            "synthetic local identity/file values; no customer data",
            "resolved AX element is acted through gated point-bound physical input",
        ],
    }
    missing = [name for name, granted in permissions.items() if not granted]
    if missing:
        base.update(
            {
                "status": "blocked",
                "missing_permissions": missing,
                "healthy_trials": [],
                "wrong_identity_trials": [],
                "ambiguity_trials": [],
            }
        )
        return 2, base
    if (
        candidate.get("git_dirty") is not False
        or not isinstance(candidate.get("git_sha"), str)
        or len(candidate["git_sha"]) != 40
        or not isinstance(candidate.get("git_tree"), str)
        or len(candidate["git_tree"]) != 40
    ):
        base.update(
            {
                "status": "blocked",
                "candidate_gate": "release qualification requires a clean exact SHA",
                "healthy_trials": [],
                "wrong_identity_trials": [],
                "ambiguity_trials": [],
            }
        )
        return 2, base

    run_id = uuid.uuid4().hex[:10]
    title = f"oa-ax-release-{run_id}"
    root = Path(tempfile.mkdtemp(prefix=f"openadapt-ax-release-{run_id}-"))
    original_frontmost = client.frontmost_pid()
    textedit_pids_before = _textedit_pids()
    cleanup_warnings: list[str] = []
    cleanup_receipts: list[dict[str, Any]] = []
    healthy: list[dict[str, Any]] = []
    wrong: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    workflow_summary: dict[str, Any] = {}
    harness_error: dict[str, str] | None = None
    try:
        workflow, bundle_dir, workflow_summary = _record_and_compile(
            client,
            root,
            title,
            cleanup_warnings,
            cleanup_receipts,
        )
        healthy = [
            _run_healthy_trial(
                client,
                root,
                title,
                workflow,
                bundle_dir,
                trial,
                cleanup_warnings,
                cleanup_receipts,
            )
            for trial in range(1, HEALTHY_TRIALS + 1)
        ]
        wrong = [
            _run_wrong_identity_trial(
                client,
                root,
                title,
                workflow,
                bundle_dir,
                trial,
                cleanup_warnings,
                cleanup_receipts,
            )
            for trial in range(1, WRONG_IDENTITY_TRIALS + 1)
        ]
        ambiguous = [
            _run_ambiguity_trial(
                client,
                root,
                title,
                workflow,
                bundle_dir,
                trial,
                cleanup_warnings,
                cleanup_receipts,
            )
            for trial in range(1, AMBIGUITY_TRIALS + 1)
        ]
    except Exception as error:  # noqa: BLE001
        harness_error = {"type": type(error).__name__, "error": str(error)}
    finally:
        if original_frontmost is not None:
            client.activate(original_frontmost)
        try:
            shutil.rmtree(root)
        except Exception as error:  # noqa: BLE001
            cleanup_warnings.append(f"temporary directory cleanup {root}: {error}")

    textedit_pids_after = _textedit_pids()
    cleanup_errors = _cleanup_failures(
        cleanup_receipts,
        root_exists=root.exists(),
        textedit_pids_before=textedit_pids_before,
        textedit_pids_after=textedit_pids_after,
    )
    if harness_error is not None:
        cleanup_errors.append(
            f"harness failed: {harness_error['type']}: {harness_error['error']}"
        )
    base.update(
        {
            "workflow": workflow_summary,
            "healthy_trials": healthy,
            "wrong_identity_trials": wrong,
            "ambiguity_trials": ambiguous,
            "cleanup_errors": cleanup_errors,
            "cleanup_warnings": cleanup_warnings,
            "cleanup_receipts": cleanup_receipts,
            "cleanup_audit": {
                "temporary_root_removed": not root.exists(),
                "textedit_pids_before": sorted(textedit_pids_before),
                "textedit_pids_after": sorted(textedit_pids_after),
                "unrelated_textedit_pids_preserved": (
                    textedit_pids_before == textedit_pids_after
                ),
            },
        }
    )
    evaluation = evaluate_report(base)
    base["metrics"] = evaluation["metrics"]
    base["status"] = "passed" if evaluation["accepted"] else "failed"
    return (0 if evaluation["accepted"] else 1), base


def write_evidence(report: dict[str, Any], output: Path) -> dict[str, Any]:
    """Write the aggregate and a byte/hash-bound adjudication beside it."""
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    output.write_bytes(payload)
    evaluation = evaluate_report(report)
    adjudication = {
        **evaluation,
        "task": report["task"],
        "lane": report["lane"],
        "evidence_classification": (
            "release_lane_scoped_acceptance"
            if evaluation["accepted"]
            else "release_lane_rejected"
        ),
        "original_evidence": {
            "path": str(output.relative_to(REPO_ROOT)),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "status": report["status"],
            "preserved_byte_for_byte": True,
        },
        "scope": (
            "TextEdit on the recorded macOS host/session: real AX locator and "
            "structured identity through Recorder -> compiler -> Replayer, "
            "with exact file effects and pre-write identity/ambiguity refusal."
        ),
    }
    adjudication_path = output.with_suffix(".adjudication.json")
    adjudication_path.write_text(
        json.dumps(adjudication, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "evidence": str(output),
        "adjudication": str(adjudication_path),
        "sha256": adjudication["original_evidence"]["sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--request-permissions", action="store_true")
    args = parser.parse_args()
    client = MacWindowClient()
    if args.request_permissions:
        client.request_capture_access()
        client.request_input_access()
        print(
            "macOS permission prompts requested. Approve Screen Recording and "
            "Accessibility for the launching terminal, restart it, then rerun."
        )
        return 2

    code, report = qualify()
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.output is not None and report["status"] != "blocked":
        written = write_evidence(report, args.output.resolve())
        print(json.dumps(written, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
