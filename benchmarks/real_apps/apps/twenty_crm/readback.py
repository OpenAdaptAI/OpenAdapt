"""Independent GraphQL read-back oracle for Twenty CRM."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from benchmarks.real_apps.harness import ReadbackResult


def readback_twenty_opportunity(
    graphql_url: str,
    *,
    opportunity_id: str,
    expected_name: str,
    expected_stage: str,
    expected_amount_micros: int,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10.0,
) -> ReadbackResult:
    """Query Twenty CRM's GraphQL endpoint out-of-band to verify persistence."""
    query_payload = {
        "operationName": "GetOpportunity",
        "query": (
            "query GetOpportunity($id: ID!) { "
            "opportunity(filter: { id: { eq: $id } }) { id name stage amountMicros } }"
        ),
        "variables": {"id": opportunity_id},
    }
    req = urllib.request.Request(
        graphql_url,
        data=json.dumps(query_payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        return ReadbackResult(
            confirmed=False,
            oracle="twenty-graphql",
            record_id=opportunity_id,
            reason=f"GraphQL read-back failed: {exc}",
        )

    if body.get("errors"):
        return ReadbackResult(
            confirmed=False,
            oracle="twenty-graphql",
            record_id=opportunity_id,
            reason=f"GraphQL read-back errors: {body['errors']}",
        )

    opp = (body.get("data") or {}).get("opportunity")
    if not isinstance(opp, dict):
        return ReadbackResult(
            confirmed=False,
            oracle="twenty-graphql",
            record_id=opportunity_id,
            reason=f"Opportunity {opportunity_id!r} not found in GraphQL store",
        )

    matched = (
        str(opp.get("id")) == opportunity_id
        and str(opp.get("name")) == expected_name
        and str(opp.get("stage")) == expected_stage
        and int(opp.get("amountMicros", -1)) == expected_amount_micros
    )
    return ReadbackResult(
        confirmed=matched,
        oracle="twenty-graphql",
        record_id=opportunity_id,
        observed_fields=dict(opp),
        reason="" if matched else "Opportunity fields did not match expected write",
    )
