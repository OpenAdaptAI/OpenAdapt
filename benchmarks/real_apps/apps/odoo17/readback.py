"""Genuine Odoo 17 JSON-RPC independent read-back oracle."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from benchmarks.real_apps.harness import ReadbackResult


def readback_odoo_vendor_bill(
    odoo_base_url: str,
    *,
    db: str,
    uid: int,
    credential: str,
    expected_ref: str,
    expected_amount: float,
    expected_state: str = "posted",
    timeout_s: float = 10.0,
) -> ReadbackResult:
    """Query Odoo's ``/jsonrpc`` endpoint (``account.move`` ``search_read``) out-of-band."""
    url = f"{odoo_base_url.rstrip('/')}/jsonrpc"
    rpc_payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "id": 42,
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [
                db,
                uid,
                credential,
                "account.move",
                "search_read",
                [[["ref", "=", expected_ref], ["move_type", "=", "in_invoice"]]],
                {"fields": ["id", "ref", "partner_name", "amount_total", "state"], "limit": 5},
            ],
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(rpc_payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        return ReadbackResult(
            confirmed=False,
            oracle="odoo17-jsonrpc",
            record_id=expected_ref,
            reason=f"JSON-RPC read-back failed: {exc}",
        )

    if "error" in body:
        return ReadbackResult(
            confirmed=False,
            oracle="odoo17-jsonrpc",
            record_id=expected_ref,
            reason=f"Odoo JSON-RPC error: {body['error']}",
        )

    rows = body.get("result")
    if not isinstance(rows, list) or not rows:
        return ReadbackResult(
            confirmed=False,
            oracle="odoo17-jsonrpc",
            record_id=expected_ref,
            reason=f"No account.move record found with ref={expected_ref!r}",
        )

    row = rows[0]
    observed_amount = float(row.get("amount_total", -1.0))
    observed_state = str(row.get("state", ""))
    matched = (
        str(row.get("ref")) == expected_ref
        and abs(observed_amount - expected_amount) < 1e-6
        and observed_state == expected_state
    )
    return ReadbackResult(
        confirmed=matched,
        oracle="odoo17-jsonrpc",
        record_id=str(row.get("id", expected_ref)),
        observed_fields={
            "ref": row.get("ref"),
            "amount_total": observed_amount,
            "state": observed_state,
        },
        reason="" if matched else "Vendor bill amount or state mismatch in Odoo JSON-RPC",
    )
