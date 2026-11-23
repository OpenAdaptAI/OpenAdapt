"""Independent REST API read-back oracle for EspoCRM Brokerage AMS."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


def verify_write(
    base_url: str,
    entity_type: str,
    record_name: str,
    expected_fields: Mapping[str, Any],
    *,
    api_key: str | None = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Read back an EspoCRM entity list via ``/api/v1/<Entity>`` and fail closed."""
    query = urllib.parse.urlencode(
        {
            "where[0][type]": "equals",
            "where[0][attribute]": "name",
            "where[0][value]": record_name,
        }
    )
    url = f"{base_url.rstrip('/')}/api/v1/{entity_type}?{query}"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key

    try:
        req = urllib.request.Request(url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {
            "verdict": "HALT",
            "channel": "espocrm_rest_api",
            "reason": f"readback_error: {exc}",
            "mismatches": ["transport_or_parse_error"],
            "observed": None,
        }

    rows = payload.get("list") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {
            "verdict": "HALT",
            "channel": "espocrm_rest_api",
            "reason": "invalid_response_shape",
            "mismatches": ["missing list array"],
            "observed": payload,
        }

    matches = [
        r for r in rows if isinstance(r, Mapping) and r.get("name") == record_name
    ]
    if len(matches) == 0:
        return {
            "verdict": "HALT",
            "channel": "espocrm_rest_api",
            "reason": "missing_record",
            "mismatches": [f"no {entity_type} found with name={record_name!r}"],
            "observed": [],
        }
    if len(matches) > 1:
        return {
            "verdict": "HALT",
            "channel": "espocrm_rest_api",
            "reason": "duplicate_record",
            "mismatches": [f"expected 1 {entity_type}, observed {len(matches)}"],
            "observed": matches,
        }

    rec = matches[0]
    mismatches: list[str] = []
    for k, expected_val in expected_fields.items():
        actual_val = rec.get(k)
        if actual_val != expected_val:
            mismatches.append(
                f"{k}: expected {expected_val!r}, observed {actual_val!r}"
            )

    return {
        "verdict": "VERIFIED" if not mismatches else "HALT",
        "channel": "espocrm_rest_api",
        "reason": "ok" if not mismatches else "field_mismatch",
        "mismatches": mismatches,
        "observed": rec,
    }
