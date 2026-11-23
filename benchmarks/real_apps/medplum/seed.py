"""Synthetic FHIR R4 seed fixtures for the Medplum real-app verification harness (0 PHI)."""

from __future__ import annotations

import json
import urllib.request
from typing import Any

SYNTHETIC_PATIENTS: tuple[dict[str, Any], ...] = (
    {
        "resourceType": "Patient",
        "id": "pat-synth-001",
        "identifier": [{"system": "urn:openadapt:mrn", "value": "MRN-88041"}],
        "name": [{"family": "Testwell", "given": ["Alex"]}],
        "birthDate": "1984-03-14",
        "active": True,
    },
    {
        "resourceType": "Patient",
        "id": "pat-synth-002",
        "identifier": [{"system": "urn:openadapt:mrn", "value": "MRN-88O41"}],
        "name": [{"family": "Testwell", "given": ["Alex"]}],
        "birthDate": "1984-03-14",
        "active": True,
    },
)


def seed_app(
    base_url: str = "http://127.0.0.1:8103",
    *,
    bearer_token: str | None = None,
    timeout_s: float = 10.0,
) -> list[dict[str, Any]]:
    """Seed synthetic FHIR Patient resources via PUT to ``/fhir/R4/Patient/<id>``."""
    headers = {
        "Content-Type": "application/fhir+json",
        "Accept": "application/fhir+json",
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    seeded: list[dict[str, Any]] = []
    root = base_url.rstrip("/")
    for patient in SYNTHETIC_PATIENTS:
        url = f"{root}/fhir/R4/Patient/{patient['id']}"
        data = json.dumps(patient).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            seeded.append(body)
    return seeded
