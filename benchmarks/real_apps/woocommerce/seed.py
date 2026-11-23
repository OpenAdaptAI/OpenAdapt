"""Synthetic WooCommerce order & refund fixtures (0 real customer or payment data)."""

from __future__ import annotations

import base64
import json
import urllib.request
from typing import Any

SYNTHETIC_ORDERS: tuple[dict[str, Any], ...] = (
    {
        "id": 1001,
        "status": "processing",
        "currency": "USD",
        "total": "149.50",
        "billing": {
            "first_name": "Casey",
            "last_name": "Testbuyer",
            "email": "casey@example.test",
        },
    },
    {
        "id": 1002,
        "status": "processing",
        "currency": "USD",
        "total": "89.00",
        "billing": {
            "first_name": "Casey",
            "last_name": "Testbuyer",
            "email": "casey@example.test",
        },
    },
)


def _auth_headers(
    consumer_key: str | None, consumer_secret: str | None
) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if consumer_key and consumer_secret:
        token = base64.b64encode(
            f"{consumer_key}:{consumer_secret}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return headers


def seed_app(
    base_url: str = "http://127.0.0.1:8086",
    *,
    consumer_key: str | None = None,
    consumer_secret: str | None = None,
    timeout_s: float = 10.0,
) -> list[dict[str, Any]]:
    """Seed synthetic WooCommerce orders via POST ``/wp-json/wc/v3/orders``."""
    headers = _auth_headers(consumer_key, consumer_secret)
    url = f"{base_url.rstrip('/')}/wp-json/wc/v3/orders"
    seeded: list[dict[str, Any]] = []
    for order in SYNTHETIC_ORDERS:
        req = urllib.request.Request(
            url,
            data=json.dumps(order).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            seeded.append(json.loads(resp.read().decode("utf-8")))
    return seeded
