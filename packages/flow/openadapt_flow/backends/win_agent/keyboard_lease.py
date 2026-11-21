"""One-shot native keyboard leases over real focus, context and exact frames.

This module is also copied next to the standalone in-guest agent. It imports
only the standard library and accepts the agent's ordinary observation/input
functions. A refusal receipt is produced only before its input callback runs.
"""

from __future__ import annotations

import base64
import hashlib
import re
import struct
import time
import uuid
from typing import Any, Callable

_HEX = re.compile(r"^[a-f0-9]{64}$")
_TOKEN = re.compile(r"^[a-f0-9]{32}$")
_PNG = b"\x89PNG\r\n\x1a\n"


class KeyboardLeaseError(RuntimeError):
    """A bounded observation failure, with no delivery assertion."""


class KeyboardDeliveryUncertain(RuntimeError):
    """Bounded diagnostics after the keyboard delivery boundary; never retryable."""

    def __init__(self, reason: str, checks: int, elapsed_ms: int) -> None:
        self.reason, self.checks, self.elapsed_ms = reason, checks, elapsed_ms
        super().__init__("native keyboard delivery became uncertain")

    def payload(self) -> dict[str, Any]:
        return {
            "status": "error",
            "code": "keyboard_delivery_uncertain",
            "reason": self.reason,
            "guard_checks": self.checks,
            "elapsed_ms": self.elapsed_ms,
        }


class KeyboardLeaseRefused(KeyboardLeaseError):
    """A consumed known lease refused before its keyboard input callback."""

    def __init__(
        self,
        reason: str,
        lease_id: str,
        operation: str,
        frame_change: dict[str, Any] | None = None,
    ) -> None:
        self.reason, self.lease_id, self.operation = reason, lease_id, operation
        self.frame_change = frame_change
        super().__init__(reason)

    def payload(self) -> dict[str, Any]:
        result = {
            "status": "refused",
            "reason": self.reason,
            "lease_id": self.lease_id,
            "operation": self.operation,
            "no_keyboard_input_delivered": True,
        }
        if self.frame_change is not None:
            result["frame_change"] = self.frame_change
        return result


def _target(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("status") != "ok":
        raise KeyboardLeaseError("focused_target_unavailable")
    target = value.get("target")
    if (
        not isinstance(target, dict)
        or not isinstance(target.get("fingerprint"), str)
        or not _HEX.fullmatch(target["fingerprint"])
    ):
        raise KeyboardLeaseError("focused_target_unavailable")
    point, bounds = target.get("point"), target.get("bounds")
    if (
        not isinstance(point, list)
        or len(point) != 2
        or not isinstance(bounds, list)
        or len(bounds) != 4
        or any(
            isinstance(v, bool) or not isinstance(v, int) or abs(v) > 32768
            for v in [*point, *bounds]
        )
        or not (bounds[0] <= point[0] < bounds[2] and bounds[1] <= point[1] < bounds[3])
    ):
        raise KeyboardLeaseError("focused_target_geometry_unavailable")
    return target


def _context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("status") != "ok":
        raise KeyboardLeaseError("context_unavailable")
    result = {
        name: value.get(name)
        for name in (
            "application",
            "application_version",
            "session",
            "environment",
            "workflow_state",
        )
    }
    if (
        not isinstance(result["application"], str)
        or not result["application"]
        or any(
            not isinstance(result[name], str) or not result[name]
            for name in ("application_version", "session", "environment")
        )
    ):
        raise KeyboardLeaseError("context_unavailable")
    return result


def _frame(value: Any) -> bytes:
    if (
        not isinstance(value, bytes)
        or len(value) < 24
        or len(value) > 32 * 1024 * 1024
        or not value.startswith(_PNG)
    ):
        raise KeyboardLeaseError("frame_unavailable")
    width, height = struct.unpack(">II", value[16:24])
    if not (0 < width <= 32768 and 0 < height <= 32768 and width * height <= 16777216):
        raise KeyboardLeaseError("frame_geometry_unavailable")
    return value


def keyboard_operation(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise KeyboardLeaseError("invalid_keyboard_input")
    action = payload.get("action")
    if action == "type_text":
        if (
            set(payload) - {"action", "text", "interval_s"}
            or not isinstance(payload.get("text"), str)
            or len(payload["text"]) > 65536
        ):
            raise KeyboardLeaseError("invalid_keyboard_input")
        interval = payload.get("interval_s", 0.05)
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not 0 <= interval <= 1
        ):
            raise KeyboardLeaseError("invalid_keyboard_input")
        return "physical_type_text"
    if action == "press":
        keys = payload.get("keys")
        if (
            set(payload) != {"action", "keys"}
            or not isinstance(keys, list)
            or not 1 <= len(keys) <= 4
            or any(not isinstance(key, str) or not 1 <= len(key) <= 32 for key in keys)
        ):
            raise KeyboardLeaseError("invalid_keyboard_input")
        return "physical_press"
    raise KeyboardLeaseError("invalid_keyboard_input")


def frame_change(before: bytes, after: bytes) -> dict[str, Any] | None:
    from io import BytesIO

    from PIL import Image, ImageChops

    try:
        with Image.open(BytesIO(before)) as a, Image.open(BytesIO(after)) as b:
            if a.size != b.size:
                return None
            delta = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
            bbox = delta.getbbox()
            if bbox is None:
                return None
            red, green, blue = delta.split()
            nonzero = ImageChops.lighter(ImageChops.lighter(red, green), blue)
            count = a.width * a.height - nonzero.histogram()[0]
            return {
                "changed_pixel_count": count,
                "changed_bbox": [
                    bbox[0],
                    bbox[1],
                    bbox[2] - bbox[0],
                    bbox[3] - bbox[1],
                ],
                "frame_size": list(a.size),
            }
    except (OSError, ValueError):
        return None


class KeyboardLeaseController:
    """A bounded lease names the actual focused element and captured desktop."""

    def __init__(
        self,
        uia: Callable[..., dict[str, Any]],
        capture: Callable[[], bytes],
        context: Callable[[], dict[str, Any]],
        deliver: Callable[..., dict[str, Any]],
        *,
        clock: Callable[[], float] = time.monotonic,
        lifetime_s: float = 15.0,
        prepare: Callable[[dict[str, Any]], None] | None = None,
        current_focus: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._uia, self._capture, self._context, self._deliver = (
            uia,
            capture,
            context,
            deliver,
        )
        self._clock, self._lifetime_s = clock, lifetime_s
        self._prepare = prepare
        self._current_focus = current_focus or (lambda: self._uia("focused-target", {}))
        self._guard_checks, self._delivery_started = 0, 0.0
        self._delivering: tuple[dict[str, Any], dict[str, Any], float] | None = None
        self._pending: (
            tuple[str, float, dict[str, Any], dict[str, Any], bytes] | None
        ) = None

    def issue(self) -> dict[str, Any]:
        # The agent is single-threaded. A new observation invalidates its prior
        # lease, and no input callback is invoked while observing.
        self._pending = None
        before = _target(self._uia("focused-target", {}))
        context = _context(self._context())
        png = _frame(self._capture())
        after = _target(self._uia("focused-target", {}))
        if before["fingerprint"] != after["fingerprint"] or context != _context(
            self._context()
        ):
            raise KeyboardLeaseError("keyboard_observation_changed")
        token = uuid.uuid4().hex
        digest = hashlib.sha256(png).hexdigest()
        self._pending = (
            token,
            self._clock() + self._lifetime_s,
            after,
            context,
            png,
        )
        return {
            "status": "ok",
            "schema": "openadapt.native-keyboard-lease/v1",
            "lease_id": token,
            "frame_sha256": digest,
            "frame_png": base64.b64encode(png).decode("ascii"),
            "target": after,
            "context": context,
        }

    def deliver(
        self, lease_id: str, expected_frame_sha256: str, payload: Any
    ) -> dict[str, Any]:
        pending, self._pending = self._pending, None
        operation = keyboard_operation(payload)
        if (
            not isinstance(lease_id, str)
            or not _TOKEN.fullmatch(lease_id)
            or pending is None
            or pending[0] != lease_id
        ):
            raise KeyboardLeaseError("keyboard_lease_not_owned")
        _, deadline, target, context, before_png = pending
        frame_sha = hashlib.sha256(before_png).hexdigest()

        def refuse(reason: str) -> None:
            raise KeyboardLeaseRefused(reason, lease_id, operation)

        if self._clock() > deadline:
            refuse("expired")
        if expected_frame_sha256 != frame_sha:
            refuse("unverified_frame")
        if self._prepare is not None:
            self._prepare(payload)
        if context != _context(self._context()):
            refuse("stale_context")
        current = _target(self._uia("focused-target", {}))
        if current["fingerprint"] != target["fingerprint"]:
            refuse("stale_focus")
        current_png = _frame(self._capture())
        if hashlib.sha256(current_png).hexdigest() != frame_sha:
            raise KeyboardLeaseRefused(
                "stale_frame",
                lease_id,
                operation,
                frame_change(before_png, current_png),
            )
        # Any exception after this boundary is uncertain. It must never be
        # wrapped in the zero-keyboard-input refusal contract.
        if self._clock() > deadline:
            refuse("expired")
        self._delivering = (target, context, deadline)
        self._guard_checks, self._delivery_started = 0, self._clock()
        try:
            result = self._deliver(payload)
        finally:
            self._delivering = None
        receipt = result if isinstance(result, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("status") != "delivered"
            or receipt.get("operation") != operation
            or receipt.get("native") is not False
            or not isinstance(receipt.get("receipt_id"), str)
            or not isinstance(receipt.get("delivered_at"), str)
            or receipt.get("outcome_verified") is not False
        ):
            raise RuntimeError(
                "keyboard delivery receipt unavailable after delivery boundary"
            )
        return {
            "status": "ok",
            "lease_id": lease_id,
            "receipt": {**receipt, "target_fingerprint": target["fingerprint"]},
        }

    def require_current_target(self) -> None:
        """After delivery may begin, any focus/context change is uncertain."""
        if self._delivering is None:
            raise RuntimeError("keyboard delivery has no active focus lease")
        target, context, deadline = self._delivering
        self._guard_checks += 1

        def uncertain(reason: str) -> None:
            raise KeyboardDeliveryUncertain(
                reason,
                self._guard_checks,
                max(
                    0,
                    min(
                        3_600_000, int((self._clock() - self._delivery_started) * 1000)
                    ),
                ),
            )

        if self._clock() > deadline:
            uncertain("deadline_during_input")
        try:
            current = _target(self._current_focus())
        except KeyboardLeaseError:
            uncertain("focused_target_unavailable_during_input")
            return
        if current["fingerprint"] != target["fingerprint"]:
            uncertain("focused_target_changed_during_input")
        try:
            current_context = _context(self._context())
        except KeyboardLeaseError:
            uncertain("context_unavailable_during_input")
            return
        if current_context != context:
            uncertain("context_changed_during_input")
        if self._clock() > deadline:
            uncertain("deadline_during_input")
