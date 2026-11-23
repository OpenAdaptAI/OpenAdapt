"""Independent WooCommerce REST v3 order & refund read-back oracle."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Mapping


def _headers(consumer_key: str | None, consumer_secret: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if consumer_key and consumer_secret:
        token = base64.b64encode(
            f"{consumer_key}:{consumer_secret}".encode("utf-8")
        ).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    return headers


def verify_write(
    base_url: str,
    order_id: int,
    expected_fields: Mapping[str, Any],
    *,
    expected_refund_amount: str | None = None,
    consumer_key: str | None = None,
    consumer_secret: str | None = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Read back a WooCommerce order (and optional refund) via REST v3; fail closed."""
    root = base_url.rstrip("/")
    hdrs = _headers(consumer_key, consumer_secret)
    try:
        req = urllib.request.Request(
            f"{root}/wp-json/wc/v3/orders/{order_id}", method="GET", headers=hdrs
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            order = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {
            "verdict": "HALT",
            "channel": "woocommerce_rest_v3",
            "reason": f"readback_error: {exc}",
            "mismatches": ["transport_or_parse_error"],
            "observed": None,
        }

    if not isinstance(order, Mapping) or order.get("id") != order_id:
        return {
            "verdict": "HALT",
            "channel": "woocommerce_rest_v3",
            "reason": "missing_or_wrong_order",
            "mismatches": [
                f"expected order id {order_id}, observed {order.get('id') if isinstance(order, Mapping) else None!r}"
            ],
            "observed": order,
        }

    mismatches: list[str] = []
    for k, exp in expected_fields.items():
        actual = order.get(k)
        if actual != exp:
            mismatches.append(f"{k}: expected {exp!r}, observed {actual!r}")

    observed: dict[str, Any] = {"order": dict(order)}
    if expected_refund_amount is not None:
        try:
            r_req = urllib.request.Request(
                f"{root}/wp-json/wc/v3/orders/{order_id}/refunds",
                method="GET",
                headers=hdrs,
            )
            with urllib.request.urlopen(r_req, timeout=timeout_s) as r_resp:
                refunds = json.loads(r_resp.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            return {
                "verdict": "HALT",
                "channel": "woocommerce_rest_v3",
                "reason": f"refund_readback_error: {exc}",
                "mismatches": ["refund_transport_error"],
                "observed": observed,
            }
        observed["refunds"] = refunds
        if not isinstance(refunds, list) or len(refunds) != 1:
            mismatches.append(
                f"refunds: expected 1 refund, observed {len(refunds) if isinstance(refunds, list) else 'invalid'}"
            )
        elif str(refunds[0].get("amount")) != expected_refund_amount:
            mismatches.append(
                f"refund.amount: expected {expected_refund_amount!r}, observed {refunds[0].get('amount')!r}"
            )

    return {
        "verdict": "VERIFIED" if not mismatches else "HALT",
        "channel": "woocommerce_rest_v3",
        "reason": "ok" if not mismatches else "order_or_refund_mismatch",
        "mismatches": mismatches,
        "observed": observed,
    }
