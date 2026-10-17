#!/usr/bin/env python3
"""Opt-in native macOS TextEdit qualification with independent file oracle.

Nothing runs unless both macOS permissions are present. Each trial launches a
new isolated TextEdit process against a unique file under /tmp, replaces its
contents using the MacOSBackend, saves with Command+S, and confirms the exact
bytes from disk. The ambiguity trial opens two isolated documents whose titles
share a selector and requires a fail-closed refusal with both files unchanged.

This is local substrate evidence, not a general reliability claim. It records
the task, environment, run count, oracle, failures, cleanup state, and caveats.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openadapt_flow import __version__
from openadapt_flow.backends.macos_backend import MacOSBackend, MacOSBackendError
from openadapt_flow.backends.remote_display import MacWindowClient, WindowInfo

MIN_TRIALS = 3
TEXTEDIT_APP = "TextEdit"
REPO_ROOT = Path(__file__).resolve().parents[1]


def file_oracle(path: Path, expected: bytes) -> dict[str, Any]:
    """Independent system-of-record verdict for the saved TextEdit document."""
    try:
        observed = path.read_bytes()
    except OSError as error:
        return {"status": "unverifiable", "error": str(error)}
    return {
        "status": "confirmed" if observed == expected else "refuted",
        "expected_bytes": len(expected),
        "observed_bytes": len(observed),
    }


def _oracle_failure_type(verdict: dict[str, Any]) -> str | None:
    status = verdict.get("status")
    if status == "confirmed":
        return None
    if status == "refuted":
        return "file_effect_refuted"
    return "file_effect_unverifiable"


def _trial_metrics(results: list[dict[str, Any]]) -> dict[str, int]:
    """Classify normal trials from action receipt and independent oracle.

    A backend sequence that returned successfully but whose final file effect
    is refuted or unverifiable is a silent incorrect success. A refusal or
    exception before the backend sequence reported success is an over-halt.
    These categories are intentionally disjoint.
    """
    return {
        "silent_incorrect_successes": sum(
            bool(result.get("backend_sequence_reported_success"))
            and result.get("oracle", {}).get("status") != "confirmed"
            for result in results
        ),
        "over_halts": sum(
            not bool(result.get("backend_sequence_reported_success"))
            for result in results
        ),
    }


def _wait_for_matches(
    client: MacWindowClient,
    title: str,
    *,
    count: int,
    timeout_s: float = 10.0,
) -> list[WindowInfo]:
    """Wait for a stable, on-screen set of matching windows.

    Launch Services can briefly publish a document title while TextEdit is
    still restoring/reordering windows. Requiring three identical window-id,
    pid, title, bounds, and visibility observations prevents the harness from
    treating that transient title as an input-ready target.
    """
    deadline = time.monotonic() + timeout_s
    matches: list[WindowInfo] = []
    prior_signature: tuple[tuple[object, ...], ...] | None = None
    stable_observations = 0
    while time.monotonic() < deadline:
        matches = client.find_windows(TEXTEDIT_APP, title)
        signature = tuple(
            (
                window.window_id,
                window.pid,
                window.title,
                window.bounds,
                window.on_screen,
            )
            for window in matches
        )
        if len(matches) == count and all(window.on_screen for window in matches):
            stable_observations = (
                stable_observations + 1 if signature == prior_signature else 1
            )
            if stable_observations >= 3:
                return matches
        else:
            stable_observations = 0
        prior_signature = signature
        time.sleep(0.1)
    raise RuntimeError(
        f"expected {count} TextEdit window(s) matching {title!r}; found {len(matches)}"
    )


def _open_isolated(path: Path) -> None:
    subprocess.run(
        ["open", "-n", "-a", TEXTEDIT_APP, str(path)],
        check=True,
        capture_output=True,
        timeout=10,
    )


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_for_process_absence(pid: int, timeout_s: float) -> bool:
    """Return only after the exact PID is absent or the deadline expires."""
    deadline = time.monotonic() + timeout_s
    while True:
        if not _process_exists(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _terminate_isolated(pid: int) -> dict[str, Any]:
    """Terminate an exact harness PID and return an independently checked receipt."""
    receipt: dict[str, Any] = {
        "pid": pid,
        "initially_present": _process_exists(pid),
        "sigterm_sent": False,
        "sigkill_sent": False,
        "verified_absent": False,
    }
    if not receipt["initially_present"]:
        receipt.update({"verified_absent": True, "exit_stage": "already_absent"})
        return receipt

    try:
        os.kill(pid, signal.SIGTERM)
        receipt["sigterm_sent"] = True
    except ProcessLookupError:
        verified_absent = not _process_exists(pid)
        receipt.update(
            {
                "verified_absent": verified_absent,
                "exit_stage": "before_sigterm",
            }
        )
        if not verified_absent:
            receipt["error"] = "PID was present after SIGTERM lookup race"
        return receipt
    if _wait_for_process_absence(pid, 2.0):
        receipt.update({"verified_absent": True, "exit_stage": "after_sigterm"})
        return receipt

    try:
        os.kill(pid, signal.SIGKILL)
        receipt["sigkill_sent"] = True
    except ProcessLookupError:
        verified_absent = not _process_exists(pid)
        receipt.update(
            {
                "verified_absent": verified_absent,
                "exit_stage": "before_sigkill",
            }
        )
        if not verified_absent:
            receipt["error"] = "PID was present after SIGKILL lookup race"
        return receipt
    if _wait_for_process_absence(pid, 2.0):
        receipt.update({"verified_absent": True, "exit_stage": "after_sigkill"})
        return receipt

    receipt.update(
        {
            "exit_stage": "still_present_after_sigkill",
            "error": "exact harness PID remained after SIGTERM and SIGKILL",
        }
    )
    return receipt


def _textedit_pids() -> set[int]:
    """Return exact TextEdit PIDs for unrelated-process preservation auditing."""
    result = subprocess.run(
        ["pgrep", "-x", TEXTEDIT_APP],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(
            f"TextEdit PID audit failed with exit {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    return {int(line) for line in result.stdout.splitlines() if line.strip()}


def _close_target(
    client: MacWindowClient,
    title: str,
    pid: int,
    *,
    warnings: list[str],
    receipts: list[dict[str, Any]],
) -> None:
    graceful_close: dict[str, Any] = {"status": "not_attempted"}
    try:
        # Re-opening an already-open file makes that exact document the main
        # window of its isolated TextEdit process; the backend still verifies
        # the exact topmost window before emitting Command+W.
        matches = client.find_windows(TEXTEDIT_APP, title)
        if len(matches) == 1:
            backend = MacOSBackend(
                client,
                app=TEXTEDIT_APP,
                window_title=title,
                settle_s=0.01,
            )
            backend.press("ControlOrMeta+w")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not client.find_windows(TEXTEDIT_APP, title):
                    graceful_close = {"status": "window_absent"}
                    break
                time.sleep(0.05)
            else:
                graceful_close = {"status": "window_still_present"}
                warnings.append(
                    f"window cleanup {title!r}: graceful Command+W did not "
                    "remove the window; exact-PID fallback required"
                )
        else:
            graceful_close = {
                "status": "selector_not_unique",
                "matched_windows": len(matches),
            }
            warnings.append(
                f"window cleanup {title!r}: selector matched {len(matches)} "
                "windows; exact-PID fallback required"
            )
    except Exception as error:  # noqa: BLE001 - cleanup continues to exact pid
        graceful_close = {"status": "refused", "error": str(error)}
        warnings.append(f"window cleanup {title!r}: {error}")
    try:
        receipt = _terminate_isolated(pid)
    except Exception as error:  # noqa: BLE001
        receipt = {
            "pid": pid,
            "verified_absent": not _process_exists(pid),
            "exit_stage": "exception",
            "error": str(error),
        }
    receipt.update({"title": title, "graceful_close": graceful_close})
    receipts.append(receipt)


def _cleanup_failures(
    receipts: list[dict[str, Any]],
    *,
    root_exists: bool,
    textedit_pids_before: set[int],
    textedit_pids_after: set[int],
) -> list[str]:
    """Return only authoritative residue or unrelated-process changes."""
    failures = [
        f"harness PID remained after cleanup: {receipt['pid']}"
        for receipt in receipts
        if not receipt.get("verified_absent")
    ]
    if root_exists:
        failures.append("temporary qualification root remained after cleanup")
    harness_pids = {int(receipt["pid"]) for receipt in receipts}
    unrelated_after = textedit_pids_after - harness_pids
    if unrelated_after != textedit_pids_before:
        failures.append(
            "unrelated TextEdit PID set changed: "
            f"before={sorted(textedit_pids_before)} after={sorted(unrelated_after)}"
        )
    return failures


def _wait_oracle(path: Path, expected: bytes, timeout_s: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    verdict = file_oracle(path, expected)
    while verdict["status"] != "confirmed" and time.monotonic() < deadline:
        time.sleep(0.1)
        verdict = file_oracle(path, expected)
    return verdict


def _run_trial(
    client: MacWindowClient,
    root: Path,
    run_id: str,
    trial: int,
    cleanup_warnings: list[str],
    cleanup_receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    title = f"oa-macos-{run_id}-trial-{trial}"
    path = root / f"{title}.txt"
    baseline = f"baseline {trial}\n".encode()
    expected = f"OpenAdapt native macOS trial {trial} {run_id}\n".encode()
    path.write_bytes(baseline)
    pid: Optional[int] = None
    backend_sequence_reported_success = False
    started = time.monotonic()
    try:
        _open_isolated(path)
        matches = _wait_for_matches(client, title, count=1)
        pid = matches[0].pid
        backend = MacOSBackend(
            client,
            app=TEXTEDIT_APP,
            window_title=title,
            settle_s=0.01,
        )
        frame = backend.screenshot()
        backend.press("ControlOrMeta+a")
        backend.type_text(expected.decode())
        backend.press("ControlOrMeta+s")
        backend_sequence_reported_success = True
        oracle = _wait_oracle(path, expected)
        result = {
            "trial": trial,
            "status": "passed" if oracle["status"] == "confirmed" else "failed",
            "oracle": oracle,
            "backend_sequence_reported_success": True,
            "frame_bytes": len(frame),
            "duration_s": round(time.monotonic() - started, 3),
        }
        failure_type = _oracle_failure_type(oracle)
        if failure_type is not None:
            result["failure_type"] = failure_type
        return result
    except Exception as error:  # noqa: BLE001 - evidence records exact failure
        return {
            "trial": trial,
            "status": "failed",
            "failure_type": type(error).__name__,
            "error": str(error),
            "oracle": file_oracle(path, expected),
            "backend_sequence_reported_success": backend_sequence_reported_success,
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
    run_id: str,
    cleanup_warnings: list[str],
    cleanup_receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    selector = f"oa-macos-{run_id}-ambiguous"
    paths = [root / f"{selector}-{suffix}.txt" for suffix in ("a", "b")]
    baseline = b"must remain unchanged\n"
    pids: dict[str, int] = {}
    try:
        for path in paths:
            path.write_bytes(baseline)
            _open_isolated(path)
            exact = _wait_for_matches(client, path.stem, count=1)
            pids[path.stem] = exact[0].pid
        matches = _wait_for_matches(client, selector, count=2)
        backend = MacOSBackend(
            client,
            app=TEXTEDIT_APP,
            window_title=selector,
            settle_s=0.01,
        )
        try:
            backend.screenshot()
        except MacOSBackendError as error:
            refused = "ambiguous native macOS target" in str(error)
            oracles = [file_oracle(path, baseline) for path in paths]
            passed = refused and all(item["status"] == "confirmed" for item in oracles)
            return {
                "status": "passed" if passed else "failed",
                "matched_windows": len(matches),
                "refused": refused,
                "error": str(error),
                "oracles": oracles,
            }
        return {
            "status": "failed",
            "matched_windows": len(matches),
            "refused": False,
            "error": "ambiguous selector did not halt",
            "oracles": [file_oracle(path, baseline) for path in paths],
        }
    except Exception as error:  # noqa: BLE001
        return {
            "status": "failed",
            "failure_type": type(error).__name__,
            "error": str(error),
            "oracles": [file_oracle(path, baseline) for path in paths],
        }
    finally:
        for title, pid in pids.items():
            _close_target(
                client,
                title,
                pid,
                warnings=cleanup_warnings,
                receipts=cleanup_receipts,
            )


def _permission_report(client: MacWindowClient) -> dict[str, bool]:
    return {
        "screen_recording": client.capture_trusted(),
        "accessibility": client.input_trusted(),
    }


def _candidate_state(trials: int) -> dict[str, Any]:
    """Bind qualification evidence to one exact committed Flow candidate."""
    try:
        sha_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        git_sha = sha_result.stdout.strip()
        dirty_paths = [line for line in status_result.stdout.splitlines() if line]
    except Exception as error:  # noqa: BLE001 - unknown provenance cannot qualify
        return {
            "git_sha": None,
            "git_dirty": None,
            "git_state_error": str(error),
            "flow_version": __version__,
            "qualification_config": _qualification_config(trials),
        }
    return {
        "git_sha": git_sha,
        "git_dirty": bool(dirty_paths),
        "dirty_paths": dirty_paths,
        "flow_version": __version__,
        "qualification_config": _qualification_config(trials),
    }


def _qualification_config(trials: int) -> dict[str, Any]:
    return {
        "backend_kind": "macos",
        "application": TEXTEDIT_APP,
        "normal_trials": trials,
        "ambiguity_refusal_cases": 1,
        "oracle": "exact target-file bytes",
        "focused_application_proof": "system-wide AX focused application PID",
        "window_proofs": [
            "unique exact PID/title",
            "exact topmost CoreGraphics window id",
            "exact focused/main AX window",
        ],
        "text_delivery": "writable AX selected-text on exact focused element",
        "physical_fallback": False,
        "automatic_retry": False,
    }


def qualify(trials: int) -> tuple[int, dict[str, Any]]:
    client = MacWindowClient()
    permissions = _permission_report(client)
    candidate = _candidate_state(trials)
    base: dict[str, Any] = {
        "schema_version": 1,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "task": "replace and save a unique /tmp TextEdit document",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "permissions": permissions,
            "candidate": candidate,
        },
        "trials_required": trials,
        "run_count": {"normal_trials": trials, "ambiguity_refusal_cases": 1},
        "automatic_retry": False,
        "evidence_classification": "acceptance_candidate",
        "oracle": "exact bytes read independently from the target file",
        "failure_taxonomy": [
            "permission_refusal",
            "target_absent",
            "target_ambiguous",
            "wrong_foreground_window",
            "capture_failure",
            "input_delivery_failure",
            "file_effect_refuted",
            "file_effect_unverifiable",
            "cleanup_failure",
        ],
        "caveats": [
            "TextEdit only; no AX structural resolution claim",
            "synthetic local files; not a design-partner workflow",
            "one macOS host and active keyboard/session state",
        ],
    }
    missing = [name for name, granted in permissions.items() if not granted]
    if missing:
        base.update(
            {
                "status": "blocked",
                "trials_completed": 0,
                "missing_permissions": missing,
                "permission_step": (
                    "Run this script once with --request-permissions, approve "
                    "both macOS prompts for the app that launches Codex/Flow, "
                    "then restart that app and rerun the qualification."
                ),
            }
        )
        return 2, base

    if candidate.get("git_dirty") is not False:
        base.update(
            {
                "status": "blocked",
                "evidence_classification": "diagnostic_only_not_acceptance",
                "trials_completed": 0,
                "candidate_gate": (
                    "live acceptance requires an exact git SHA and a clean "
                    "worktree; commit or remove all candidate changes first"
                ),
            }
        )
        return 2, base

    run_id = uuid.uuid4().hex[:10]
    root = Path(tempfile.mkdtemp(prefix=f"openadapt-macos-{run_id}-"))
    original_frontmost = client.frontmost_pid()
    textedit_pids_before = _textedit_pids()
    cleanup_warnings: list[str] = []
    cleanup_receipts: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    ambiguity: dict[str, Any] = {"status": "not_run"}
    try:
        results = [
            _run_trial(
                client,
                root,
                run_id,
                trial,
                cleanup_warnings,
                cleanup_receipts,
            )
            for trial in range(1, trials + 1)
        ]
        ambiguity = _run_ambiguity_trial(
            client,
            root,
            run_id,
            cleanup_warnings,
            cleanup_receipts,
        )
    finally:
        if original_frontmost is not None:
            client.activate(original_frontmost)
        try:
            shutil.rmtree(root)
        except Exception as error:  # noqa: BLE001
            cleanup_warnings.append(f"temporary directory cleanup {root}: {error}")

    textedit_pids_after = _textedit_pids()
    root_exists = root.exists()
    cleanup_errors = _cleanup_failures(
        cleanup_receipts,
        root_exists=root_exists,
        textedit_pids_before=textedit_pids_before,
        textedit_pids_after=textedit_pids_after,
    )
    harness_pids = {int(receipt["pid"]) for receipt in cleanup_receipts}
    cleanup_audit = {
        "temporary_root": str(root),
        "temporary_root_exists": root_exists,
        "textedit_pids_before": sorted(textedit_pids_before),
        "textedit_pids_after": sorted(textedit_pids_after),
        "harness_pids": sorted(harness_pids),
        "unrelated_textedit_pids_after": sorted(textedit_pids_after - harness_pids),
    }

    passed = (
        all(result["status"] == "passed" for result in results)
        and ambiguity["status"] == "passed"
        and not cleanup_errors
    )
    base.update(
        {
            "status": "passed" if passed else "failed",
            "trials_completed": len(results),
            "trials": results,
            "ambiguity_trial": ambiguity,
            "cleanup_errors": cleanup_errors,
            "cleanup_warnings": cleanup_warnings,
            "cleanup_receipts": cleanup_receipts,
            "cleanup_audit": cleanup_audit,
            **_trial_metrics(results),
        }
    )
    return (0 if passed else 1), base


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=MIN_TRIALS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--request-permissions", action="store_true")
    args = parser.parse_args()
    if args.trials < MIN_TRIALS:
        parser.error(f"--trials must be >= {MIN_TRIALS}")

    client = MacWindowClient()
    if args.request_permissions:
        client.request_capture_access()
        client.request_input_access()
        print(
            "macOS permission prompts requested. Approve Screen & System Audio "
            "Recording and Accessibility for the app that launches this process, "
            "restart it, then rerun without --request-permissions."
        )
        return 2

    code, report = qualify(args.trials)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
