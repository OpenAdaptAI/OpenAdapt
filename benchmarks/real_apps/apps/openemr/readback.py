"""Independent FHIR R4 HTTP read-back oracle for OpenEMR."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from benchmarks.real_apps.harness import ReadbackResult


def readback_fhir_service_request(
    fhir_base_url: str,
    *,
    external_ref: str,
    expected_patient_id: str,
    expected_reason: str,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10.0,
) -> ReadbackResult:
    """Query OpenEMR's FHIR R4 ``ServiceRequest`` endpoint out-of-band."""
    url = f"{fhir_base_url.rstrip('/')}/ServiceRequest/{external_ref}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/fhir+json", **(headers or {})},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status != 200:
                return ReadbackResult(
                    confirmed=False,
                    oracle="openemr-fhir-r4",
                    record_id=external_ref,
                    reason=f"Unexpected HTTP {resp.status}",
                )
            data: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return ReadbackResult(
            confirmed=False,
            oracle="openemr-fhir-r4",
            record_id=external_ref,
            reason=f"FHIR read-back HTTP {err.code}",
        )
    except Exception as exc:
        return ReadbackResult(
            confirmed=False,
            oracle="openemr-fhir-r4",
            record_id=external_ref,
            reason=f"FHIR read-back failed: {exc}",
        )

    subject_ref = str((data.get("subject") or {}).get("reference") or "")
    reasons = data.get("reasonCode") or []
    reason_text = str(reasons[0].get("text") or "") if reasons else ""
    matched = (
        data.get("resourceType") == "ServiceRequest"
        and data.get("id") == external_ref
        and subject_ref == f"Patient/{expected_patient_id}"
        and reason_text == expected_reason
    )
    return ReadbackResult(
        confirmed=matched,
        oracle="openemr-fhir-r4",
        record_id=external_ref,
        observed_fields={
            "subject": subject_ref,
            "reason": reason_text,
            "status": data.get("status"),
        },
        reason="" if matched else "FHIR ServiceRequest fields did not match expected write",
    )
