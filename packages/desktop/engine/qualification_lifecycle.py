"""Local artifact and evidence operations for the qualification cockpit."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


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
        env_names = ", ".join(secret_environment_reference(name) for name in sorted(forbidden))
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


def stage_case_runtime_inputs(
    data_dir: Path,
    *,
    workflow_id: str,
    case_id: str,
    workflow: Any,
    parameters_path: Path,
) -> tuple[Path, bytes]:
    """Write the exact private canonical inputs Flow authorizes for one case."""

    workflow_id = validate_path_token(workflow_id, label="Workflow id")
    case_id = validate_case_id(case_id)
    try:
        raw_parameters = parameters_path.read_bytes()
        parameters = json.loads(raw_parameters)
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationLifecycleError("Case parameters cannot be read safely") from exc
    if not isinstance(parameters, dict) or any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in parameters.items()
    ):
        raise QualificationLifecycleError("Case parameters must be a string-value object")
    from openadapt_flow.runtime.authorization import runtime_inputs_bytes

    inputs = runtime_inputs_bytes(workflow, parameters, {})
    root = data_dir / "qualification-inputs" / workflow_id
    path = root / f"{case_id}.runtime-inputs.json"
    temporary = root / f".{case_id}.runtime-inputs.json.tmp"
    try:
        temporary.write_bytes(inputs)
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path, inputs


def retain_run_evidence(
    bundle_dir: Path,
    *,
    case_id: str,
    run_id: str,
    run_dir: Path,
    report_bytes: bytes | None = None,
    runtime_input_bytes: bytes | None = None,
) -> list[dict[str, str]]:
    """Retain exact local qualification evidence inside its local boundary."""

    case_id = validate_case_id(case_id)
    run_id = validate_path_token(run_id, label="Run id")
    source = run_dir / "report.json"
    if report_bytes is None and (not source.is_file() or source.is_symlink()):
        return []
    if report_bytes is None:
        report_bytes = source.read_bytes()
    try:
        report = json.loads(report_bytes)
    except json.JSONDecodeError as exc:
        raise QualificationLifecycleError("Run report is not valid JSON") from exc
    if not isinstance(report, dict):
        raise QualificationLifecycleError("Run report must be a JSON object")
    if runtime_input_bytes is None:
        raise QualificationLifecycleError("Qualification evidence requires canonical inputs")
    relative = Path(case_id) / run_id / "report.json"
    destination = bundle_dir / "qualification-evidence" / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(report_bytes)
    report_digest = hashlib.sha256(report_bytes).hexdigest()
    input_relative = Path(case_id) / run_id / "runtime-inputs.json"
    input_destination = bundle_dir / "qualification-evidence" / input_relative
    input_destination.write_bytes(runtime_input_bytes)
    input_digest = hashlib.sha256(runtime_input_bytes).hexdigest()
    return [
        {
            "kind": "run_report",
            "sha256": report_digest,
            "relative_path": relative.as_posix(),
        },
        {
            "kind": "case_input",
            "sha256": input_digest,
            "relative_path": input_relative.as_posix(),
        },
    ]


def retain_capability_observation(
    bundle_dir: Path,
    *,
    case_id: str,
    run_id: str,
    observation: dict[str, Any],
) -> dict[str, str]:
    """Retain a canonical PHI-free signed capability receipt."""

    from engine.qualification_capabilities import (
        SignedQualificationCapabilityObservation,
    )

    case_id = validate_case_id(case_id)
    run_id = validate_path_token(run_id, label="Run id")
    validated = SignedQualificationCapabilityObservation.model_validate(observation)
    relative = Path(case_id) / run_id / "capability-observation.json"
    destination = bundle_dir / "qualification-evidence" / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            validated.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return {
        "kind": "other",
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "relative_path": relative.as_posix(),
    }


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


_PUSH_SCHEMA = "openadapt.push-result/v1"
_PUSH_TOP_LEVEL = {
    "schema",
    "status",
    "workflow_id",
    "artifact_ingest_id",
    "review",
    "attestation",
    "binding",
    "next_action",
    "dashboard_url",
    "delivery",
    "error",
}
_PUSH_BINDING_KEYS = {
    "kind",
    "source_tree_sha256",
    "derivative_tree_sha256",
    "approved_archive_sha256",
    "artifact_sha256",
    "bundle_sha256",
    "source_recording_sha256",
    "sanitization_policy",
    "certification_policy",
    "certification_evidence_sha256",
    "governed_authorization_template_sha256",
    "parameter_schema_sha256",
    "attested_run_report_sha256",
    "resolves_run_id",
    "organization_id",
    "bundle_version_id",
    "bundle_version",
    "runtime_validation_id",
}
_PUSH_HASH_KEYS = _PUSH_BINDING_KEYS - {
    "kind",
    "sanitization_policy",
    "certification_policy",
    "resolves_run_id",
    "organization_id",
    "bundle_version_id",
    "bundle_version",
    "runtime_validation_id",
}
_UUID_RE = re.compile(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}"
)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_uuid(value: Any) -> bool:
    return isinstance(value, str) and _UUID_RE.fullmatch(value) is not None


def _invalid_push_result() -> dict[str, Any]:
    return {
        "ok": False,
        "deployed": False,
        "pending_review": False,
        "accepted_for_ingest": False,
        "delivery_uncertain": True,
        "workflow_id": None,
        "artifact_ingest_id": None,
        "dashboard_url": None,
        "next_action": "reconcile",
        "error_code": "invalid_ingest_response",
        "error": (
            "Flow did not return a valid push result. Reconcile the exact artifact "
            "in Cloud before any retry."
        ),
    }


def _require_exact_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise QualificationLifecycleError(f"{label} did not match its closed schema")
    return value


def _origin(value: str) -> tuple[str, str, int | None]:
    """Return a safe web origin tuple for a controller trust comparison."""

    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise QualificationLifecycleError("Flow dashboard URL is unsafe")
    hostname = parsed.hostname.lower()
    if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise QualificationLifecycleError("Flow dashboard URL is unsafe")
    try:
        port = parsed.port
    except ValueError as exc:
        raise QualificationLifecycleError("Flow dashboard URL is unsafe") from exc
    if (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    ):
        port = None
    return parsed.scheme, hostname, port


def _validate_push_document(
    document: Any, *, process_ok: bool, expected_host: str | None = None
) -> dict[str, Any]:
    """Validate Flow's complete V1 result and its phase-specific invariants."""

    doc = _require_exact_object(document, _PUSH_TOP_LEVEL, "Flow push result")
    if doc["schema"] != _PUSH_SCHEMA:
        raise QualificationLifecycleError("Flow push result schema is unsupported")
    status = doc["status"]
    if status not in {
        "paused_for_review",
        "accepted_for_ingest",
        "failed",
        "delivery_uncertain",
    }:
        raise QualificationLifecycleError("Flow push result status is unsupported")
    if process_ok != (status in {"paused_for_review", "accepted_for_ingest"}):
        raise QualificationLifecycleError("Flow push process outcome conflicts with its status")

    for field in ("workflow_id", "artifact_ingest_id"):
        if doc[field] is not None and not _is_uuid(doc[field]):
            raise QualificationLifecycleError(f"Flow push {field} is invalid")

    review = doc["review"]
    if review is not None:
        review = _require_exact_object(
            review, {"id", "scope", "sanitized_path", "command"}, "Flow review"
        )
        if not _is_sha256(review["id"]) or review["scope"] != "local_non_authoritative":
            raise QualificationLifecycleError("Flow review binding is invalid")
        for field in ("sanitized_path", "command"):
            if review[field] is not None and (
                not isinstance(review[field], str) or not review[field]
            ):
                raise QualificationLifecycleError("Flow review handoff is invalid")

    attestation = doc["attestation"]
    if attestation is not None:
        attestation = _require_exact_object(
            attestation, {"id", "schema"}, "Flow attestation"
        )
        schema = attestation["schema"]
        if (
            not isinstance(attestation["id"], str)
            or not 1 <= len(attestation["id"]) <= 200
            or not isinstance(schema, str)
            or not schema.startswith("openadapt.runtime-validation/v")
            or not schema.removeprefix("openadapt.runtime-validation/v").isdigit()
        ):
            raise QualificationLifecycleError("Flow runtime attestation is invalid")

    binding = _require_exact_object(
        doc["binding"], _PUSH_BINDING_KEYS, "Flow artifact binding"
    )
    if binding["kind"] not in {"recording", "bundle", None}:
        raise QualificationLifecycleError("Flow artifact kind is invalid")
    for field in _PUSH_HASH_KEYS:
        if binding[field] is not None and not _is_sha256(binding[field]):
            raise QualificationLifecycleError(f"Flow artifact binding {field} is invalid")
    for field in ("sanitization_policy", "certification_policy"):
        if binding[field] is not None and (
            not isinstance(binding[field], str) or not binding[field]
        ):
            raise QualificationLifecycleError(f"Flow artifact binding {field} is invalid")
    if binding["resolves_run_id"] is not None and not _is_uuid(
        binding["resolves_run_id"]
    ):
        raise QualificationLifecycleError("Flow resolved run binding is invalid")
    for field in ("organization_id", "bundle_version_id", "runtime_validation_id"):
        if binding[field] is not None and not _is_uuid(binding[field]):
            raise QualificationLifecycleError(
                f"Flow artifact binding {field} is invalid"
            )
    bundle_version = binding["bundle_version"]
    if bundle_version is not None and (
        not isinstance(bundle_version, int)
        or isinstance(bundle_version, bool)
        or bundle_version < 1
    ):
        raise QualificationLifecycleError("Flow bundle version is invalid")

    if doc["next_action"] not in {
        "review_local",
        "parameterize",
        "validate_runtime",
        "open_dashboard",
        "reconcile",
        None,
    }:
        raise QualificationLifecycleError("Flow next action is invalid")
    dashboard = doc["dashboard_url"]
    if dashboard is not None:
        if not isinstance(dashboard, str):
            raise QualificationLifecycleError("Flow dashboard URL is invalid")
        dashboard_origin = _origin(dashboard)
        if expected_host is not None and dashboard_origin != _origin(expected_host):
            raise QualificationLifecycleError("Flow dashboard origin is not trusted")

    delivery = _require_exact_object(
        doc["delivery"], {"attempted", "certainty"}, "Flow delivery"
    )
    if (
        delivery["attempted"] is not True
        and delivery["attempted"] is not False
        and delivery["attempted"] is not None
    ) or delivery["certainty"] not in {
        "not_attempted",
        "not_accepted",
        "accepted",
        "unknown",
    }:
        raise QualificationLifecycleError("Flow delivery binding is invalid")
    error = doc["error"]
    if error is not None:
        error = _require_exact_object(error, {"code", "message"}, "Flow push error")
        if error["code"] not in {
            "push_failed",
            "delivery_uncertain",
            "invalid_ingest_response",
        } or not isinstance(error["message"], str) or not 1 <= len(error["message"]) <= 500:
            raise QualificationLifecycleError("Flow push error is invalid")

    if status == "paused_for_review":
        pause_only_nulls = {
            "approved_archive_sha256",
            "artifact_sha256",
            "bundle_sha256",
            "source_recording_sha256",
            "certification_policy",
            "certification_evidence_sha256",
            "governed_authorization_template_sha256",
            "parameter_schema_sha256",
            "attested_run_report_sha256",
            "resolves_run_id",
            "organization_id",
            "bundle_version_id",
            "bundle_version",
            "runtime_validation_id",
        }
        if not (
            doc["workflow_id"] is None
            and doc["artifact_ingest_id"] is None
            and isinstance(review, dict)
            and isinstance(review["sanitized_path"], str)
            and isinstance(review["command"], str)
            and attestation is None
            and doc["next_action"] == "review_local"
            and dashboard is None
            and error is None
            and delivery == {"attempted": False, "certainty": "not_attempted"}
            and binding["kind"] in {"recording", "bundle"}
            and _is_sha256(binding["source_tree_sha256"])
            and _is_sha256(binding["derivative_tree_sha256"])
            and isinstance(binding["sanitization_policy"], str)
            and all(binding[field] is None for field in pause_only_nulls)
        ):
            raise QualificationLifecycleError("Flow review pause is incomplete")
    elif status == "accepted_for_ingest":
        if not (
            _is_uuid(doc["artifact_ingest_id"])
            and isinstance(review, dict)
            and review["sanitized_path"] is None
            and review["command"] is None
            and error is None
            and delivery == {"attempted": True, "certainty": "accepted"}
            and _is_sha256(binding["source_tree_sha256"])
            and _is_sha256(binding["derivative_tree_sha256"])
            and _is_sha256(binding["approved_archive_sha256"])
            and binding["approved_archive_sha256"] == binding["artifact_sha256"]
            and isinstance(binding["sanitization_policy"], str)
        ):
            raise QualificationLifecycleError("Flow accepted ingest binding is incomplete")
        if binding["kind"] == "recording":
            recording_only_nulls = {
                "bundle_sha256",
                "source_recording_sha256",
                "certification_policy",
                "certification_evidence_sha256",
                "governed_authorization_template_sha256",
                "parameter_schema_sha256",
                "attested_run_report_sha256",
                "resolves_run_id",
                "organization_id",
                "bundle_version_id",
                "bundle_version",
                "runtime_validation_id",
            }
            if not (
                doc["workflow_id"] is None
                and attestation is None
                and doc["next_action"] in {"parameterize", "validate_runtime"}
                and dashboard is None
                and all(binding[field] is None for field in recording_only_nulls)
            ):
                raise QualificationLifecycleError("Flow recording ingest state is invalid")
        elif binding["kind"] == "bundle":
            required_bundle_hashes = (
                "bundle_sha256",
                "source_recording_sha256",
                "certification_evidence_sha256",
                "parameter_schema_sha256",
                "attested_run_report_sha256",
            )
            if not (
                _is_uuid(doc["workflow_id"])
                and isinstance(attestation, dict)
                and doc["next_action"] == "open_dashboard"
                and all(_is_sha256(binding[field]) for field in required_bundle_hashes)
                and binding["bundle_sha256"] == binding["artifact_sha256"]
                and isinstance(binding["certification_policy"], str)
                and _is_uuid(binding["organization_id"])
                and _is_uuid(binding["bundle_version_id"])
                and isinstance(binding["bundle_version"], int)
                and not isinstance(binding["bundle_version"], bool)
                and binding["bundle_version"] >= 1
                and _is_uuid(binding["runtime_validation_id"])
                and isinstance(dashboard, str)
                and urlsplit(dashboard).path == f"/dashboard/workflows/{doc['workflow_id']}"
                and not urlsplit(dashboard).query
                and not urlsplit(dashboard).fragment
            ):
                raise QualificationLifecycleError("Flow bundle ingest state is invalid")
        else:
            raise QualificationLifecycleError("Flow accepted ingest has no artifact kind")
    elif status == "delivery_uncertain":
        uncertain_server_nulls = {
            "organization_id",
            "bundle_version_id",
            "bundle_version",
            "runtime_validation_id",
        }
        if not (
            doc["workflow_id"] is None
            and doc["artifact_ingest_id"] is None
            and doc["next_action"] == "reconcile"
            and dashboard is None
            and delivery == {"attempted": True, "certainty": "unknown"}
            and isinstance(error, dict)
            and error["code"] == "delivery_uncertain"
            and all(binding[field] is None for field in uncertain_server_nulls)
        ):
            raise QualificationLifecycleError("Flow uncertain delivery state is invalid")
    else:
        if not (
            doc["workflow_id"] is None
            and doc["artifact_ingest_id"] is None
            and review is None
            and attestation is None
            and dashboard is None
            and isinstance(error, dict)
            and all(value is None for value in binding.values())
        ):
            raise QualificationLifecycleError("Flow failed state is invalid")
        if error["code"] == "push_failed":
            if not (
                doc["next_action"] is None
                and delivery == {"attempted": None, "certainty": "not_accepted"}
            ):
                raise QualificationLifecycleError("Flow rejected delivery state is invalid")
        elif error["code"] == "invalid_ingest_response":
            if not (
                doc["next_action"] == "reconcile"
                and delivery == {"attempted": True, "certainty": "unknown"}
            ):
                raise QualificationLifecycleError("Flow invalid response state is invalid")
        else:
            raise QualificationLifecycleError("Flow failed state has the wrong error")
    return doc


def parse_flow_push(
    stdout: str, stderr: str, *, ok: bool, expected_host: str | None = None
) -> dict[str, Any]:
    """Project only Flow's exact JSON V1 result into Desktop state."""

    del stderr  # Raw child diagnostics must not cross the local UI boundary.
    try:
        document = _validate_push_document(
            json.loads(stdout), process_ok=ok, expected_host=expected_host
        )
    except (json.JSONDecodeError, QualificationLifecycleError, TypeError, ValueError):
        return _invalid_push_result()
    status = document["status"]
    accepted = status == "accepted_for_ingest"
    bundle_accepted = accepted and document["binding"]["kind"] == "bundle"
    error = document["error"] or {}
    return {
        "ok": status in {"paused_for_review", "accepted_for_ingest"},
        "deployed": bundle_accepted,
        "pending_review": status == "paused_for_review",
        "accepted_for_ingest": accepted,
        "delivery_uncertain": status == "delivery_uncertain"
        or document["delivery"]["certainty"] == "unknown",
        "status": status,
        "workflow_id": document["workflow_id"],
        "artifact_ingest_id": document["artifact_ingest_id"],
        "review": document["review"],
        "attestation": document["attestation"],
        "binding": document["binding"],
        "next_action": document["next_action"],
        "dashboard_url": document["dashboard_url"],
        "delivery": document["delivery"],
        "error_code": error.get("code"),
        "error": error.get("message", ""),
        "push_result": document,
        "sanitized_path": (document["review"] or {}).get("sanitized_path") or "",
        "review_command": (document["review"] or {}).get("command") or "",
    }


def persist_deployment_handoff(
    data_dir: Path, *, local_workflow_id: str, result: dict[str, Any]
) -> Path:
    """Persist the exact typed Flow state so Desktop never loses a handoff."""

    local_workflow_id = validate_path_token(local_workflow_id, label="Workflow id")
    push_result = result.get("push_result")
    if isinstance(push_result, dict):
        state = {
            "paused_for_review": "needs_review",
            "accepted_for_ingest": (
                "deployed"
                if push_result["binding"]["kind"] == "bundle"
                else "accepted_recording"
            ),
            "failed": "failed",
            "delivery_uncertain": "delivery_uncertain",
        }[push_result["status"]]
    elif result.get("delivery_uncertain"):
        state = "delivery_uncertain"
        push_result = None
    else:
        raise QualificationLifecycleError("No valid Flow push result is available")
    root = Path(data_dir) / "deployment-handoffs"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    destination = root / f"{local_workflow_id}.json"
    document = {
        "schema": "openadapt.desktop-deployment-handoff/v1",
        "local_workflow_id": local_workflow_id,
        "state": state,
        "updated_at": datetime.now(UTC).isoformat(),
        "push_result": push_result,
        "error_code": result.get("error_code"),
    }
    fd, temporary_name = tempfile.mkstemp(
        dir=str(root), prefix=f".{local_workflow_id}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
