"""The example portal's New Order form as a system of record, for the
agent-framework adapter tests: a real HTTP app with sign-in, a POST that
saves an order, ``GET /api/orders`` for the contract to read back, and
faults (an order that isn't kept, one kept twice)."""

from __future__ import annotations

from typing import Any, Iterator

import httpx
import pytest

from openadapt_flow.agent.example_app import DEMO_PASSWORD, DEMO_USER, serve


class Portal:
    def __init__(self, base: str) -> None:
        self.base = base
        self.posts = 0

    def contract(self) -> dict[str, Any]:
        return {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "rest",
                "base_url": self.base,
                "records_path": "/api/orders",
                "records_key": "records",
            },
            "effects": [
                {
                    "kind": "record_written",
                    "match": {
                        "customer": {"param": "customer"},
                        "product": {"param": "product"},
                        "units": {"param": "units"},
                    },
                    "expected_count": 1,
                    "count_new_only": True,
                    "timeout_s": 1.0,
                }
            ],
        }

    def save_order(self, customer: str, product: str, units: str) -> str:
        """The agent's write tool, as an app integration would write it."""

        self.posts += 1
        with httpx.Client(base_url=self.base, follow_redirects=False) as client:
            client.post(
                "/login", data={"username": DEMO_USER, "password": DEMO_PASSWORD}
            )
            reply = client.post(
                "/orders",
                data={"customer": customer, "product": product, "units": units},
            )
        return f"saved: {reply.headers.get('location', '')}"

    def fault(self, mode: str) -> None:
        httpx.get(f"{self.base}/_fault", params={"mode": mode})

    def orders(self) -> list[dict[str, str]]:
        return httpx.get(f"{self.base}/api/orders").json()["records"]


@pytest.fixture
def portal() -> Iterator[Portal]:
    base, stop = serve()
    try:
        yield Portal(base)
    finally:
        stop()
