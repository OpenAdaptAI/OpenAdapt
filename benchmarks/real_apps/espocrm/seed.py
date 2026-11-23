"""Synthetic insurance brokerage AMS fixtures for EspoCRM (0 real customer data)."""

from __future__ import annotations

import json
import urllib.request
from typing import Any

SYNTHETIC_ACCOUNTS: tuple[dict[str, Any], ...] = (
    {
        "id": "acct-broker-001",
        "name": "Northstar Home Brokerage",
        "type": "Customer",
        "industry": "Insurance",
        "billingAddressCity": "Toronto",
    },
    {
        "id": "acct-broker-002",
        "name": "Northstar Home Brokerage East",
        "type": "Customer",
        "industry": "Insurance",
        "billingAddressCity": "Ottawa",
    },
)


def seed_app(
    base_url: str = "http://127.0.0.1:8085",
    *,
    api_key: str | None = None,
    timeout_s: float = 10.0,
) -> list[dict[str, Any]]:
    """Seed synthetic Account records via POST to ``/api/v1/Account``."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    seeded: list[dict[str, Any]] = []
    url = f"{base_url.rstrip('/')}/api/v1/Account"
    for acct in SYNTHETIC_ACCOUNTS:
        req = urllib.request.Request(
            url,
            data=json.dumps(acct).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            seeded.append(json.loads(resp.read().decode("utf-8")))
    return seeded
