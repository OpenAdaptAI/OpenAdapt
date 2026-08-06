#!/usr/bin/env python3
"""Run a counted, customer-local, real Citrix ICA/HDX acceptance campaign.

The public harness defines the contract. Customer-specific commands, approval
artifacts, identifiers, recipes, and evidence stay inside the private customer
boundary. The harness can actuate only through one approved runner executable
bound to a pre-existing Citrix session. It has no infrastructure lifecycle
operation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import secrets
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SCHEMA = "openadapt.citrix-real-acceptance.v3"
REPORT_SCHEMA = "openadapt.citrix-real-acceptance-report.v3"
TRUST_ROOT_SCHEMA = "openadapt.citrix-acceptance-trust-roots.v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
NONCE_RE = re.compile(r"[0-9a-f]{32,128}")
MAX_COLLECTOR_AGE_S = 300
EXPECTED_OUTCOMES = {
    "healthy": "VERIFIED",
    "wrong_session_or_entity": "HALTED",
    "ambiguity": "HALTED",
    "stale_state": "HALTED",
    "display_drift": "HALTED",
    "partial_effect": "HALTED",
    "reconnect": "HALTED",
    "commit_timeout": "HALTED_UNCERTAIN",
}
REQUIRED_FINGERPRINT_FIELDS = {
    "citrix_workspace": {"product", "version", "binary_sha256"},
    "ica_hdx": {"protocol", "vda_version", "policy_sha256", "transport_sha256"},
    "application": {"name", "version", "binary_sha256"},
    "session": {"session_id_sha256", "published_resource_sha256", "vda_sha256"},
    "display": {
        "width_px",
        "height_px",
        "dpi_x",
        "dpi_y",
        "scale_x",
        "scale_y",
        "monitor_topology_sha256",
        "window_mode",
    },
    "runner": {
        "name",
        "version",
        "binary_sha256",
        "principal_sha256",
        "host_sha256",
    },
    "bundle": {"workflow_version", "bundle_sha256", "source_commit"},
    "verifier": {"name", "version", "binary_sha256", "principal_sha256"},
    "collector": {"name", "version", "binary_sha256", "principal_sha256"},
    "environment": {"environment_sha256", "os_build", "network_zone_sha256"},
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_secure_regular_file(
    path: Path,
    label: str,
    *,
    executable: bool = False,
) -> None:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be an existing absolute regular file")
    if path.stat().st_mode & stat.S_IWOTH:
        raise ValueError(f"{label} must not be world-writable")
    if executable and not os.access(path, os.X_OK):
        raise ValueError(f"{label} must be executable")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _object_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _decode_public_key(value: Any, label: str) -> Ed25519PublicKey:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a base64 Ed25519 public key")
    try:
        raw = base64.b64decode(value, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a base64 Ed25519 public key") from exc


def _verify_signed_envelope(
    envelope: Any,
    *,
    public_key: Ed25519PublicKey,
    label: str,
) -> dict:
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        raise ValueError(f"{label} must be a signed payload envelope")
    if not isinstance(envelope["payload"], dict):
        raise ValueError(f"{label} payload must be an object")
    try:
        signature = base64.b64decode(envelope["signature"], validate=True)
        public_key.verify(signature, _canonical_json(envelope["payload"]))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise ValueError(f"{label} signature is invalid") from exc
    return envelope["payload"]


def load_trust_roots(path: Path) -> dict:
    _require_secure_regular_file(path, "trust roots")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "keys",
        "components",
    }:
        raise ValueError("trust roots have an incomplete or unknown field set")
    if data["schema_version"] != TRUST_ROOT_SCHEMA:
        raise ValueError("unsupported trust-root schema")
    required_keys = {
        "customer_authority",
        "upgrade_authority",
        "collector_authority",
        "oracle_authority",
    }
    if not isinstance(data["keys"], dict) or set(data["keys"]) != required_keys:
        raise ValueError("trust roots must contain the exact authority key set")
    if len(set(data["keys"].values())) != len(required_keys):
        raise ValueError("all authority public keys must be distinct")
    decoded_keys = {
        name: _decode_public_key(value, f"trust roots {name}")
        for name, value in data["keys"].items()
    }
    required_components = {"runner", "oracle", "collector"}
    if (
        not isinstance(data["components"], dict)
        or set(data["components"]) != required_components
    ):
        raise ValueError("trust roots must contain runner, oracle, and collector")
    for name, component in data["components"].items():
        if not isinstance(component, dict) or set(component) != {
            "executable_sha256",
            "principal_sha256",
        }:
            raise ValueError(f"trusted component {name} has an invalid field set")
        _require_sha256(
            component["executable_sha256"],
            f"trusted component {name} executable_sha256",
        )
        _require_sha256(
            component["principal_sha256"],
            f"trusted component {name} principal_sha256",
        )
    return {**data, "decoded_keys": decoded_keys, "path_sha256": _sha256(path)}


def _require_sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _require_nonempty(value: Any, label: str) -> None:
    if isinstance(value, str) and value.strip():
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return
    raise ValueError(f"{label} must be a non-empty structured fingerprint value")


def _validate_fingerprints(fingerprints: Any) -> None:
    if not isinstance(fingerprints, dict):
        raise ValueError("fingerprints must be an object")
    if set(fingerprints) != set(REQUIRED_FINGERPRINT_FIELDS):
        raise ValueError("fingerprints must contain the exact required component set")
    for component, fields in REQUIRED_FINGERPRINT_FIELDS.items():
        value = fingerprints[component]
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError(f"fingerprint {component} must contain {sorted(fields)}")
        for field, item in value.items():
            _require_nonempty(item, f"fingerprints.{component}.{field}")
            if field.endswith("sha256"):
                _require_sha256(item, f"fingerprints.{component}.{field}")
    if fingerprints["ica_hdx"]["protocol"] != "ICA/HDX":
        raise ValueError(
            "ICA/HDX protocol fingerprint must name the real ICA/HDX protocol"
        )
    source_commit = fingerprints["bundle"]["source_commit"]
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ValueError("bundle source_commit must be a full lowercase Git commit")


def _validate_file_binding(binding: Any, label: str) -> Path:
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise ValueError(f"{label} must contain only path and sha256")
    path = Path(binding["path"])
    _require_secure_regular_file(path, f"{label}.path")
    _require_sha256(binding["sha256"], f"{label}.sha256")
    if _sha256(path) != binding["sha256"]:
        raise ValueError(f"{label} digest does not match its retained file")
    return path


def _load_signed_artifact(
    binding: Any,
    *,
    public_key: Ed25519PublicKey,
    label: str,
) -> dict:
    path = _validate_file_binding(binding, label)
    try:
        envelope = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must contain one signed JSON envelope") from exc
    return _verify_signed_envelope(envelope, public_key=public_key, label=label)


def _validate_component_authority(
    *,
    name: str,
    authority: dict,
    fingerprints: dict,
    trust_roots: dict,
    customer_payload: dict | None,
) -> None:
    trusted = trust_roots["components"][name]
    executable = Path(authority["command"][0])
    observed_executable_sha256 = _sha256(executable)
    if observed_executable_sha256 != trusted["executable_sha256"]:
        raise ValueError(f"observed {name} executable is not trusted")
    if authority["executable_sha256"] != observed_executable_sha256:
        raise ValueError(f"configured {name} executable does not match observation")
    fingerprint_name = "verifier" if name == "oracle" else name
    fingerprint = fingerprints[fingerprint_name]
    if fingerprint["binary_sha256"] != observed_executable_sha256:
        raise ValueError(f"{name} fingerprint does not match observed executable")
    if authority["principal_sha256"] != trusted["principal_sha256"]:
        raise ValueError(f"configured {name} principal is not trusted")
    if fingerprint["principal_sha256"] != trusted["principal_sha256"]:
        raise ValueError(f"{name} fingerprint does not match the trusted principal")
    upgrade_payload = _load_signed_artifact(
        authority["upgrade_artifact"],
        public_key=trust_roots["decoded_keys"]["upgrade_authority"],
        label=f"{name} upgrade artifact",
    )
    expected_upgrade = {
        "schema_version": "openadapt.component-upgrade-attestation.v1",
        "component": name,
        "executable_sha256": observed_executable_sha256,
        "principal_sha256": trusted["principal_sha256"],
    }
    if upgrade_payload != expected_upgrade:
        raise ValueError(f"{name} upgrade attestation is not exact")
    if customer_payload is not None:
        approval_payload = _load_signed_artifact(
            authority["approval_artifact"],
            public_key=trust_roots["decoded_keys"]["customer_authority"],
            label=f"{name} customer approval",
        )
        if approval_payload != customer_payload:
            raise ValueError(f"{name} customer approval is not exact")


def _validate_authorities(data: dict, trust_roots: dict) -> None:
    boundary = data.get("runner_authority")
    if not isinstance(boundary, dict):
        raise ValueError("runner_authority must be an object")
    required = {
        "mode",
        "pre_existing_session",
        "infrastructure_lifecycle_authority",
        "command",
        "executable_sha256",
        "principal_sha256",
        "approval_artifact",
        "upgrade_artifact",
    }
    if set(boundary) != required:
        raise ValueError("runner_authority has an incomplete or unknown field set")
    if boundary["mode"] != "customer_approved_session_runner":
        raise ValueError(
            "runner must use the customer-approved session-runner boundary"
        )
    if boundary["pre_existing_session"] is not True:
        raise ValueError("the Citrix session must exist before this harness starts")
    if boundary["infrastructure_lifecycle_authority"] is not False:
        raise ValueError("the runner must have no infrastructure lifecycle authority")
    command = boundary["command"]
    if not isinstance(command, list) or len(command) != 1:
        raise ValueError(
            "runner command must be one approved executable without arguments"
        )
    executable = Path(command[0])
    _require_secure_regular_file(executable, "runner executable", executable=True)
    _require_sha256(boundary["executable_sha256"], "runner executable_sha256")
    _require_sha256(boundary["principal_sha256"], "runner principal_sha256")
    expected_runner_approval = {
        "schema_version": "openadapt.customer-runner-approval.v1",
        "authority": "customer_approved",
        "principal_sha256": boundary["principal_sha256"],
        "executable_sha256": boundary["executable_sha256"],
        "session_id_sha256": data["fingerprints"]["session"]["session_id_sha256"],
        "campaign_nonce": data["campaign_nonce"],
        "allowed_operation": "citrix_acceptance_trial",
        "infrastructure_lifecycle_authority": False,
    }
    _validate_component_authority(
        name="runner",
        authority=boundary,
        fingerprints=data["fingerprints"],
        trust_roots=trust_roots,
        customer_payload=expected_runner_approval,
    )

    oracle = data.get("independent_oracle")
    if not isinstance(oracle, dict):
        raise ValueError("independent_oracle must be an object")
    oracle_required = {
        "command",
        "executable_sha256",
        "authority",
        "principal_sha256",
        "approval_artifact",
        "upgrade_artifact",
    }
    if set(oracle) != oracle_required:
        raise ValueError("independent_oracle has an incomplete or unknown field set")
    if oracle["authority"] != "authenticated_read_only":
        raise ValueError("the oracle authority must be authenticated and read-only")
    if oracle["principal_sha256"] == boundary["principal_sha256"]:
        raise ValueError("the oracle and runner must use separate principals")
    oracle_command = oracle["command"]
    if not isinstance(oracle_command, list) or len(oracle_command) != 1:
        raise ValueError(
            "oracle command must be one approved executable without arguments"
        )
    oracle_executable = Path(oracle_command[0])
    _require_secure_regular_file(
        oracle_executable, "oracle executable", executable=True
    )
    _require_sha256(oracle["executable_sha256"], "oracle executable_sha256")
    _require_sha256(oracle["principal_sha256"], "oracle principal_sha256")
    expected_oracle_approval = {
        "schema_version": "openadapt.customer-oracle-approval.v1",
        "authority": "customer_approved",
        "principal_sha256": oracle["principal_sha256"],
        "executable_sha256": oracle["executable_sha256"],
        "environment_sha256": data["fingerprints"]["environment"]["environment_sha256"],
        "allowed_operation": "read_only_effect_observation",
        "separately_authenticated": True,
        "write_authority": False,
    }
    _validate_component_authority(
        name="oracle",
        authority=oracle,
        fingerprints=data["fingerprints"],
        trust_roots=trust_roots,
        customer_payload=expected_oracle_approval,
    )

    collector = data.get("collector_authority")
    if not isinstance(collector, dict):
        raise ValueError("collector_authority must be an object")
    collector_required = {
        "command",
        "executable_sha256",
        "principal_sha256",
        "upgrade_artifact",
    }
    if set(collector) != collector_required:
        raise ValueError("collector_authority has an incomplete or unknown field set")
    collector_command = collector["command"]
    if not isinstance(collector_command, list) or len(collector_command) != 1:
        raise ValueError("collector command must be one approved executable")
    collector_executable = Path(collector_command[0])
    _require_secure_regular_file(
        collector_executable, "collector executable", executable=True
    )
    _require_sha256(collector["executable_sha256"], "collector executable_sha256")
    _require_sha256(collector["principal_sha256"], "collector principal_sha256")
    if (
        len(
            {
                boundary["principal_sha256"],
                oracle["principal_sha256"],
                collector["principal_sha256"],
            }
        )
        != 3
    ):
        raise ValueError("runner, oracle, and collector principals must be separate")
    _validate_component_authority(
        name="collector",
        authority=collector,
        fingerprints=data["fingerprints"],
        trust_roots=trust_roots,
        customer_payload=None,
    )


def _validate_trials(trials: Any) -> None:
    if not isinstance(trials, list):
        raise ValueError("trials must be a list")
    counts = {condition: 0 for condition in EXPECTED_OUTCOMES}
    seen: set[str] = set()
    required = {
        "id",
        "condition",
        "expected",
        "entity_sha256",
        "effect_contract_sha256",
    }
    for trial in trials:
        if not isinstance(trial, dict) or set(trial) != required:
            raise ValueError("each trial must contain the exact trial binding fields")
        trial_id = trial["id"]
        if not isinstance(trial_id, str) or not trial_id or trial_id in seen:
            raise ValueError("trial ids must be non-empty and unique")
        seen.add(trial_id)
        condition = trial["condition"]
        if condition not in EXPECTED_OUTCOMES:
            raise ValueError(f"unknown condition {condition!r}")
        expected = EXPECTED_OUTCOMES[condition]
        if trial["expected"] != expected:
            raise ValueError(
                f"condition {condition} has fixed expected outcome {expected}"
            )
        _require_sha256(trial["entity_sha256"], f"trial {trial_id} entity_sha256")
        _require_sha256(
            trial["effect_contract_sha256"],
            f"trial {trial_id} effect_contract_sha256",
        )
        counts[condition] += 1
    short = [name for name, count in counts.items() if count < 3]
    if short:
        raise ValueError(
            f"at least three trials are required for every condition: {short}"
        )


def load_config(path: Path, trust_roots: dict) -> dict:
    data = json.loads(path.read_text())
    if data.get("schema_version") != SCHEMA:
        raise ValueError("unsupported acceptance configuration schema")
    allowed = {
        "schema_version",
        "campaign_nonce",
        "fingerprints",
        "runner_authority",
        "independent_oracle",
        "collector_authority",
        "trials",
    }
    if set(data) != allowed:
        raise ValueError(
            "configuration has an incomplete or unknown top-level field set"
        )
    if (
        not isinstance(data["campaign_nonce"], str)
        or NONCE_RE.fullmatch(data["campaign_nonce"]) is None
    ):
        raise ValueError("campaign_nonce must be 16-64 random bytes in lowercase hex")
    _validate_fingerprints(data["fingerprints"])
    _validate_authorities(data, trust_roots)
    _validate_trials(data["trials"])
    return data


def _run(command: list[str], args: list[str], *, timeout_s: int = 300) -> dict:
    result = subprocess.run(
        [*command, *args],
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr[-4000:],
    }


def _run_pinned(
    command: list[str],
    expected_sha256: str,
    args: list[str],
    *,
    timeout_s: int = 300,
) -> dict:
    executable = Path(command[0])
    _require_secure_regular_file(
        executable,
        "approved executable",
        executable=True,
    )
    if _sha256(executable) != expected_sha256:
        raise ValueError("approved executable changed after preflight")
    return _run(command, args, timeout_s=timeout_s)


def _load_command_json(result: dict, label: str) -> dict:
    if result["returncode"] != 0:
        raise ValueError(f"{label} command failed")
    try:
        value = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must return one JSON object")
    return value


def _validate_oracle_evidence(
    result: dict,
    *,
    trial: dict,
    phase: str,
    oracle_principal_sha256: str,
    oracle_public_key: Ed25519PublicKey,
    campaign_nonce: str,
    config_sha256: str,
    execution_challenge: str,
    observation_challenge: str,
    consumed_challenges: set[str],
    now: datetime | None = None,
) -> dict:
    envelope = _load_command_json(result, f"oracle {phase}")
    evidence = _verify_signed_envelope(
        envelope,
        public_key=oracle_public_key,
        label=f"oracle {phase} evidence",
    )
    required = {
        "schema_version",
        "campaign_nonce",
        "config_sha256",
        "execution_challenge",
        "observation_challenge",
        "observed_at",
        "phase",
        "trial_id",
        "entity_sha256",
        "effect_contract_sha256",
        "principal_sha256",
        "authority",
        "effect_status",
        "state_digest",
        "evidence",
    }
    if set(evidence) != required:
        raise ValueError(f"oracle {phase} evidence has incomplete or unknown fields")
    expected = {
        "schema_version": "openadapt.citrix-oracle-observation.v1",
        "campaign_nonce": campaign_nonce,
        "config_sha256": config_sha256,
        "execution_challenge": execution_challenge,
        "observation_challenge": observation_challenge,
        "phase": phase,
        "trial_id": trial["id"],
        "entity_sha256": trial["entity_sha256"],
        "effect_contract_sha256": trial["effect_contract_sha256"],
        "principal_sha256": oracle_principal_sha256,
        "authority": "authenticated_read_only",
    }
    for key, value in expected.items():
        if evidence[key] != value:
            raise ValueError(f"oracle {phase} evidence is not bound to {key}")
    if evidence["effect_status"] not in {"CONFIRMED", "REFUTED", "INDETERMINATE"}:
        raise ValueError(f"oracle {phase} effect_status is invalid")
    _require_sha256(evidence["state_digest"], f"oracle {phase} state_digest")
    _validate_file_binding(evidence["evidence"], f"oracle {phase} evidence")
    _consume_fresh_challenge(
        observation_challenge,
        evidence["observed_at"],
        consumed_challenges=consumed_challenges,
        label=f"oracle {phase}",
        now=now,
    )
    return envelope


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _consume_fresh_challenge(
    challenge: str,
    observed_at: Any,
    *,
    consumed_challenges: set[str],
    label: str,
    now: datetime | None = None,
) -> None:
    if SHA256_RE.fullmatch(challenge) is None:
        raise ValueError(f"{label} challenge must be 32 random bytes in lowercase hex")
    if challenge in consumed_challenges:
        raise ValueError(f"{label} challenge was already consumed")
    captured = _parse_timestamp(observed_at, f"{label} observed_at")
    reference = now or datetime.now(timezone.utc)
    if abs((reference - captured).total_seconds()) > MAX_COLLECTOR_AGE_S:
        raise ValueError(f"{label} evidence is stale or from the future")
    consumed_challenges.add(challenge)


def _validate_collector_evidence(
    result: dict,
    *,
    trial: dict,
    config: dict,
    config_sha256: str,
    trust_roots: dict,
    execution_challenge: str,
    observation_challenge: str,
    consumed_challenges: set[str],
    now: datetime | None = None,
) -> dict:
    envelope = _load_command_json(result, "independent ICA/HDX collector")
    proof = _verify_signed_envelope(
        envelope,
        public_key=trust_roots["decoded_keys"]["collector_authority"],
        label="independent ICA/HDX collector evidence",
    )
    required = {
        "schema_version",
        "campaign_nonce",
        "config_sha256",
        "execution_challenge",
        "observation_challenge",
        "trial_id",
        "protocol",
        "standin",
        "session_id_sha256",
        "transport_sha256",
        "observed_at",
        "observed_components",
        "diagnostic_evidence",
    }
    if not isinstance(proof, dict) or set(proof) != required:
        raise ValueError("collector evidence has incomplete or unknown fields")
    expected = {
        "schema_version": "openadapt.citrix-independent-collector.v1",
        "campaign_nonce": config["campaign_nonce"],
        "config_sha256": config_sha256,
        "execution_challenge": execution_challenge,
        "observation_challenge": observation_challenge,
        "trial_id": trial["id"],
        "protocol": "ICA/HDX",
        "standin": False,
        "session_id_sha256": config["fingerprints"]["session"]["session_id_sha256"],
        "transport_sha256": config["fingerprints"]["ica_hdx"]["transport_sha256"],
    }
    for key, value in expected.items():
        if proof[key] != value:
            raise ValueError(f"collector evidence is not bound to {key}")
    observed_components = proof["observed_components"]
    if observed_components != trust_roots["components"]:
        raise ValueError(
            "collector did not observe the trusted principals and executables"
        )
    _validate_file_binding(
        proof["diagnostic_evidence"], "collector diagnostic evidence"
    )
    _consume_fresh_challenge(
        observation_challenge,
        proof["observed_at"],
        consumed_challenges=consumed_challenges,
        label="collector",
        now=now,
    )
    return envelope


def _validate_runner_receipt(
    result: dict,
    *,
    trial: dict,
    collector_evidence_sha256: str,
) -> dict:
    receipt = _load_command_json(result, "session runner")
    required = {
        "schema_version",
        "trial_id",
        "condition",
        "delivery_state",
        "retry_count",
        "collector_evidence_sha256",
        "reconciliation_required",
    }
    if set(receipt) != required:
        raise ValueError("session runner receipt has incomplete or unknown fields")
    if receipt["schema_version"] != "openadapt.citrix-trial-receipt.v1":
        raise ValueError("session runner receipt schema is invalid")
    if receipt["trial_id"] != trial["id"] or receipt["condition"] != trial["condition"]:
        raise ValueError("session runner receipt is not bound to the trial")
    if receipt["retry_count"] != 0:
        raise ValueError("a counted acceptance trial must not retry")
    if receipt["collector_evidence_sha256"] != collector_evidence_sha256:
        raise ValueError("session runner receipt is not bound to collector evidence")
    required_delivery = {
        "healthy": "dispatched",
        "wrong_session_or_entity": "not_dispatched",
        "ambiguity": "not_dispatched",
        "stale_state": "not_dispatched",
        "display_drift": "not_dispatched",
        "partial_effect": "dispatched",
        "reconnect": "not_dispatched",
        "commit_timeout": "uncertain",
    }[trial["condition"]]
    if receipt["delivery_state"] != required_delivery:
        raise ValueError(
            f"condition {trial['condition']} requires delivery state {required_delivery}"
        )
    if trial["condition"] == "commit_timeout":
        if receipt["reconciliation_required"] is not True:
            raise ValueError("commit-timeout requires independent reconciliation")
    elif receipt["reconciliation_required"] is not False:
        raise ValueError("only commit-timeout uses the uncertainty reconciliation path")
    return receipt


def _trial_passed(trial: dict, receipt: dict, before: dict, after: dict) -> bool:
    before_payload = before["payload"]
    after_payload = after["payload"]
    if trial["condition"] == "healthy":
        return (
            before_payload["effect_status"] == "REFUTED"
            and after_payload["effect_status"] == "CONFIRMED"
            and before_payload["state_digest"] != after_payload["state_digest"]
        )
    if trial["condition"] == "commit_timeout":
        # The terminal result stays HALTED_UNCERTAIN even if reconciliation
        # later proves or refutes the effect. A blind retry is never allowed.
        return after_payload["effect_status"] in {"CONFIRMED", "REFUTED"}
    if trial["condition"] == "partial_effect":
        return before_payload["state_digest"] != after_payload[
            "state_digest"
        ] and after_payload["effect_status"] in {"REFUTED", "INDETERMINATE"}
    return (
        before_payload["state_digest"] == after_payload["state_digest"]
        and before_payload["effect_status"] == after_payload["effect_status"]
    )


def _authoritative_outcome(trial: dict, *, passed: bool) -> str:
    if trial["condition"] == "commit_timeout":
        return "HALTED_UNCERTAIN"
    if trial["condition"] == "healthy" and passed:
        return "VERIFIED"
    return "HALTED"


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


class DurableJournal:
    """An exclusive, hash-chained, fsynced campaign journal."""

    def __init__(self, path: Path, *, campaign_nonce: str, config_sha256: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        self._sequence = 0
        self._previous_sha256 = "0" * 64
        self.append(
            "CAMPAIGN_OPENED",
            {"campaign_nonce": campaign_nonce, "config_sha256": config_sha256},
        )
        _fsync_directory(path.parent)

    @classmethod
    def reopen(cls, path: Path) -> tuple[DurableJournal, list[dict]]:
        _require_secure_regular_file(path, "campaign journal")
        records = cls.read_verified(path)
        journal = cls.__new__(cls)
        journal.path = path
        journal._sequence = len(records)
        journal._previous_sha256 = records[-1]["record_sha256"]
        return journal, records

    @staticmethod
    def read_verified(path: Path) -> list[dict]:
        records: list[dict] = []
        previous = "0" * 64
        for sequence, line in enumerate(path.read_text().splitlines()):
            record = json.loads(line)
            if not isinstance(record, dict) or set(record) != {
                "sequence",
                "recorded_at",
                "event",
                "previous_sha256",
                "payload",
                "record_sha256",
            }:
                raise ValueError("journal record has an invalid field set")
            unsigned = {
                key: value for key, value in record.items() if key != "record_sha256"
            }
            if record["sequence"] != sequence or record["previous_sha256"] != previous:
                raise ValueError("journal sequence or hash chain is invalid")
            if record["record_sha256"] != _object_sha256(unsigned):
                raise ValueError("journal record digest is invalid")
            records.append(record)
            previous = record["record_sha256"]
        if not records:
            raise ValueError("journal is empty")
        return records

    def append(self, event: str, payload: dict) -> dict:
        unsigned = {
            "sequence": self._sequence,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "previous_sha256": self._previous_sha256,
            "payload": payload,
        }
        record_sha256 = _object_sha256(unsigned)
        record = {**unsigned, "record_sha256": record_sha256}
        flags = os.O_WRONLY | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags)
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode) or mode & stat.S_IWOTH:
            os.close(descriptor)
            raise ValueError("campaign journal changed to an unsafe file")
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._sequence += 1
        self._previous_sha256 = record_sha256
        return record

    def close(self) -> None:
        return None


def _require_secure_directory(path: Path, label: str) -> None:
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError(f"{label} must be an existing absolute directory")
    if path.stat().st_mode & stat.S_IWOTH:
        raise ValueError(f"{label} must not be world-writable")


def _reserve_campaign_nonce(
    registry: Path,
    *,
    campaign_nonce: str,
    config_sha256: str,
    trust_roots_sha256: str,
    output: Path,
    execution_challenge: str,
) -> tuple[dict, bool]:
    _require_secure_directory(registry, "nonce registry")
    if not output.is_absolute():
        raise ValueError("output must be an absolute path")
    resolved_output = str(output.resolve(strict=False))
    binding_path = registry / f"{campaign_nonce}.binding.json"
    journal_path = registry / f"{campaign_nonce}.journal.jsonl"
    fallback_path = registry / f"{campaign_nonce}.terminal.json"
    expected = {
        "schema_version": "openadapt.citrix-campaign-binding.v1",
        "campaign_nonce": campaign_nonce,
        "config_sha256": config_sha256,
        "trust_roots_sha256": trust_roots_sha256,
        "output": resolved_output,
        "journal": str(journal_path),
        "terminal_fallback": str(fallback_path),
        "execution_challenge": execution_challenge,
    }
    try:
        descriptor = os.open(
            binding_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(expected, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(registry)
        return expected, True
    except FileExistsError:
        _require_secure_regular_file(binding_path, "campaign nonce binding")
        existing = json.loads(binding_path.read_text())
        immutable = {
            key: value
            for key, value in expected.items()
            if key != "execution_challenge"
        }
        observed = {
            key: value
            for key, value in existing.items()
            if key != "execution_challenge"
        }
        if observed != immutable:
            raise ValueError(
                "campaign nonce is already bound to another config, trust root, output, or journal"
            )
        if (
            not isinstance(existing.get("execution_challenge"), str)
            or SHA256_RE.fullmatch(existing["execution_challenge"]) is None
        ):
            raise ValueError("campaign binding has no valid execution challenge")
        return existing, False


def _retain_failure_evidence(output: Path, trial_id: str, value: dict) -> dict:
    safe_trial_id = re.sub(r"[^A-Za-z0-9_.-]", "_", trial_id)
    evidence_path = output.with_name(f"{output.name}.{safe_trial_id}.failure.json")
    _atomic_write_json(evidence_path, value)
    return {"path": str(evidence_path), "sha256": _sha256(evidence_path)}


def _write_terminal_report(
    output: Path,
    fallback: Path,
    report: dict,
    journal: DurableJournal,
    *,
    require_primary: bool = False,
) -> None:
    report["journal"] = {
        "path": str(journal.path),
        "sha256": _sha256(journal.path),
    }
    _atomic_write_json(fallback, report)
    try:
        _atomic_write_json(output, report)
    except OSError as exc:
        report["primary_report_error"] = str(exc)
        _atomic_write_json(fallback, report)
        if require_primary:
            raise


def _recover_reserved_campaign(binding: dict) -> dict:
    output = Path(binding["output"])
    fallback = Path(binding["terminal_fallback"])
    journal_path = Path(binding["journal"])
    if journal_path.exists():
        journal, records = DurableJournal.reopen(journal_path)
    else:
        journal = DurableJournal(
            journal_path,
            campaign_nonce=binding["campaign_nonce"],
            config_sha256=binding["config_sha256"],
        )
        records = DurableJournal.read_verified(journal_path)
    report: dict = {}
    for candidate in (fallback, output):
        if candidate.is_file():
            try:
                value = json.loads(candidate.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(value, dict):
                continue
            expected_binding = {
                "schema_version": REPORT_SCHEMA,
                "campaign_nonce": binding["campaign_nonce"],
                "config_sha256": binding["config_sha256"],
                "trust_roots_sha256": binding["trust_roots_sha256"],
            }
            if all(
                value.get(key) == expected for key, expected in expected_binding.items()
            ):
                report = value
                break
    last = records[-1]
    if last["event"] in {"CAMPAIGN_COMPLETE", "CAMPAIGN_TERMINAL"}:
        report.update(
            {
                "schema_version": REPORT_SCHEMA,
                "campaign_nonce": binding["campaign_nonce"],
                "config_sha256": binding["config_sha256"],
                "trust_roots_sha256": binding["trust_roots_sha256"],
                "accepted": last["event"] == "CAMPAIGN_COMPLETE",
                "terminal": True,
                "recovered": True,
            }
        )
        report.setdefault("trials", [])
        if last["event"] == "CAMPAIGN_COMPLETE":
            report.setdefault("terminal_reason", "campaign_complete")
        else:
            report.setdefault("terminal_reason", last["payload"].get("reason"))
            terminal_trial_id = str(last["payload"].get("trial_id", "unknown"))
            if not any(
                isinstance(row, dict) and row.get("id") == terminal_trial_id
                for row in report["trials"]
            ):
                report["trials"].append(
                    {
                        "id": terminal_trial_id,
                        "outcome": last["payload"].get("outcome", "HALTED"),
                        "passed": False,
                        "recovered_from_journal": True,
                    }
                )
        _write_terminal_report(output, fallback, report, journal)
        journal.close()
        return report
    report.update(
        {
            "schema_version": REPORT_SCHEMA,
            "campaign_nonce": binding["campaign_nonce"],
            "config_sha256": binding["config_sha256"],
            "trust_roots_sha256": binding["trust_roots_sha256"],
            "accepted": False,
            "terminal": True,
            "recovered": True,
        }
    )
    report.setdefault("trials", [])
    trial_id = str(last["payload"].get("trial_id", "unknown"))
    if last["event"] == "DISPATCH_ATTEMPT":
        row = {
            "id": trial_id,
            "outcome": "HALTED_UNCERTAIN",
            "delivery_state": "uncertain",
            "retry_count": 0,
            "reconciliation_required": True,
            "passed": False,
        }
        reason = "recovered_dispatch_attempt"
    else:
        row = {
            "id": trial_id,
            "outcome": "HALTED",
            "delivery_state": "not_dispatched",
            "retry_count": 0,
            "reconciliation_required": False,
            "passed": False,
        }
        reason = "recovered_before_dispatch"
    report["trials"].append(row)
    report["terminal_reason"] = reason
    journal.append(
        "CAMPAIGN_TERMINAL",
        {"trial_id": trial_id, "reason": reason, "outcome": row["outcome"]},
    )
    _write_terminal_report(output, fallback, report, journal)
    journal.close()
    return report


def run_campaign(
    config: dict,
    *,
    trust_roots: dict,
    config_sha256: str,
    output: Path,
    nonce_registry: Path,
) -> dict:
    proposed_execution_challenge = secrets.token_hex(32)
    binding, reserved = _reserve_campaign_nonce(
        nonce_registry,
        campaign_nonce=config["campaign_nonce"],
        config_sha256=config_sha256,
        trust_roots_sha256=trust_roots["path_sha256"],
        output=output,
        execution_challenge=proposed_execution_challenge,
    )
    if not reserved:
        return _recover_reserved_campaign(binding)
    execution_challenge = binding["execution_challenge"]
    fallback = Path(binding["terminal_fallback"])
    report = {
        "schema_version": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "campaign_nonce": config["campaign_nonce"],
        "config_sha256": config_sha256,
        "trust_roots_sha256": trust_roots["path_sha256"],
        "preflight": "passed",
        "executed": True,
        "execution_challenge": execution_challenge,
        "host": {"platform": platform.platform(), "python": sys.version.split()[0]},
        "fingerprints": config["fingerprints"],
        "accepted": False,
        "terminal": False,
        "trials": [],
    }
    runner = config["runner_authority"]["command"]
    runner_sha256 = config["runner_authority"]["executable_sha256"]
    oracle = config["independent_oracle"]["command"]
    oracle_sha256 = config["independent_oracle"]["executable_sha256"]
    oracle_principal = config["independent_oracle"]["principal_sha256"]
    oracle_public_key = trust_roots["decoded_keys"]["oracle_authority"]
    collector = config["collector_authority"]["command"]
    collector_sha256 = config["collector_authority"]["executable_sha256"]
    journal_path = Path(binding["journal"])
    journal = DurableJournal(
        journal_path,
        campaign_nonce=config["campaign_nonce"],
        config_sha256=config_sha256,
    )
    _write_terminal_report(
        output,
        fallback,
        report,
        journal,
        require_primary=True,
    )
    consumed_challenges: set[str] = set()

    def terminate(row: dict, reason: str) -> dict:
        report["trials"].append(row)
        report["terminal"] = True
        report["terminal_reason"] = reason
        report["accepted"] = False
        journal.append(
            "CAMPAIGN_TERMINAL",
            {"trial_id": row["id"], "reason": reason, "outcome": row["outcome"]},
        )
        _write_terminal_report(output, fallback, report, journal)
        journal.close()
        return report

    for trial in config["trials"]:
        binding_args = [
            "--campaign-nonce",
            config["campaign_nonce"],
            "--config-sha256",
            config_sha256,
            "--execution-challenge",
            execution_challenge,
            "--trial-id",
            trial["id"],
            "--entity-sha256",
            trial["entity_sha256"],
            "--effect-contract-sha256",
            trial["effect_contract_sha256"],
        ]
        before_result: dict | None = None
        collector_result: dict | None = None
        runner_result: dict | None = None
        after_result: dict | None = None
        before_challenge = secrets.token_hex(32)
        collector_challenge = secrets.token_hex(32)
        try:
            before_result = _run_pinned(
                oracle,
                oracle_sha256,
                [
                    "observe",
                    "--phase",
                    "before",
                    "--observation-challenge",
                    before_challenge,
                    *binding_args,
                ],
            )
            before = _validate_oracle_evidence(
                before_result,
                trial=trial,
                phase="before",
                oracle_principal_sha256=oracle_principal,
                oracle_public_key=oracle_public_key,
                campaign_nonce=config["campaign_nonce"],
                config_sha256=config_sha256,
                execution_challenge=execution_challenge,
                observation_challenge=before_challenge,
                consumed_challenges=consumed_challenges,
            )
            if before["payload"]["effect_status"] != "REFUTED":
                raise ValueError("safe REFUTED effect baseline is required")
            collector_result = _run_pinned(
                collector,
                collector_sha256,
                [
                    "collect",
                    "--observation-challenge",
                    collector_challenge,
                    *binding_args,
                ],
            )
            collector_evidence = _validate_collector_evidence(
                collector_result,
                trial=trial,
                config=config,
                config_sha256=config_sha256,
                trust_roots=trust_roots,
                execution_challenge=execution_challenge,
                observation_challenge=collector_challenge,
                consumed_challenges=consumed_challenges,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            retained = _retain_failure_evidence(
                output,
                trial["id"],
                {
                    "phase": "pre_dispatch",
                    "error": str(exc),
                    "oracle_before_result": before_result,
                    "collector_result": collector_result,
                },
            )
            journal.append(
                "PRE_DISPATCH_REFUSED",
                {"trial_id": trial["id"], "evidence": retained},
            )
            return terminate(
                {
                    "id": trial["id"],
                    "condition": trial["condition"],
                    "expected": trial["expected"],
                    "outcome": "HALTED",
                    "delivery_state": "not_dispatched",
                    "retry_count": 0,
                    "reconciliation_required": False,
                    "passed": False,
                    "failure_evidence": retained,
                },
                "pre_dispatch_safety_refusal",
            )

        collector_evidence_sha256 = _object_sha256(collector_evidence)
        journal.append(
            "PRE_DISPATCH_DURABLE",
            {
                "trial_id": trial["id"],
                "condition": trial["condition"],
                "entity_sha256": trial["entity_sha256"],
                "effect_contract_sha256": trial["effect_contract_sha256"],
                "oracle_before_sha256": _object_sha256(before),
                "collector_evidence_sha256": collector_evidence_sha256,
                "execution_challenge": execution_challenge,
                "before_challenge": before_challenge,
                "collector_challenge": collector_challenge,
            },
        )
        journal.append(
            "DISPATCH_ATTEMPT",
            {"trial_id": trial["id"], "retry_count": 0},
        )
        receipt: dict | None = None
        after: dict | None = None
        after_challenge = secrets.token_hex(32)
        post_dispatch_error: Exception | None = None
        try:
            runner_result = _run_pinned(
                runner,
                runner_sha256,
                ["execute-trial", "--condition", trial["condition"], *binding_args],
            )
            receipt = _validate_runner_receipt(
                runner_result,
                trial=trial,
                collector_evidence_sha256=collector_evidence_sha256,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            post_dispatch_error = exc

        try:
            after_result = _run_pinned(
                oracle,
                oracle_sha256,
                [
                    "observe",
                    "--phase",
                    "after",
                    "--observation-challenge",
                    after_challenge,
                    *binding_args,
                ],
            )
            after = _validate_oracle_evidence(
                after_result,
                trial=trial,
                phase="after",
                oracle_principal_sha256=oracle_principal,
                oracle_public_key=oracle_public_key,
                campaign_nonce=config["campaign_nonce"],
                config_sha256=config_sha256,
                execution_challenge=execution_challenge,
                observation_challenge=after_challenge,
                consumed_challenges=consumed_challenges,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            if post_dispatch_error is None:
                post_dispatch_error = exc

        if post_dispatch_error is not None:
            retained = _retain_failure_evidence(
                output,
                trial["id"],
                {
                    "phase": "post_dispatch",
                    "error": str(post_dispatch_error),
                    "oracle_before": before,
                    "collector_evidence": collector_evidence,
                    "runner_result": runner_result,
                    "oracle_after_result": after_result,
                    "oracle_after": after,
                },
            )
            journal.append(
                "POST_DISPATCH_HALTED_UNCERTAIN",
                {"trial_id": trial["id"], "evidence": retained, "retry_count": 0},
            )
            return terminate(
                {
                    "id": trial["id"],
                    "condition": trial["condition"],
                    "expected": trial["expected"],
                    "outcome": "HALTED_UNCERTAIN",
                    "delivery_state": "uncertain",
                    "retry_count": 0,
                    "reconciliation_required": True,
                    "passed": False,
                    "collector_evidence": collector_evidence,
                    "oracle_before": before,
                    "oracle_after": after,
                    "failure_evidence": retained,
                },
                "post_dispatch_error",
            )

        assert receipt is not None and after is not None
        passed = _trial_passed(trial, receipt, before, after)
        outcome = _authoritative_outcome(trial, passed=passed)
        row = {
            "id": trial["id"],
            "condition": trial["condition"],
            "expected": trial["expected"],
            "outcome": outcome,
            "passed": passed,
            "receipt": receipt,
            "collector_evidence": collector_evidence,
            "oracle_before": before,
            "oracle_after": after,
        }
        report["trials"].append(row)
        journal.append(
            "TRIAL_TERMINAL",
            {"trial_id": trial["id"], "outcome": outcome, "passed": passed},
        )
        if not passed:
            report["trials"].pop()
            return terminate(row, "first_failed_safety_identity_or_effect_trial")
        _write_terminal_report(output, fallback, report, journal)

    report["accepted"] = True
    report["terminal"] = True
    report["terminal_reason"] = "campaign_complete"
    journal.append(
        "CAMPAIGN_COMPLETE",
        {"trial_count": len(report["trials"]), "accepted": True},
    )
    _write_terminal_report(output, fallback, report, journal)
    journal.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--trust-roots",
        type=Path,
        required=True,
        help="absolute trust-root file provisioned outside the campaign config",
    )
    parser.add_argument(
        "--nonce-registry",
        type=Path,
        required=True,
        help="secure customer registry that gives each campaign nonce one output",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="invoke the approved runner against the confirmed pre-existing session",
    )
    args = parser.parse_args()
    try:
        trust_roots = load_trust_roots(args.trust_roots)
        _require_secure_directory(args.nonce_registry, "nonce registry")
        config = load_config(args.config, trust_roots)
        config_sha256 = hashlib.sha256(args.config.read_bytes()).hexdigest()
        report = {
            "schema_version": REPORT_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "campaign_nonce": config["campaign_nonce"],
            "config_sha256": config_sha256,
            "trust_roots_sha256": trust_roots["path_sha256"],
            "preflight": "passed",
            "executed": bool(args.execute),
            "fingerprints": config["fingerprints"],
            "trials": [],
        }
        if args.execute:
            report = run_campaign(
                config,
                trust_roots=trust_roots,
                config_sha256=config_sha256,
                output=args.output,
                nonce_registry=args.nonce_registry,
            )
            report["preflight"] = "passed"
            report["executed"] = True
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"acceptance refused: {exc}", file=sys.stderr)
        return 2
    if not args.execute:
        _atomic_write_json(args.output, report)
    if not args.execute:
        print("preflight passed; no trial ran")
        return 0
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
