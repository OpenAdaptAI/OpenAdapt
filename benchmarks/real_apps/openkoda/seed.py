"""Synthetic P&C insurance policy and claim fixtures for Openkoda (0 real policyholder data)."""

from __future__ import annotations

import json
import urllib.request
from typing import Any

SYNTHETIC_POLICIES: tuple[dict[str, Any], ...] = (
    {
        "policyNumber": "POL-HO3-2026-001",
        "insuredName": "Jordan Sample",
        "coverageLimit": 450000,
        "deductible": 1000,
        "status": "QUOTED",
    },
    {
        "policyNumber": "POL-HO3-2026-002",
        "insuredName": "Jordan Sample",
        "coverageLimit": 325000,
        "deductible": 2500,
        "status": "QUOTED",
    },
)


def seed_app(
    base_url: str = "http://127.0.0.1:8084",
    *,
    api_token: str | None = None,
    timeout_s: float = 10.0,
) -> list[dict[str, Any]]:
    """Seed synthetic P&C policies via POST to ``/api/v2/policies``."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    seeded: list[dict[str, Any]] = []
    url = f"{base_url.rstrip('/')}/api/v2/policies"
    for policy in SYNTHETIC_POLICIES:
        req = urllib.request.Request(
            url,
            data=json.dumps(policy).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            seeded.append(json.loads(resp.read().decode("utf-8")))
    return seeded
