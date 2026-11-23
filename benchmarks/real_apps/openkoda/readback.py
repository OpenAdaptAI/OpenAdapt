"""Independent REST read-back oracle for Openkoda P&C Insurance."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


def verify_write(
    base_url: str,
    entity_type: str,
    lookup_field: str,
    lookup_value: str,
    expected_fields: Mapping[str, Any],
    *,
    api_token: str | None = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Read back an Openkoda policy/claim via REST and fail closed on any mismatch."""
    query = urllib.parse.urlencode({lookup_field: lookup_value})
    url = f"{base_url.rstrip('/')}/api/v2/{entity_type}?{query}"
    headers = {"Accept": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    try:
        req = urllib.request.Request(url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {
            "verdict": "HALT",
            "channel": "openkoda_rest_api",
            "reason": f"readback_error: {exc}",
            "mismatches": ["transport_or_parse_error"],
            "observed": None,
        }

    records = payload.get("content", payload) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        return {
            "verdict": "HALT",
            "channel": "openkoda_rest_api",
            "reason": "invalid_response_shape",
            "mismatches": ["expected list payload"],
            "observed": payload,
        }

    matches = [
        r
        for r in records
        if isinstance(r, Mapping) and str(r.get(lookup_field)) == lookup_value
    ]
    if len(matches) == 0:
        return {
            "verdict": "HALT",
            "channel": "openkoda_rest_api",
            "reason": "missing_record",
            "mismatches": [
                f"no {entity_type} found with {lookup_field}={lookup_value!r}"
            ],
            "observed": [],
        }
    if len(matches) > 1:
        return {
            "verdict": "HALT",
            "channel": "openkoda_rest_api",
            "reason": "duplicate_record",
            "mismatches": [f"expected 1 {entity_type}, observed {len(matches)}"],
            "observed": matches,
        }

    record = matches[0]
    mismatches: list[str] = []
    for k, expected_val in expected_fields.items():
        actual_val = record.get(k)
        if actual_val != expected_val:
            mismatches.append(
                f"{k}: expected {expected_val!r}, observed {actual_val!r}"
            )

    return {
        "verdict": "VERIFIED" if not mismatches else "HALT",
        "channel": "openkoda_rest_api",
        "reason": "ok" if not mismatches else "field_mismatch",
        "mismatches": mismatches,
        "observed": record,
    }
