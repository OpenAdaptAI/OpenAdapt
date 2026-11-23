"""Independent FHIR R4 REST read-back oracle for Medplum."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


def _extract_field(resource: Mapping[str, Any], path: str) -> Any:
    """Resolve simple dot-separated FHIR field paths (e.g. ``name.0.family``)."""
    cur: Any = resource
    for part in path.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def verify_write(
    base_url: str,
    resource_type: str,
    search_params: Mapping[str, str],
    expected_fields: Mapping[str, Any],
    *,
    bearer_token: str | None = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Read back a FHIR R4 resource bundle and fail closed on any anomaly."""
    query = urllib.parse.urlencode(sorted(search_params.items()))
    url = f"{base_url.rstrip('/')}/fhir/R4/{resource_type}?{query}"
    headers = {"Accept": "application/fhir+json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    try:
        req = urllib.request.Request(url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {
            "verdict": "HALT",
            "channel": "fhir_r4_rest",
            "reason": f"readback_error: {exc}",
            "mismatches": ["transport_or_parse_error"],
            "observed": None,
        }

    if (
        not isinstance(payload, dict)
        or payload.get("resourceType") == "OperationOutcome"
    ):
        return {
            "verdict": "HALT",
            "channel": "fhir_r4_rest",
            "reason": "operation_outcome_or_invalid_bundle",
            "mismatches": ["fhir_operation_outcome"],
            "observed": payload,
        }

    entries: list[Mapping[str, Any]] = []
    for e in payload.get("entry") or []:
        if isinstance(e, Mapping):
            res = e.get("resource")
            if isinstance(res, Mapping) and res.get("resourceType") == resource_type:
                entries.append(res)
    if len(entries) == 0:
        return {
            "verdict": "HALT",
            "channel": "fhir_r4_rest",
            "reason": "missing_record",
            "mismatches": ["expected 1 resource, observed 0"],
            "observed": [],
        }
    if len(entries) > 1:
        return {
            "verdict": "HALT",
            "channel": "fhir_r4_rest",
            "reason": "duplicate_record",
            "mismatches": [f"expected 1 resource, observed {len(entries)}"],
            "observed": entries,
        }

    resource = entries[0]
    mismatches: list[str] = []
    for field_path, expected_val in expected_fields.items():
        actual_val = _extract_field(resource, field_path)
        if actual_val != expected_val:
            mismatches.append(
                f"{field_path}: expected {expected_val!r}, observed {actual_val!r}"
            )

    return {
        "verdict": "VERIFIED" if not mismatches else "HALT",
        "channel": "fhir_r4_rest",
        "reason": "ok" if not mismatches else "field_mismatch",
        "mismatches": mismatches,
        "observed": resource,
    }
