#!/usr/bin/env python3
"""CI-only X11/AT-SPI qualification against one deterministic GTK workflow.

The harness starts a fresh GTK process for every trial. The backend focuses an
editable field, replaces its text through AT-SPI, and invokes a GTK button.
Success is determined only by exact bytes read independently from a file, never
by GTK state or the action-delivery receipt.

Exactly three clean, three ambiguous-target, and three stale-target trials are
counted. The latter conditions must refuse before creating the effect file.
This is scoped evidence for the in-tree fixture, not arbitrary Linux apps.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from openadapt_flow import __version__
from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.backends.linux_backend import (
    AtspiLinuxClient,
    LinuxBackend,
    LinuxBackendError,
)
from openadapt_flow.ir import StructuralHandle, StructuralLocator

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "scripts" / "fixtures" / "linux_atspi_qualification_app.py"
APP_NAME = "OpenAdapt Linux Qualification"
ENTRY_NAME = "Effect value"
BUTTON_NAME = "Write effect"
TRIALS_PER_CONDITION = 3
POLL_SECONDS = 0.05

_T = TypeVar("_T")


def exact_file_oracle(path: Path, expected: bytes) -> dict[str, Any]:
    """Confirm a final effect from exact file bytes outside the target UI."""
    try:
        observed = path.read_bytes()
    except FileNotFoundError:
        return {
            "status": "refuted",
            "reason": "effect_file_missing",
            "expected_bytes": len(expected),
        }
    except OSError as error:
        return {"status": "unverifiable", "error": str(error)}
    return {
        "status": "confirmed" if observed == expected else "refuted",
        "expected_bytes": len(expected),
        "observed_bytes": len(observed),
    }


def absent_file_oracle(path: Path) -> dict[str, Any]:
    """Confirm that a refused action produced no external file effect."""
    try:
        observed = path.read_bytes()
    except FileNotFoundError:
        return {"status": "confirmed", "expected": "absent"}
    except OSError as error:
        return {"status": "unverifiable", "error": str(error)}
    return {
        "status": "refuted",
        "expected": "absent",
        "observed_bytes": len(observed),
    }


def clean_metrics(results: list[dict[str, Any]]) -> dict[str, int]:
    """Separate false success from conservative refusal on valid inputs."""
    return {
        "effects_confirmed": sum(
            result.get("effect_oracle", {}).get("status") == "confirmed"
            for result in results
        ),
        "silent_incorrect_successes": sum(
            bool(result.get("backend_sequence_reported_success"))
            and result.get("effect_oracle", {}).get("status") != "confirmed"
            for result in results
        ),
        "over_halts": sum(
            not bool(result.get("backend_sequence_reported_success"))
            for result in results
        ),
    }


def evaluate(
    clean: list[dict[str, Any]],
    ambiguity: list[dict[str, Any]],
    stale: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate the fixed qualification matrix without touching a GUI."""
    normal = clean_metrics(clean)
    receipts = [
        receipt for result in clean for receipt in result.get("delivery_receipts", [])
    ]
    native_delivery_receipts = sum(
        bool(receipt.get("native"))
        and not bool(receipt.get("outcome_verified"))
        and str(receipt.get("operation", "")).startswith("atspi_")
        for receipt in receipts
    )
    ambiguity_passes = sum(
        bool(result.get("refused"))
        and result.get("effect_oracle", {}).get("status") == "confirmed"
        for result in ambiguity
    )
    stale_passes = sum(
        bool(result.get("refused"))
        and result.get("effect_oracle", {}).get("status") == "confirmed"
        for result in stale
    )
    refusal_failures = len(ambiguity) - ambiguity_passes + len(stale) - stale_passes
    accepted = all(
        (
            len(clean) == TRIALS_PER_CONDITION,
            len(ambiguity) == TRIALS_PER_CONDITION,
            len(stale) == TRIALS_PER_CONDITION,
            normal["effects_confirmed"] == TRIALS_PER_CONDITION,
            normal["silent_incorrect_successes"] == 0,
            normal["over_halts"] == 0,
            len(receipts) == TRIALS_PER_CONDITION * 2,
            native_delivery_receipts == TRIALS_PER_CONDITION * 2,
            ambiguity_passes == TRIALS_PER_CONDITION,
            stale_passes == TRIALS_PER_CONDITION,
            refusal_failures == 0,
        )
    )
    return {
        "accepted": accepted,
        "clean_trials": len(clean),
        "clean_effects_confirmed": normal["effects_confirmed"],
        "silent_incorrect_successes": normal["silent_incorrect_successes"],
        "over_halts": normal["over_halts"],
        "native_delivery_only_receipts": native_delivery_receipts,
        "ambiguity_refusals_confirmed": ambiguity_passes,
        "stale_target_refusals_confirmed": stale_passes,
        "refusal_condition_failures": refusal_failures,
        "operator_interventions": 0,
        "model_calls": 0,
    }


def _wait_until(
    predicate: Callable[[], _T | None],
    *,
    timeout_s: float,
    description: str,
) -> _T:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as error:  # noqa: BLE001 - transient accessibility race
            last_error = error
        time.sleep(POLL_SECONDS)
    detail = f": {last_error}" if last_error is not None else ""
    raise RuntimeError(f"timed out waiting for {description}{detail}")


def _wait_handle(
    backend: LinuxBackend,
    locator: StructuralLocator,
    *,
    different_from: str | None = None,
) -> StructuralHandle:
    def resolve() -> StructuralHandle | None:
        handle = backend.locate_structural(locator)
        if handle is None:
            return None
        if different_from is not None and handle.target_fingerprint == different_from:
            return None
        return handle

    return _wait_until(resolve, timeout_s=10, description=f"AT-SPI target {locator}")


def _wait_exact_window(client: AtspiLinuxClient, title: str) -> None:
    def unique() -> bool:
        return len(client.find_windows(APP_NAME, title)) == 1

    try:
        _wait_until(unique, timeout_s=10, description=f"exact GTK window {title!r}")
    except RuntimeError as error:
        # Preserve a bounded discovery snapshot in failed evidence. This does
        # not relax the exact app/title selector or timeout; it makes an
        # accessibility-registration defect diagnosable from the retained
        # artifact instead of returning nine indistinguishable timeouts.
        discovered: list[dict[str, Any]] = []
        desktop = client._desktop()
        for application in client._children(desktop)[:20]:
            app_name = client._call(application, "get_name", default=None)
            windows = [
                client._call(window, "get_name", default=None)
                for window in client._children(application)[:20]
            ]
            discovered.append({"application": app_name, "windows": windows})
        raise RuntimeError(
            f"{error}; discovered AT-SPI applications/windows={discovered!r}"
        ) from error


def _launch(
    root: Path,
    *,
    condition: str,
    trial: int,
    run_id: str,
) -> tuple[subprocess.Popen[str], str, Path, Path]:
    title = f"OpenAdapt Linux {condition} {trial} {run_id}"
    effect_path = root / f"{condition}-{trial}.effect"
    control_path = root / f"{condition}-{trial}.control"
    process = subprocess.Popen(
        [
            sys.executable,
            str(FIXTURE),
            "--title",
            title,
            "--effect-path",
            str(effect_path),
            "--control-path",
            str(control_path),
            "--mode",
            condition,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    return process, title, effect_path, control_path


def _cleanup(process: subprocess.Popen[str]) -> dict[str, Any]:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    stdout, stderr = process.communicate(timeout=1)
    return {
        "pid": process.pid,
        "returncode": process.returncode,
        "verified_absent": process.poll() is not None,
        "stdout": stdout[-1000:],
        "stderr": stderr[-2000:],
    }


def _clean_trial(
    client: AtspiLinuxClient,
    root: Path,
    run_id: str,
    trial: int,
) -> dict[str, Any]:
    started = time.monotonic()
    process, title, effect_path, _ = _launch(
        root, condition="clean", trial=trial, run_id=run_id
    )
    expected = f"linux-atspi-effect-{run_id}-{trial}".encode()
    result: dict[str, Any] = {
        "trial": trial,
        "condition": "clean",
        "expected_effect": expected.decode(),
        "backend_sequence_reported_success": False,
        "delivery_receipts": [],
    }
    try:
        _wait_exact_window(client, title)
        backend = LinuxBackend(client, app=APP_NAME, window_title=title)
        entry_locator = StructuralLocator(
            role="textbox", name=ENTRY_NAME, window_name=title
        )
        entry = _wait_handle(backend, entry_locator)
        focus = backend.act_structural(entry_locator, entry)
        backend.type_text(expected.decode())

        button_locator = StructuralLocator(
            role="button", name=BUTTON_NAME, window_name=title
        )
        button = _wait_handle(backend, button_locator)
        invoke = backend.act_structural(button_locator, button)
        result["delivery_receipts"] = [
            focus.model_dump(mode="json"),
            invoke.model_dump(mode="json"),
        ]
        result["backend_sequence_reported_success"] = True
        result["effect_oracle"] = _wait_until(
            lambda: (
                verdict
                if (verdict := exact_file_oracle(effect_path, expected))["status"]
                == "confirmed"
                else None
            ),
            timeout_s=5,
            description="exact external file effect",
        )
    except Exception as error:  # noqa: BLE001 - evidence records exact failure
        result["error"] = f"{type(error).__name__}: {error}"
        result["effect_oracle"] = exact_file_oracle(effect_path, expected)
    finally:
        result["cleanup"] = _cleanup(process)
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
    return result


def _ambiguity_trial(
    client: AtspiLinuxClient,
    root: Path,
    run_id: str,
    trial: int,
) -> dict[str, Any]:
    process, title, effect_path, _ = _launch(
        root, condition="ambiguous", trial=trial, run_id=run_id
    )
    result: dict[str, Any] = {
        "trial": trial,
        "condition": "ambiguous",
        "refused": False,
    }
    try:
        _wait_exact_window(client, title)
        backend = LinuxBackend(client, app=APP_NAME, window_title=title)
        locator = StructuralLocator(role="button", name=BUTTON_NAME, window_name=title)
        try:
            backend.locate_structural(locator)
        except StructuralResolutionRefused as error:
            result["refused"] = "ambiguous" in str(error).casefold()
            result["refusal"] = str(error)
        time.sleep(0.1)
        result["effect_oracle"] = absent_file_oracle(effect_path)
    except Exception as error:  # noqa: BLE001
        result["error"] = f"{type(error).__name__}: {error}"
        result["effect_oracle"] = absent_file_oracle(effect_path)
    finally:
        result["cleanup"] = _cleanup(process)
    return result


def _stale_trial(
    client: AtspiLinuxClient,
    root: Path,
    run_id: str,
    trial: int,
) -> dict[str, Any]:
    process, title, effect_path, control_path = _launch(
        root, condition="stale", trial=trial, run_id=run_id
    )
    result: dict[str, Any] = {
        "trial": trial,
        "condition": "stale",
        "refused": False,
    }
    try:
        _wait_exact_window(client, title)
        backend = LinuxBackend(client, app=APP_NAME, window_title=title)
        locator = StructuralLocator(role="button", name=BUTTON_NAME, window_name=title)
        old_handle = _wait_handle(backend, locator)
        control_path.write_text("replace", encoding="utf-8")
        _wait_handle(
            backend,
            locator,
            different_from=old_handle.target_fingerprint,
        )
        try:
            backend.act_structural(locator, old_handle)
        except LinuxBackendError as error:
            message = str(error).casefold()
            result["refused"] = "changed" in message or "stale" in message
            result["refusal"] = str(error)
        time.sleep(0.1)
        result["effect_oracle"] = absent_file_oracle(effect_path)
    except Exception as error:  # noqa: BLE001
        result["error"] = f"{type(error).__name__}: {error}"
        result["effect_oracle"] = absent_file_oracle(effect_path)
    finally:
        result["cleanup"] = _cleanup(process)
    return result


def _git_state() -> dict[str, Any]:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return {"git_sha": sha, "git_dirty": bool(status), "dirty_paths": status}


def qualify() -> tuple[int, dict[str, Any]]:
    candidate = _git_state()
    base: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "in-tree GTK3 file-write fixture on isolated X11/AT-SPI",
        "trial_contract": {
            "clean": TRIALS_PER_CONDITION,
            "ambiguity": TRIALS_PER_CONDITION,
            "stale_target": TRIALS_PER_CONDITION,
            "automatic_retry": False,
        },
        "environment": {
            "flow_version": __version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "display": os.environ.get("DISPLAY"),
            "xdg_session_type": os.environ.get("XDG_SESSION_TYPE"),
            "session_dbus": bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")),
            "candidate": candidate,
        },
        "oracle": {
            "type": "independent exact file bytes / independently confirmed absence",
            "target_ui_used_as_oracle": False,
        },
        "caveats": [
            "Acceptance is bounded to this GTK3 fixture and CI Xvfb image.",
            "It does not establish Wayland or arbitrary third-party app support.",
            "Native receipts prove input delivery only; the file oracle proves effect.",
        ],
    }
    if candidate["git_dirty"]:
        base.update(
            {
                "status": "blocked",
                "reason": "qualification candidate is dirty",
                "results": {},
            }
        )
        return 2, base
    if (
        os.environ.get("XDG_SESSION_TYPE", "").casefold() != "x11"
        or not os.environ.get("DISPLAY")
        or not os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    ):
        base.update(
            {
                "status": "blocked",
                "reason": "isolated X11 display and session D-Bus are required",
                "results": {},
            }
        )
        return 2, base

    run_id = uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="openadapt-linux-atspi-") as temp:
        root = Path(temp)
        client = AtspiLinuxClient()
        clean = [
            _clean_trial(client, root, run_id, trial)
            for trial in range(1, TRIALS_PER_CONDITION + 1)
        ]
        ambiguity = [
            _ambiguity_trial(client, root, run_id, trial)
            for trial in range(1, TRIALS_PER_CONDITION + 1)
        ]
        stale = [
            _stale_trial(client, root, run_id, trial)
            for trial in range(1, TRIALS_PER_CONDITION + 1)
        ]

    metrics = evaluate(clean, ambiguity, stale)
    cleanup_ok = all(
        result.get("cleanup", {}).get("verified_absent")
        for result in [*clean, *ambiguity, *stale]
    )
    accepted = bool(metrics["accepted"] and cleanup_ok)
    base.update(
        {
            "status": "passed" if accepted else "failed",
            "run_id": run_id,
            "metrics": metrics,
            "cleanup_verified": cleanup_ok,
            "results": {
                "clean": clean,
                "ambiguity": ambiguity,
                "stale_target": stale,
            },
        }
    )
    return (0 if accepted else 1), base


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    code, report = qualify()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["metrics"] if "metrics" in report else report, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
