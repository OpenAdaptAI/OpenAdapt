#!/usr/bin/env python3
"""Live-AX qualification of the native macOS structured-identity capability.

This exercises the macOS backend's OPTIONAL structured-layer contract --
:class:`~openadapt_flow.backend.IdentityBackend` (structured a11y text under a
point) and :class:`~openadapt_flow.backend.StructuralActionBackend` (record-time
locator + replay-time deterministic element re-find) -- against the REAL macOS
Accessibility (AX) tree of a real TextEdit document on this Apple-Silicon host.
No fakes: the backend reads the live AX tree through :class:`QuartzMacAXClient`.

It proves, on live AX:

* ``structured_text_at`` returns the REAL characters under a point (the digit
  ``0`` in ``MG4408``/``01-17`` and the letter ``O`` in ``Okafor`` -- the exact
  glyph fidelity OCR cannot guarantee), catching the same-name/same-DOB sibling
  the OCR band collapses;
* ``structural_locator_at`` records a stable AX locator (the text view's
  ``AXIdentifier`` + role) scoped to the exact window;
* ``locate_structural`` re-finds the UNIQUE element and returns a fingerprinted
  handle (``candidate_count == 1``); a nonexistent locator and an out-of-window
  point are ordinary misses (safe fall-through), never a wrong target.

The ambiguity / truncation / scope-escape REFUSALS (halt-don't-guess) are
exercised deterministically in ``tests/test_macos_structural.py``; a live app
cannot be forced to publish two byte-identical durable controls on demand, so
this run proves the positive path plus the live scope negative controls, and
records that the refusal contract is unit-covered.

This is DEVELOPMENT-LANE (local engineering) evidence for an Experimental
capability, not a release-lane scoped acceptance: it runs once on one host with
one app, requires Screen Recording + Accessibility, and its status is honest
about that boundary. It refuses (``blocked``) before opening anything if a
permission is missing, and it cleans up the exact PID it launched while
preserving every unrelated TextEdit process.

Usage:
    python scripts/qualify_macos_ax_identity.py            # print + write evidence
    python scripts/qualify_macos_ax_identity.py --no-write # print only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from openadapt_flow.backends.macos_backend import (  # noqa: E402
    MacOSBackend,
    QuartzMacAXClient,
)
from openadapt_flow.backends.remote_display import (  # noqa: E402
    MacWindowClient,
    WindowInfo,
)
from openadapt_flow.ir import StructuralLocator  # noqa: E402

TEXTEDIT_APP = "TextEdit"
# A glyph-confusable identity band: it contains BOTH a literal digit '0'
# (MG4408 / 01-17) and a literal letter 'O' (Okafor). structured_text_at must
# return each glyph exactly -- the fidelity that separates the same-name sibling
# an OCR band collapses to identical pixels.
IDENTITY = "MG4408 Okafor, Philip DOB 1966-01-17"

EVIDENCE_DIR = REPO_ROOT / "benchmark" / "macos_native"
EVIDENCE_PATH = EVIDENCE_DIR / "ax_identity_20260720.json"
ADJUDICATION_PATH = EVIDENCE_DIR / "ax_identity_20260720.adjudication.json"


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=10)


def _candidate_state() -> dict[str, Any]:
    sha = _run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]).stdout.strip()
    dirty = _run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"]).stdout
    from openadapt_flow import __version__

    return {
        "git_sha": sha or "unknown",
        "git_dirty": bool(dirty.strip()),
        "flow_version": __version__,
    }


def _textedit_pids() -> set[int]:
    result = _run(["pgrep", "-x", TEXTEDIT_APP])
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"TextEdit PID audit failed: {result.stderr.strip()}")
    return {int(line) for line in result.stdout.splitlines() if line.strip()}


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_stable_window(
    client: MacWindowClient, name: str, timeout_s: float = 20.0
) -> Optional[WindowInfo]:
    """Return the unique on-screen window once its geometry is stable.

    TextEdit animates a document window open; requiring several identical
    (id, pid, title, bounds, on_screen) observations keeps the AX read and the
    capture from racing the still-moving window (which would push the text area
    outside the captured frame).
    """
    deadline = time.monotonic() + timeout_s
    prior: Any = None
    stable = 0
    while time.monotonic() < deadline:
        matches = client.find_windows(TEXTEDIT_APP, name)
        signature = tuple(
            (w.window_id, w.pid, w.title, w.bounds, w.on_screen) for w in matches
        )
        if len(matches) == 1 and matches[0].on_screen:
            stable = stable + 1 if signature == prior else 1
            if stable >= 4:
                return matches[0]
        else:
            stable = 0
        prior = signature
        time.sleep(0.15)
    return None


def _terminate(pid: int) -> dict[str, Any]:
    receipt: dict[str, Any] = {"pid": pid, "initially_present": _process_exists(pid)}
    if not receipt["initially_present"]:
        receipt["verified_absent"] = True
        return receipt
    for sig, deadline in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 3.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            break
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if not _process_exists(pid):
                break
            time.sleep(0.05)
        if not _process_exists(pid):
            break
    receipt["verified_absent"] = not _process_exists(pid)
    return receipt


def _blocked(reason: str, missing: list[str], candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": "macos_ax_structured_identity",
        "status": "blocked",
        "evidence_classification": "diagnostic_only_not_acceptance",
        "reason": reason,
        "missing_permissions": missing,
        "environment": {"candidate": candidate},
    }


def qualify(*, client: Optional[MacWindowClient] = None) -> dict[str, Any]:
    client = client or MacWindowClient()
    candidate = _candidate_state()

    missing = []
    if not client.capture_trusted():
        missing.append("screen_recording")
    if not client.input_trusted():
        missing.append("accessibility")
    if missing:
        return _blocked("required macOS permissions are not granted", missing, candidate)

    pids_before = _textedit_pids()
    root = Path(tempfile.mkdtemp(prefix="oa-axid-"))
    token = secrets.token_hex(4)
    name = f"oa-axid-{token}.txt"
    doc = root / name
    doc.write_text(IDENTITY + "\n")

    report: dict[str, Any] = {
        "task": "macos_ax_structured_identity",
        "status": "failed",
        "evidence_classification": "diagnostic_local_engineering_evidence",
        "lane": "development",
        "environment": {
            "os": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "candidate": candidate,
        },
        "application": TEXTEDIT_APP,
        "identity_under_test": {"expected": IDENTITY},
        "model_calls": 0,
        "capabilities_proven": [],
        "refusal_contract": {
            "note": (
                "ambiguity / truncation / scope-escape refusals (halt-don't-guess) "
                "are exercised deterministically in tests/test_macos_structural.py; "
                "a live app cannot be forced to publish two byte-identical durable "
                "controls, so this run proves the positive path plus live scope "
                "negative controls"
            ),
            "unit_covered_cases": [
                "ambiguous enumeration -> StructuralResolutionRefused",
                "truncated enumeration -> StructuralResolutionRefused",
                "candidate outside app/window scope -> StructuralResolutionRefused",
                "element outside captured window rect -> ordinary miss",
            ],
        },
    }

    launched_pid: Optional[int] = None
    try:
        subprocess.run(
            ["open", "-n", "-a", TEXTEDIT_APP, str(doc)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        window = _wait_stable_window(client, name)
        if window is None:
            report["error"] = "no stable unique TextEdit window appeared"
            return report
        launched_pid = window.pid

        backend = MacOSBackend(
            client,
            app=TEXTEDIT_APP,
            window_title=name,
            ax_client=QuartzMacAXClient(),
        )
        backend.screenshot()
        viewport = backend.viewport
        report["window"] = {
            "title": window.title,
            "screen_bounds": list(window.bounds),
            "viewport": list(viewport),
            "scale": [round(backend._scale_x, 4), round(backend._scale_y, 4)],
        }

        ax = QuartzMacAXClient()
        text_view_locator = StructuralLocator(
            automation_id="First Text View", role="textbox"
        )

        # Enumerate the exact text view to derive a probe point and prove the
        # window-scoped, unique, non-truncated enumeration on live AX. macOS
        # document restoration can briefly show autosaved text before the opened
        # file's bytes land, so poll until the live AX value settles to the
        # file's exact content (or a timeout, recording whatever was observed).
        observed: Optional[str] = None
        px = py = 0
        enum = ax.find_candidates(window.pid, window.title, text_view_locator, limit=4000)
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            enum = ax.find_candidates(
                window.pid, window.title, text_view_locator, limit=4000
            )
            if len(enum.candidates) != 1 or enum.truncated:
                time.sleep(0.15)
                continue
            ex, ey, ew, eh = enum.candidates[0].bounds
            wx, wy, _ww, _wh = window.bounds
            px = int((ex + ew / 2 - wx) * backend._scale_x)
            py = int((ey + eh / 2 - wy) * backend._scale_y)
            observed = backend.structured_text_at(px, py)
            if observed == IDENTITY:
                break
            time.sleep(0.15)

        report["enumeration"] = {
            "candidate_count": len(enum.candidates),
            "truncated": enum.truncated,
        }
        if len(enum.candidates) != 1 or enum.truncated:
            report["error"] = "expected exactly one non-truncated text-view candidate"
            return report
        report["probe_point_px"] = [px, py]
        exact = observed == IDENTITY
        report["identity_under_test"].update(
            {
                "observed": observed,
                "exact_match": exact,
                "glyph_fidelity": {
                    "contains_digit_zero": bool(observed and "0" in observed),
                    "contains_letter_O": bool(observed and "O" in observed),
                },
            }
        )

        locator = backend.structural_locator_at(px, py)
        report["structural_locator"] = (
            None if locator is None else locator.model_dump()
        )

        handle = backend.locate_structural(locator) if locator is not None else None
        report["structural_handle"] = (
            None
            if handle is None
            else {
                "point": list(handle.point),
                "region": list(handle.region) if handle.region else None,
                "candidate_count": handle.candidate_count,
                "target_fingerprint": handle.target_fingerprint,
                "supported_operations": list(handle.supported_operations),
            }
        )

        # Live negative controls: a nonexistent locator and an out-of-window
        # point must be ordinary misses (safe fall-through), never a wrong hit.
        nonexistent_miss = (
            backend.locate_structural(
                StructuralLocator(automation_id="__no_such_ax_id__")
            )
            is None
        )
        out_of_window_none = backend.structured_text_at(viewport[0] - 1, 0) is None
        report["negative_controls"] = {
            "nonexistent_locator_is_miss": nonexistent_miss,
            "out_of_window_point_text_is_none": out_of_window_none,
        }

        proven = []
        if exact and observed and "0" in observed and "O" in observed:
            proven.append("IdentityBackend.structured_text_at")
        if locator is not None and locator.automation_id:
            proven.append("StructuralActionBackend.structural_locator_at")
        if (
            handle is not None
            and handle.candidate_count == 1
            and handle.target_fingerprint
        ):
            proven.append("StructuralActionBackend.locate_structural")
        report["capabilities_proven"] = proven

        report["status"] = (
            "passed"
            if (
                len(proven) == 3
                and nonexistent_miss
                and out_of_window_none
                and len(enum.candidates) == 1
            )
            else "failed"
        )
        return report
    finally:
        cleanup: dict[str, Any] = {}
        if launched_pid is not None:
            cleanup["terminate"] = _terminate(launched_pid)
        pids_after = _textedit_pids()
        harness = {launched_pid} if launched_pid is not None else set()
        cleanup["unrelated_textedit_pids_preserved"] = (
            (pids_before - harness) == (pids_after - harness)
        )
        cleanup["unrelated_textedit_pids_after"] = sorted(pids_after - harness)
        try:
            for child in root.iterdir():
                child.unlink()
            root.rmdir()
            cleanup["temporary_root_removed"] = True
        except OSError:
            cleanup["temporary_root_removed"] = False
        report["cleanup"] = cleanup


def _write_evidence(report: dict[str, Any]) -> dict[str, Any]:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    EVIDENCE_PATH.write_text(payload)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    adjudication = {
        "task": "macos_ax_structured_identity",
        "original_evidence": {
            "path": str(EVIDENCE_PATH.relative_to(REPO_ROOT)),
            "sha256": digest,
            "bytes": len(payload.encode("utf-8")),
            "status": report["status"],
            "preserved_byte_for_byte": True,
            "status_is_not_rewritten_or_superseded": True,
        },
        "evidence_classification": report.get("evidence_classification"),
        "lane": report.get("lane"),
        "candidate": report.get("environment", {}).get("candidate"),
        "capabilities_proven": report.get("capabilities_proven", []),
        "acceptance_scope": (
            "TextEdit on one Apple-Silicon macOS host with an active user "
            "session and Screen Recording + Accessibility granted. Proves the "
            "live AX identity + structural round-trip and safe misses; the "
            "refusal contract is unit-covered. Experimental: not a general "
            "per-application support claim, and no native AXPress actuation is "
            "claimed (a resolved element is acted on by the gated physical click)."
        ),
        "refusal_contract_is_unit_covered_in": "tests/test_macos_structural.py",
    }
    ADJUDICATION_PATH.write_text(json.dumps(adjudication, indent=2, sort_keys=True) + "\n")
    return {"sha256": digest, "evidence": str(EVIDENCE_PATH)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-write", action="store_true", help="print the report but do not write it"
    )
    args = parser.parse_args()
    report = qualify()
    print(json.dumps(report, indent=2, sort_keys=True))
    if not args.no_write and report["status"] != "blocked":
        written = _write_evidence(report)
        print(f"\nwrote {written['evidence']}\nsha256 {written['sha256']}")
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
