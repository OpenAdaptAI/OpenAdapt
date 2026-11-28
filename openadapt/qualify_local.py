"""Local workflow qualification supporting inline-save and autosave applications.

``openadapt qualify --local`` validates a workflow bundle or local run evidence
without requiring cloud connectivity. When ``--autosave`` or ``--inline-save``
is enabled (or declared in the bundle profile), qualification verifies that
modified fields persist on readback/reload without requiring a separate
explicit Save button click.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

LOCAL_QUALIFY_SCHEMA = "openadapt.local-qualification/v1"
VALID_SAVE_MODES = ("explicit-save", "autosave", "inline-save")


class LocalQualifyError(ValueError):
    """Raised when local qualification inputs or flags are invalid."""


@dataclass(frozen=True)
class QualificationCheck:
    """Single check outcome in a local qualification report."""

    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class LocalQualificationReport:
    """Structured result of ``openadapt qualify --local``."""

    qualified: bool
    target: str
    save_mode: str
    requires_save_button: bool
    persisted_fields_verified: tuple[str, ...] = ()
    unverified_fields: tuple[str, ...] = ()
    checks: tuple[QualificationCheck, ...] = field(default_factory=tuple)
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": LOCAL_QUALIFY_SCHEMA,
            "qualified": self.qualified,
            "target": self.target,
            "save_mode": self.save_mode,
            "requires_save_button": self.requires_save_button,
            "persisted_fields_verified": list(self.persisted_fields_verified),
            "unverified_fields": list(self.unverified_fields),
            "checks": [c.to_dict() for c in self.checks],
            "generated_at": self.generated_at,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_target_doc(target: Path) -> dict[str, Any]:
    if not target.exists():
        raise LocalQualifyError(f"Qualification target does not exist: {target}")
    if target.is_file():
        try:
            doc = json.loads(target.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise LocalQualifyError(f"Target {target} is not valid JSON: {exc}") from exc
        if not isinstance(doc, dict):
            raise LocalQualifyError(f"Target {target} must contain a JSON object.")
        return doc

    for candidate in ("bundle.json", "workflow.json", "qualify.json", "program.json"):
        path = target / candidate
        if path.is_file():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise LocalQualifyError(f"Malformed JSON in {path}: {exc}") from exc
            if isinstance(doc, dict):
                return doc
    raise LocalQualifyError(
        f"No bundle or qualification profile JSON found in {target}."
    )


def _has_explicit_save_step(doc: dict[str, Any]) -> bool:
    if doc.get("explicit_save") is True or doc.get("has_save_button") is True:
        return True
    steps = doc.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            kind = str(step.get("kind") or step.get("action") or "").lower()
            role = str(step.get("role") or "").lower()
            if kind in ("save", "commit", "submit") or role in ("save", "commit", "submit"):
                return True
            if step.get("is_commit") is True or step.get("is_save") is True:
                return True
    return False


def _extract_declared_fields(doc: dict[str, Any]) -> list[str]:
    for key in ("fields", "expected_fields", "persisted_fields", "checked_fields"):
        val = doc.get(key)
        if isinstance(val, list):
            out: list[str] = []
            for item in val:
                if isinstance(item, str) and item.strip():
                    out.append(item.strip())
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("field")
                    if isinstance(name, str) and name.strip():
                        out.append(name.strip())
            if out:
                return out
    return []


def _extract_readback_results(
    doc: dict[str, Any], runs_dir: Optional[Path]
) -> dict[str, bool]:
    """Collect field persistence readback outcomes from the target doc or runs_dir."""
    outcomes: dict[str, bool] = {}

    def _consume(container: dict[str, Any]) -> None:
        readback = container.get("readback") or container.get("persisted_state")
        if isinstance(readback, dict):
            fields_map = readback.get("fields") if isinstance(readback.get("fields"), dict) else readback
            for fname, fval in fields_map.items():
                if fname in ("reload_verified", "method", "channel", "timestamp"):
                    continue
                if isinstance(fval, bool):
                    outcomes[str(fname)] = fval
                elif isinstance(fval, dict) and "persisted" in fval:
                    outcomes[str(fname)] = bool(fval["persisted"])
                elif isinstance(fval, dict) and "matched" in fval:
                    outcomes[str(fname)] = bool(fval["matched"])
        field_checks = container.get("field_checks")
        if isinstance(field_checks, list):
            for entry in field_checks:
                if isinstance(entry, dict):
                    name = entry.get("name") or entry.get("field")
                    if isinstance(name, str) and name.strip():
                        passed = entry.get("persisted")
                        if passed is None:
                            passed = entry.get("matched")
                        if passed is None:
                            passed = entry.get("ok")
                        if passed is not None:
                            outcomes[name.strip()] = bool(passed)

    _consume(doc)
    if runs_dir is not None and runs_dir.exists():
        files = [runs_dir] if runs_dir.is_file() else sorted(runs_dir.rglob("*.json"))
        for path in files:
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(parsed, dict):
                _consume(parsed)
    return outcomes


def _resolve_save_mode(
    doc: dict[str, Any],
    *,
    autosave: bool = False,
    inline_save: bool = False,
    save_mode: Optional[str] = None,
) -> str:
    if autosave and inline_save:
        return "inline-save"
    if inline_save:
        return "inline-save"
    if autosave:
        return "autosave"
    if save_mode is not None:
        if save_mode not in VALID_SAVE_MODES:
            raise LocalQualifyError(
                f"Invalid save_mode {save_mode!r}; expected one of {VALID_SAVE_MODES}."
            )
        return save_mode
    doc_mode = doc.get("save_mode")
    if isinstance(doc_mode, str) and doc_mode in VALID_SAVE_MODES:
        return doc_mode
    if doc.get("inline_save") is True:
        return "inline-save"
    if doc.get("autosave") is True:
        return "autosave"
    return "explicit-save"


def qualify_local(
    target: Path,
    *,
    autosave: bool = False,
    inline_save: bool = False,
    save_mode: Optional[str] = None,
    runs_dir: Optional[Path] = None,
) -> LocalQualificationReport:
    """Run local qualification against a workflow bundle or profile JSON."""
    doc = _load_target_doc(target)
    resolved_mode = _resolve_save_mode(
        doc, autosave=autosave, inline_save=inline_save, save_mode=save_mode
    )
    requires_save_button = resolved_mode == "explicit-save"

    checks: list[QualificationCheck] = []
    has_save = _has_explicit_save_step(doc)
    if requires_save_button:
        checks.append(
            QualificationCheck(
                name="explicit_save_step",
                passed=has_save,
                detail=(
                    "Explicit save/commit step found."
                    if has_save
                    else "No explicit save/commit step found; pass --autosave or --inline-save for apps that persist on change/blur."
                ),
            )
        )
    else:
        checks.append(
            QualificationCheck(
                name="save_mode_contract",
                passed=True,
                detail=(
                    f"Mode '{resolved_mode}' enabled: explicit Save button click is not required; "
                    "persistence is verified by readback."
                ),
            )
        )

    declared_fields = _extract_declared_fields(doc)
    readback = _extract_readback_results(doc, runs_dir)

    verified: list[str] = []
    unverified: list[str] = []

    if declared_fields:
        for fname in declared_fields:
            if readback.get(fname) is True:
                verified.append(fname)
            else:
                unverified.append(fname)
    else:
        for fname, ok in sorted(readback.items()):
            if ok:
                verified.append(fname)
            else:
                unverified.append(fname)

    has_readback_evidence = bool(verified) and not unverified
    if not declared_fields and not readback:
        checks.append(
            QualificationCheck(
                name="persisted_state_readback",
                passed=False,
                detail="No persisted-state readback evidence found to validate.",
            )
        )
    elif unverified:
        checks.append(
            QualificationCheck(
                name="persisted_state_readback",
                passed=False,
                detail=f"Unverified persisted fields: {', '.join(unverified)}.",
            )
        )
    else:
        checks.append(
            QualificationCheck(
                name="persisted_state_readback",
                passed=has_readback_evidence,
                detail=f"Verified persisted readback for {len(verified)} field(s).",
            )
        )

    qualified = all(c.passed for c in checks)
    return LocalQualificationReport(
        qualified=qualified,
        target=str(target),
        save_mode=resolved_mode,
        requires_save_button=requires_save_button,
        persisted_fields_verified=tuple(verified),
        unverified_fields=tuple(unverified),
        checks=tuple(checks),
        generated_at=_utc_now_iso(),
    )


def has_local_flag(argv: Sequence[str]) -> bool:
    """Return True if ``--local`` appears before any literal ``--`` separator."""
    for token in argv:
        if token == "--":
            return False
        if token == "--local":
            return True
    return False


def build_local_qualify_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openadapt qualify --local",
        description="Locally qualify a workflow bundle or persisted-state profile.",
    )
    parser.add_argument(
        "target",
        type=Path,
        help="Path to workflow bundle directory or qualification profile JSON.",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        default=True,
        help="Run local offline qualification.",
    )
    parser.add_argument(
        "--autosave",
        action="store_true",
        default=False,
        help="Validate persisted readback for autosave apps without requiring an explicit Save click.",
    )
    parser.add_argument(
        "--inline-save",
        action="store_true",
        default=False,
        help="Validate persisted readback for inline-save apps without requiring an explicit Save click.",
    )
    parser.add_argument(
        "--save-mode",
        choices=VALID_SAVE_MODES,
        default=None,
        help="Explicit save mode override.",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Optional directory or JSON file containing readback evidence.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the local qualification JSON report to this path.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=False,
        help="Emit the qualification report as JSON.",
    )
    return parser


def run_local_qualify_cli(argv: Sequence[str]) -> int:
    """Execute ``openadapt qualify --local ...`` and return a process exit code."""
    parser = build_local_qualify_parser()
    try:
        ns = parser.parse_args(list(argv))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1

    try:
        report = qualify_local(
            ns.target,
            autosave=ns.autosave,
            inline_save=ns.inline_save,
            save_mode=ns.save_mode,
            runs_dir=ns.runs_dir,
        )
    except LocalQualifyError as exc:
        print(f"Error: {exc}")
        return 2

    payload = report.to_dict()
    if ns.out is not None:
        ns.out.parent.mkdir(parents=True, exist_ok=True)
        ns.out.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    if ns.as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        status = "QUALIFIED" if report.qualified else "NOT QUALIFIED"
        print(
            f"Local qualification: {status} | mode={report.save_mode} | "
            f"verified_fields={len(report.persisted_fields_verified)}"
        )
        for check in report.checks:
            mark = "PASS" if check.passed else "FAIL"
            print(f"  [{mark}] {check.name}: {check.detail}")

    return 0 if report.qualified else 1
