#!/usr/bin/env python3
"""Block committing a compiled bundle that carries plaintext PHI (audit REM-1).

A compiled bundle (``workflow.json``) is a HIPAA-designated record. This guard
refuses to let one into git while it still carries a PLAINTEXT patient
identifier, so the ``docs/showcase-openemr/bundle`` precedent (a whole bundle
committed to the repo) can never recur with a real identity band.

Two layers:

* ALWAYS (no dependencies): fail on any ``workflow.json`` that carries a
  non-empty ``anchor.context_text`` or ``anchor.structured_identity`` in any
  step, or a top-level ``contains_phi: true``. This is the flagship PHI-at-rest
  leak (GAP-1a); PHI-free bundles store a salted-hash ``identity_template``
  instead and pass.
* OPTIONAL (when openadapt-privacy is importable): additionally run the Presidio
  scrubber over each step's free text — TEXT_PRESENT postconditions, the anchor
  label (``ocr_text``), and literal TYPE text — and fail on any detected
  identifier. This catches identifier-bearing postconditions (GAP-1b) that the
  structural check cannot see. Skipped with a NOTE when the extra is absent.

Usage:
    python scripts/check_bundle_phi.py [FILE ...]

With no FILE args it scans every git-tracked ``workflow.json``. Pre-commit
passes the staged files. Exit code 0 = clean, 1 = PHI found (commit blocked).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _git_tracked_workflow_jsons() -> list[Path]:
    try:
        out = subprocess.check_output(["git", "ls-files", "*workflow.json"], text=True)
    except Exception:
        return []
    return [Path(p) for p in out.split("\n") if p.strip()]


def _iter_workflow_jsons(args: list[str]) -> list[Path]:
    if args:
        return [Path(a) for a in args if a.endswith("workflow.json")]
    return _git_tracked_workflow_jsons()


def _load_scrubber():
    """Return a text scrubber if openadapt-privacy is importable, else None."""
    try:
        from openadapt_privacy.providers.presidio import PresidioScrubbingProvider
    except Exception:
        return None
    try:
        return PresidioScrubbingProvider()
    except Exception:
        return None


def _walk_steps(workflow: dict):
    """Yield (step_id, step_dict) for a linear or program-graph bundle."""
    for step in workflow.get("steps", []) or []:
        yield step.get("id", "?"), step
    program = workflow.get("program") or {}
    for graphs in ([program], (workflow.get("subflows") or {}).values()):
        for graph in graphs:
            for state in (graph.get("states") or {}).values():
                step = state.get("step")
                if step:
                    yield step.get("id", "?"), step


def _structural_violations(path: Path, workflow: dict) -> list[str]:
    out: list[str] = []
    if workflow.get("contains_phi") is True:
        out.append(f"{path}: manifest contains_phi=true")
    for step_id, step in _walk_steps(workflow):
        anchor = step.get("anchor") or {}
        if anchor.get("context_text"):
            out.append(
                f"{path}: step {step_id} carries PLAINTEXT anchor.context_text "
                "(patient identity band). Recompile with the current PHI-free "
                "compiler (stores a salted-hash identity_template instead)."
            )
        if anchor.get("structured_identity"):
            out.append(
                f"{path}: step {step_id} carries PLAINTEXT "
                "anchor.structured_identity. Recompile PHI-free."
            )
    return out


def _presidio_violations(path: Path, workflow: dict, scrubber) -> list[str]:
    out: list[str] = []

    def flag(step_id: str, field: str, text: str) -> None:
        if not text or not str(text).strip():
            return
        scrubbed = scrubber.scrub_text(str(text))
        if scrubbed and scrubbed != str(text):
            out.append(
                f"{path}: step {step_id} {field} looks like it contains an "
                f"identifier (Presidio-detected). Value redacted from this "
                f"message."
            )

    for step_id, step in _walk_steps(workflow):
        flag(step_id, "text (TYPE literal)", step.get("text"))
        anchor = step.get("anchor") or {}
        flag(step_id, "anchor.ocr_text", anchor.get("ocr_text"))
        for lm in anchor.get("landmarks", []) or []:
            flag(step_id, "landmark.ocr_text", lm.get("ocr_text"))
        for pc in step.get("expect", []) or []:
            flag(step_id, "postcondition.text", pc.get("text"))
    return out


def main(argv: list[str]) -> int:
    paths = _iter_workflow_jsons(argv)
    if not paths:
        return 0
    scrubber = _load_scrubber()
    violations: list[str] = []
    for path in paths:
        try:
            workflow = json.loads(path.read_text())
        except Exception as exc:
            print(f"WARN: could not parse {path}: {exc}", file=sys.stderr)
            continue
        violations.extend(_structural_violations(path, workflow))
        if scrubber is not None:
            violations.extend(_presidio_violations(path, workflow, scrubber))

    if scrubber is None:
        print(
            "NOTE: openadapt-privacy not installed — checked only for plaintext "
            "identity bands (structural). Install the 'privacy' extra to also "
            "scan postconditions/labels for identifiers.",
            file=sys.stderr,
        )

    if violations:
        print("PHI GUARD: refusing to commit bundle(s) with plaintext PHI:\n")
        for v in violations:
            print(f"  - {v}")
        print(
            "\nA compiled bundle is a HIPAA record. See docs/phi_at_rest.md. "
            "Recompile with the current compiler (PHI-free by default) or remove "
            "the committed bundle.",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
