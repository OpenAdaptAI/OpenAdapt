"""Local-only demonstration coach payload.

The control overlay may show this on the operator's machine. It must never be
written into evidence, report.json, a Seal, Cloud ingest, PostHog, or the
closed ``overlay://frame`` Types contract. Capture exclusion stays on.
"""

from __future__ import annotations

import re
from typing import Any

COACH_SCHEMA_VERSION = "openadapt.control-overlay-coach/v1"
HINT_MAX_CHARS = 80
_TURNS = frozenset({"your_turn", "wait", "auth", "feedback"})
_PAUSE_REASONS = frozenset(
    {"auth", "secret_field", "wrong_step", "skip", "done", "next"}
)
_OPERATOR_RESPONSES = frozenset({"continue", "wrong", "skip", "done", "secret_field"})
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_LONG_ID_RE = re.compile(r"\d{6,}")
_HMAC_RE = re.compile(r"^[a-f0-9]{64}$")

EMPTY_COACH: dict[str, Any] = {
    "schema_version": COACH_SCHEMA_VERSION,
    "hint": None,
    "turn": None,
    "pause_reason": None,
    "target": None,
    "operator_response": None,
}


def empty_coach() -> dict[str, Any]:
    """Return a fresh empty coach payload."""

    return dict(EMPTY_COACH)


def sanitize_hint(raw: object) -> str | None:
    """Return a short local hint, or None if the text looks identifying."""

    if raw is None:
        return None
    text = " ".join(str(raw).split())
    if not text:
        return None
    if len(text) > HINT_MAX_CHARS:
        text = text[:HINT_MAX_CHARS].rstrip()
    if _URL_RE.search(text) or "@" in text or _LONG_ID_RE.search(text):
        return None
    return text


def coach_holds_pause(payload: dict[str, Any]) -> bool:
    """True when the overlay must stay at a pause boundary for the operator."""

    turn = payload.get("turn")
    reason = payload.get("pause_reason")
    return turn in {"auth", "feedback"} or reason in {
        "auth",
        "secret_field",
        "wrong_step",
        "skip",
        "done",
    }


def _normalized_rect(raw: object) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    try:
        x = float(raw["x"])
        y = float(raw["y"])
        width = float(raw["width"])
        height = float(raw["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    if min(x, y) < 0 or max(x + width, y + height) > 1.0001:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def _binding(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind == "observation_hmac_sha256":
        digest = raw.get("observation_hmac_sha256")
        if not isinstance(digest, str) or _HMAC_RE.fullmatch(digest) is None:
            return None
        return {
            "kind": "observation_hmac_sha256",
            "observation_hmac_sha256": digest,
        }
    if kind == "media_frame":
        digest = raw.get("media_sha256")
        index = raw.get("frame_index")
        if not isinstance(digest, str) or _HMAC_RE.fullmatch(digest) is None:
            return None
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            return None
        return {
            "kind": "media_frame",
            "media_sha256": digest,
            "frame_index": index,
        }
    return None


def bind_coach_target(raw: object) -> dict[str, Any] | None:
    """Return a ring rect only when an exact observation binding is present.

    A rect without a binding is omitted. Heuristic reconstruction is forbidden.
    """

    if not isinstance(raw, dict):
        return None
    if raw.get("coordinate_space") != "top_level_viewport_normalized":
        return None
    binding = _binding(raw.get("binding"))
    rect = _normalized_rect(raw.get("rect"))
    if binding is None or rect is None:
        return None
    return {
        "coordinate_space": "top_level_viewport_normalized",
        "rect": rect,
        "binding": binding,
    }


def apply_coach_update(current: dict[str, Any], params: dict[str, Any] | None) -> dict[str, Any]:
    """Merge a set_coach params dict into the in-memory coach payload."""

    incoming = dict(params or {})
    if incoming.get("clear") is True:
        return empty_coach()
    next_state = dict(current) if current else empty_coach()
    next_state["schema_version"] = COACH_SCHEMA_VERSION
    # Drop anything a caller might have stuffed in. Pack URLs, screenshots,
    # and identities belong nowhere on this channel.
    next_state.pop("pack_url", None)
    next_state.pop("screenshot", None)
    next_state.pop("image", None)

    if "hint" in incoming:
        next_state["hint"] = sanitize_hint(incoming.get("hint"))
    if "turn" in incoming:
        turn = incoming.get("turn")
        next_state["turn"] = turn if turn in _TURNS else None
    if "pause_reason" in incoming:
        reason = incoming.get("pause_reason")
        next_state["pause_reason"] = reason if reason in _PAUSE_REASONS else None
    if "target" in incoming:
        next_state["target"] = bind_coach_target(incoming.get("target"))
    if "operator_response" in incoming:
        response = incoming.get("operator_response")
        if response in _OPERATOR_RESPONSES:
            next_state["operator_response"] = response
            if response == "continue":
                next_state["pause_reason"] = None
                if next_state.get("turn") in {"auth", "feedback"}:
                    next_state["turn"] = "your_turn"
        elif incoming.get("operator_response") is None:
            next_state["operator_response"] = None
    return {
        "schema_version": COACH_SCHEMA_VERSION,
        "hint": next_state.get("hint"),
        "turn": next_state.get("turn"),
        "pause_reason": next_state.get("pause_reason"),
        "target": next_state.get("target"),
        "operator_response": next_state.get("operator_response"),
    }
