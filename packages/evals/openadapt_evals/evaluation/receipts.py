"""Strict validation for command and setup HTTP receipts."""

from __future__ import annotations

import json
from typing import Any


class ReceiptValidationError(ValueError):
    """A remote endpoint did not prove that its requested operation succeeded."""


def _reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object and reject ambiguous duplicate fields."""
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise ReceiptValidationError(f"duplicate receipt field {key!r}")
        parsed[key] = value
    return parsed


def parse_strict_json_response(response: Any, *, context: str) -> Any:
    """Parse an HTTP JSON response without last-key-wins ambiguity.

    Real ``requests.Response`` objects retain the raw body in ``text``. Tests
    and compatible response adapters can expose only ``json()``; that fallback
    remains useful, but production responses always take the strict raw path.
    """
    raw = getattr(response, "text", None)
    try:
        if isinstance(raw, str):
            return json.loads(raw, object_pairs_hook=_reject_duplicate_fields)
        return response.json()
    except (TypeError, ValueError) as exc:
        raise ReceiptValidationError(
            f"{context} response was not unambiguous JSON: {exc}"
        ) from exc


def require_successful_receipt(
    payload: object,
    *,
    context: str,
    require_output: bool = False,
) -> dict[str, Any]:
    """Return one explicit successful receipt or raise.

    HTTP 200 only proves transport success. The JSON body must include at least
    one positive operation signal and no failure or uncertain-delivery signal.
    """
    if not isinstance(payload, dict):
        raise ReceiptValidationError(f"{context} receipt must be an object")

    positive = False
    for field in ("success", "ok"):
        if field in payload:
            if payload[field] is not True:
                raise ReceiptValidationError(
                    f"{context} receipt has {field}={payload[field]!r}"
                )
            positive = True

    if "failed" in payload:
        if payload["failed"] is not False:
            raise ReceiptValidationError(
                f"{context} receipt has failed={payload['failed']!r}"
            )
        # An explicit lack of failure is not proof that the operation
        # completed. Require a separate positive outcome signal.

    for field in ("error", "stderr"):
        if field not in payload:
            continue
        value = payload[field]
        if not isinstance(value, str) or value.strip():
            raise ReceiptValidationError(
                f"{context} receipt has {field}={value!r}"
            )

    if "returncode" in payload:
        returncode = payload["returncode"]
        if (
            isinstance(returncode, bool)
            or not isinstance(returncode, int)
            or returncode != 0
        ):
            raise ReceiptValidationError(
                f"{context} receipt has returncode={returncode!r}"
            )
        positive = True

    if "delivery_state" in payload:
        delivery_state = payload["delivery_state"]
        if delivery_state != "delivered":
            raise ReceiptValidationError(
                f"{context} receipt has delivery_state={delivery_state!r}"
            )
        # Delivery proves that the command was dispatched, not that it
        # completed successfully. Require a separate outcome signal below.

    if "status" in payload:
        status = payload["status"]
        if not isinstance(status, str) or status.lower() not in {
            "completed",
            "ok",
            "success",
            "succeeded",
        }:
            raise ReceiptValidationError(
                f"{context} receipt has status={status!r}"
            )
        positive = True

    if not positive:
        raise ReceiptValidationError(
            f"{context} receipt has no explicit success signal"
        )

    if require_output and not isinstance(payload.get("output"), str):
        raise ReceiptValidationError(
            f"{context} receipt has no string output"
        )
    return payload
