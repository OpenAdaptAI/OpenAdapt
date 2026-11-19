"""The connector's egress allow-list: the only bytes that may leave the machine.

Every request body the connector sends to the control plane is built here, from
a closed set of fields with closed value shapes. Anything not on the list is
refused before a socket opens. This is the code that enforces the runner's data
boundary; the docstrings elsewhere describe it, this module makes it true.

What may leave (and nothing else):

* identifiers the control plane issued (run, org, workflow, bundle version,
  runtime validation, lease job ids);
* SHA-256 digests (bundle archive, read-back spec, effect receipt);
* closed labels (status, outcome, error code, effect verdict);
* structural counts (steps, halts, heals, model calls, cost);
* the opaque report key the control plane itself assigned;
* the closed outcome envelope and failure signal Flow already defines as
  PHI-free (re-validated here, with every string leaf restricted to a short
  token that cannot hold free text).

Record values, screenshots, OCR text, typed values, halt reasons, exception
text, file paths and secrets have no field to travel in. As a second wall, every
serialized body is scanned for every encoding of every secret value the local
secret store resolved; a hit refuses the send.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Optional

from openadapt_flow.wire_check import carries_secret

__all__ = [
    "EgressViolation",
    "ERROR_CODE",
    "guard_ack",
    "guard_callback",
    "guard_heartbeat",
    "guard_poll",
    "guard_empty",
    "serialize_checked",
]


class EgressViolation(ValueError):
    """A body would carry something outside the allow-list. Never sent."""


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HEX64 = re.compile(r"^[a-f0-9]{64}$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LABEL = re.compile(r"^[A-Za-z_]{1,40}$")
#: A string leaf inside the closed envelopes: a short token, no whitespace.
_TOKEN = re.compile(r"^[A-Za-z0-9_.:/+-]{0,128}$")
_REPORT_KEY = re.compile(
    r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}(/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}){0,7}$"
)
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_STATUSES = {"running", "success", "halt", "failed"}
_ACK_STATUSES = {"done", "failed"}
_MODES = {"replay", "teach"}
_METRIC_KEYS = {"steps", "steps_ok", "halts", "heals", "model_calls", "cost_usd"}
_READBACK_KEYS = {
    "readback_sha256",
    "outcome",
    "effect_verdict",
    "verification_tier",
    "receipt_sha256",
}
_FAILURE_SIGNAL_KEYS = {
    "schema",
    "failure_kind",
    "substrate",
    "action_kind",
    "risk_class",
    "resolution_rung",
    "identity_state",
    "effect_tier",
    "delivery_state",
    "execution_profile",
    "outcome",
    "runtime_version",
    "model_calls",
    "external_network_calls",
    "occurred_at",
}
MAX_ACTIVE_JOBS = 64


def _ident(value: Any, field: str, *, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise EgressViolation(f"{field} is not a control-plane identifier")
    return value


def _hex64(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise EgressViolation(f"{field} is not a SHA-256 digest")
    return value


def _number(value: Any, field: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EgressViolation(f"metric {field} is not a number")
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        raise EgressViolation(f"metric {field} is not a finite count")
    return value


def _closed_tree(value: Any, field: str, depth: int = 0) -> Any:
    """Re-walk a closed envelope: dicts, lists, bools, numbers, short tokens."""
    if depth > 6:
        raise EgressViolation(f"{field} nests too deeply")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _number(value, field)
    if isinstance(value, str):
        if not _TOKEN.fullmatch(value):
            raise EgressViolation(f"{field} carries a string that is not a short token")
        return value
    if isinstance(value, list):
        if len(value) > 256:
            raise EgressViolation(f"{field} is too long")
        return [_closed_tree(item, field, depth + 1) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _LABEL.fullmatch(key):
                raise EgressViolation(f"{field} has a key that is not a label")
            out[key] = _closed_tree(item, f"{field}.{key}", depth + 1)
        return out
    raise EgressViolation(f"{field} has an unsupported value type")


def _outcome_envelope(value: Any) -> dict[str, Any]:
    from openadapt_flow.ir import ExecutionOutcomeEnvelope

    try:
        model = ExecutionOutcomeEnvelope.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise EgressViolation("outcome is not a valid closed outcome envelope") from exc
    dumped = model.model_dump(mode="json")
    return _closed_tree(dumped, "outcome")


def _failure_signal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _FAILURE_SIGNAL_KEYS:
        raise EgressViolation("failure_signal is not the closed v1 envelope")
    if value.get("schema") != "openadapt.automation-failure-signal/v1":
        raise EgressViolation("failure_signal has an unknown schema")
    return _closed_tree(value, "failure_signal")


def _wrap_readback(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _READBACK_KEYS:
        raise EgressViolation("wrap_readback is not the closed read-back record")
    out = {
        "readback_sha256": _hex64(
            value["readback_sha256"], "wrap_readback.readback_sha256"
        ),
        "receipt_sha256": _hex64(
            value["receipt_sha256"], "wrap_readback.receipt_sha256"
        ),
    }
    for key in ("outcome", "effect_verdict", "verification_tier"):
        label = value[key]
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            raise EgressViolation(f"wrap_readback.{key} is not a label")
        out[key] = label
    return out


def _metrics(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EgressViolation("metrics is not an object")
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key == "wrap_readback":
            if item is not None:
                out[key] = _wrap_readback(item)
            continue
        if key not in _METRIC_KEYS:
            raise EgressViolation(f"metric {key!r} is not on the allow-list")
        out[key] = _number(item, key)
    return out


def guard_callback(body: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild a run-callback body from allow-listed fields, or refuse."""
    allowed = {
        "run_id",
        "org_id",
        "workflow_id",
        "bundle_version_id",
        "runtime_validation_id",
        "mode",
        "status",
        "report_path",
        "metrics",
        "error_code",
        "bundle_sha256",
        "halt",
        "outcome",
        "failure_signal",
    }
    extra = set(body) - allowed
    if extra:
        raise EgressViolation(
            f"callback field(s) not on the allow-list: {sorted(extra)}"
        )
    out: dict[str, Any] = {
        "run_id": _ident(body.get("run_id"), "run_id"),
        "org_id": _ident(body.get("org_id"), "org_id"),
        "workflow_id": _ident(body.get("workflow_id"), "workflow_id"),
        "bundle_version_id": _ident(
            body.get("bundle_version_id"), "bundle_version_id", optional=True
        ),
        "runtime_validation_id": _ident(
            body.get("runtime_validation_id"), "runtime_validation_id", optional=True
        ),
    }
    mode = body.get("mode", "replay")
    if mode not in _MODES:
        raise EgressViolation("mode is not replay or teach")
    out["mode"] = mode
    status = body.get("status")
    if status not in _STATUSES:
        raise EgressViolation("status is not a lifecycle status")
    out["status"] = status
    report_path = body.get("report_path")
    if report_path is not None:
        if (
            not isinstance(report_path, str)
            or not _REPORT_KEY.fullmatch(report_path)
            or ".." in report_path.split("/")
        ):
            raise EgressViolation("report_path is not an opaque relative storage key")
    out["report_path"] = report_path
    out["metrics"] = _metrics(body.get("metrics"))
    error_code = body.get("error_code")
    if error_code is not None and (
        not isinstance(error_code, str) or not ERROR_CODE.fullmatch(error_code)
    ):
        raise EgressViolation("error_code is not a closed code")
    out["error_code"] = error_code
    if "bundle_sha256" in body:
        out["bundle_sha256"] = _hex64(body["bundle_sha256"], "bundle_sha256")
    if "halt" in body:
        if body["halt"] != {"present": True}:
            raise EgressViolation("halt may only say that a halt is present")
        out["halt"] = {"present": True}
    if "outcome" in body and body["outcome"] is not None:
        out["outcome"] = _outcome_envelope(body["outcome"])
    if "failure_signal" in body and body["failure_signal"] is not None:
        out["failure_signal"] = _failure_signal(body["failure_signal"])
    return out


def guard_ack(job_id: Any, status: Any, error_code: Any = None) -> dict[str, Any]:
    if status not in _ACK_STATUSES:
        raise EgressViolation("ack status is not done or failed")
    if error_code is not None and (
        not isinstance(error_code, str) or not ERROR_CODE.fullmatch(error_code)
    ):
        raise EgressViolation("ack error_code is not a closed code")
    return {
        "job_id": _ident(job_id, "job_id"),
        "status": status,
        "error_code": error_code,
    }


def _active_ids(active_job_ids: Iterable[Any]) -> list[str]:
    ids = [_ident(item, "active_job_ids[]") for item in active_job_ids]
    if len(ids) > MAX_ACTIVE_JOBS:
        raise EgressViolation("too many active job ids")
    return [item for item in ids if item is not None]


def guard_poll(
    wait_s: Any,
    capabilities: Iterable[Any],
    active_job_ids: Iterable[Any] = (),
    lease_seconds: Any = None,
) -> dict[str, Any]:
    if isinstance(wait_s, bool) or not isinstance(wait_s, int) or not 0 <= wait_s <= 50:
        raise EgressViolation("poll wait is not 0..50 seconds")
    caps = []
    for cap in capabilities:
        if not isinstance(cap, str) or not _CAPABILITY.fullmatch(cap):
            raise EgressViolation("capability is not a closed token")
        caps.append(cap)
    body: dict[str, Any] = {
        "wait": wait_s,
        "capabilities": caps,
        "active_job_ids": _active_ids(active_job_ids),
    }
    if lease_seconds is not None:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise EgressViolation("lease_seconds is not an integer")
        body["lease_seconds"] = lease_seconds
    return body


def guard_heartbeat(active_job_ids: Iterable[Any]) -> dict[str, Any]:
    return {"active_job_ids": _active_ids(active_job_ids)}


def guard_empty() -> dict[str, Any]:
    return {}


def serialize_checked(
    body: Mapping[str, Any], secret_values: Iterable[str] = ()
) -> bytes:
    """Serialize an allow-listed body and refuse it if any secret shows up."""
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
    secrets = [value for value in secret_values if value and len(value) >= 4]
    if secrets and carries_secret(raw, secrets):
        raise EgressViolation("a local secret value would leave the machine; refused")
    return raw.encode("utf-8")
