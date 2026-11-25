"""Strict native value observations; previews are never effect evidence."""

from __future__ import annotations

from typing import Any, Mapping

from openadapt_flow.approval_mode.window_fields import valid_value_observation

from .native_value import valid_value_proof as valid_value_proof

SCOPE_KEYS = ("automation_id", "role", "name", "window_name")
READ_KEYS = frozenset(
    {
        "status",
        "match",
        "candidate_count",
        "point",
        "text",
        "complete",
        "value_proof",
        "value_identity",
        "read_scope",
        "preview_text",
        "reason",
    }
)
ROW_KEYS = frozenset({"rows", "rows_truncated", "rows_complete", "preview_rows"})


def locator_scope(locator: Mapping[str, Any]) -> dict[str, Any]:
    return {key: locator.get(key) for key in SCOPE_KEYS}


def valid_structural_response(
    payload: object, scope: Mapping[str, Any], *, rows: bool = False
) -> bool:
    try:
        if not isinstance(payload, dict) or set(payload) != READ_KEYS | (
            ROW_KEYS if rows else set()
        ):
            return False
        if payload["status"] != "ok" or payload["read_scope"] != locator_scope(scope):
            return False
        match, count, point = (
            payload["match"],
            payload["candidate_count"],
            payload["point"],
        )
        if (
            match not in {"unique", "ambiguous", "not_found"}
            or type(count) is not int
            or count < 0
        ):
            return False
        if match == "unique":
            if (
                count != 1
                or not isinstance(point, list)
                or len(point) != 2
                or any(type(v) is not int for v in point)
            ):
                return False
        elif point is not None or (match == "not_found" and count != 0):
            return False
        preview = payload["preview_text"]
        if preview is not None and (
            not isinstance(preview, str) or len(preview) > 4096
        ):
            return False
        if type(payload["complete"]) is not bool:
            return False
        if payload["complete"]:
            if (
                match != "unique"
                or payload["reason"] is not None
                or not valid_value_observation(
                    payload["text"], payload["value_proof"], payload["value_identity"]
                )
            ):
                return False
        elif (
            payload["text"] is not None
            or payload["value_proof"] is not None
            or payload["value_identity"] is not None
            or not isinstance(payload["reason"], str)
            or not 1 <= len(payload["reason"]) <= 128
        ):
            return False
        if rows and (
            payload["rows"] is not None
            or payload["rows_truncated"] is not True
            or payload["rows_complete"] is not False
            or not isinstance(payload["preview_rows"], list)
        ):
            # No native row-inventory/composite completeness contract is
            # qualified yet. In particular, empty previews never prove zero.
            return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def valid_structural_value(payload: object, scope: Mapping[str, Any]) -> bool:
    return (
        isinstance(payload, dict)
        and valid_structural_response(payload, scope)
        and payload["complete"] is True
    )
