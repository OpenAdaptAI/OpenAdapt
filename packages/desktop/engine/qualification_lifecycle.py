"""Local artifact and evidence operations for the qualification cockpit."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path
from typing import Any


class QualificationLifecycleError(RuntimeError):
    """A local qualification lifecycle operation was refused."""


def secret_environment_reference(name: str) -> str:
    """Mirror Flow's documented secret-reference normalization."""

    key = "".join(char if char.isalnum() else "_" for char in name).upper()
    return f"OPENADAPT_FLOW_SECRET_{key}"


def validate_path_token(value: str, *, label: str) -> str:
    """Keep one user-derived identifier inside a single local path segment."""

    if not value or any(not (char.isalnum() or char in "-_.") for char in value):
        raise QualificationLifecycleError(
            f"{label} may contain only letters, numbers, dash, underscore, and dot."
        )
    if value in {".", ".."}:
        raise QualificationLifecycleError(f"{label} must be a stable identifier")
    return value


def validate_case_id(case_id: str) -> str:
    return validate_path_token(case_id, label="Case id")


def store_case_parameters(
    data_dir: Path,
    *,
    workflow_id: str,
    case_id: str,
    parameters_json: str,
    forbidden_keys: set[str] | None = None,
) -> tuple[Path, str]:
    """Validate a parameter object and keep it outside the workflow artifact."""

    workflow_id = validate_path_token(workflow_id, label="Workflow id")
    case_id = validate_case_id(case_id)
    if len(parameters_json.encode("utf-8")) > 1_000_000:
        raise QualificationLifecycleError("Case parameters exceed 1 MB")
    try:
        payload = json.loads(parameters_json)
    except json.JSONDecodeError as exc:
        raise QualificationLifecycleError("Case parameters are not valid JSON") from exc
    if not isinstance(payload, dict):
        raise QualificationLifecycleError("Case parameters must be a JSON object")
    forbidden = set(payload) & (forbidden_keys or set())
    if forbidden:
        env_names = ", ".join(
            secret_environment_reference(name) for name in sorted(forbidden)
        )
        raise QualificationLifecycleError(
            "Secret values cannot be stored in qualification cases. "
            f"Configure these runner secret references instead: {env_names}"
        )

    root = data_dir / "qualification-inputs" / workflow_id
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    path = root / f"{case_id}.json"
    temporary = root / f".{case_id}.json.tmp"
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    try:
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(temporary, path)
    return path, f"desktop-input://{workflow_id}/{case_id}"


def case_parameters_path(data_dir: Path, *, workflow_id: str, case_id: str) -> Path | None:
    """Resolve a previously stored case fixture without accepting an arbitrary path."""

    workflow_id = validate_path_token(workflow_id, label="Workflow id")
    case_id = validate_case_id(case_id)
    root = (data_dir / "qualification-inputs" / workflow_id).resolve()
    path = (root / f"{case_id}.json").resolve()
    if not path.is_relative_to(root) or path.is_symlink():
        raise QualificationLifecycleError("Case parameters leave the local fixture store")
    return path if path.is_file() else None


def retain_run_evidence(
    bundle_dir: Path,
    *,
    case_id: str,
    run_id: str,
    run_dir: Path,
) -> list[dict[str, str]]:
    """Retain a privacy-safe receipt bound to the exact local run report."""

    case_id = validate_case_id(case_id)
    run_id = validate_path_token(run_id, label="Run id")
    source = run_dir / "report.json"
    if not source.is_file() or source.is_symlink():
        return []
    report_bytes = source.read_bytes()
    try:
        report = json.loads(report_bytes)
    except json.JSONDecodeError as exc:
        raise QualificationLifecycleError("Run report is not valid JSON") from exc
    envelope = report.get("outcome_envelope") or {}
    receipt = {
        "schema": "openadapt.qualification-run-receipt/v1",
        "run_id": run_id,
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "execution_outcome": report.get("execution_outcome"),
        "production_eligible": bool(report.get("production_eligible", False)),
        "execution_completed": bool(report.get("execution_completed", False)),
        "model_calls": report.get("model_calls"),
        "contracts": {
            "required": envelope.get("required_contracts", {}),
            "passed": envelope.get("passed_contracts", {}),
            "evidence_classes": envelope.get("evidence_classes", []),
            "external_network_calls": envelope.get("external_network_calls"),
        },
    }
    relative = Path(case_id) / run_id / "run-report-receipt.json"
    destination = bundle_dir / "qualification-evidence" / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return [
        {
            "kind": "run_report",
            "sha256": digest,
            "relative_path": relative.as_posix(),
        }
    ]


def copy_bundle_version(source: Path, destination: Path) -> None:
    """Publish an exact local working version without following symlinks."""

    if destination.exists():
        raise QualificationLifecycleError("The destination workflow version already exists")
    for path in [source, *source.rglob("*")]:
        if path.is_symlink():
            raise QualificationLifecycleError("Workflow versions cannot contain symbolic links")
    staging = destination.parent / f".{destination.name}.copying"
    if staging.exists():
        raise QualificationLifecycleError("A prior version operation needs attention")
    try:
        shutil.copytree(source, staging)
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def export_certified_bundle(bundle_dir: Path, destination: Path) -> str:
    """Write a deterministic archive of one exact sealed artifact."""

    if destination.exists():
        raise QualificationLifecycleError("The export destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_symlink():
                raise QualificationLifecycleError("Workflow exports cannot contain symbolic links")
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(path.relative_to(bundle_dir).as_posix())
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, path.read_bytes())
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def parse_flow_push(stdout: str, stderr: str, *, ok: bool) -> dict[str, Any]:
    """Project Flow's bounded push states without treating review as deployment."""

    if not ok:
        return {"ok": False, "deployed": False, "error": stderr or "Cloud deploy failed"}
    if "Upload paused for local review" in stdout:
        sanitized_path = ""
        marker = "Sanitized derivative created at "
        for line in stdout.splitlines():
            if line.startswith(marker):
                sanitized_path = line[len(marker) :].rstrip(".")
                break
        return {
            "ok": True,
            "deployed": False,
            "pending_review": True,
            "sanitized_path": sanitized_path,
        }
    workflow_id = ""
    dashboard_url = ""
    for line in stdout.splitlines():
        if "workflow_id=" in line:
            workflow_id = line.split("workflow_id=", 1)[1].split()[0].rstrip(",).")
        if line.startswith("Dashboard: "):
            dashboard_url = line.removeprefix("Dashboard: ").strip()
    return {
        "ok": bool(workflow_id),
        "deployed": bool(workflow_id),
        "workflow_id": workflow_id,
        "dashboard_url": dashboard_url,
        "error": "" if workflow_id else "Cloud did not return a deployed workflow id",
    }
