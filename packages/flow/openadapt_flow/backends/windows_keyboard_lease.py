"""Client for an authenticated native keyboard lease, with no silent fallback."""

from __future__ import annotations

import base64
import hashlib
import re
import struct
from typing import Any, Callable

from openadapt_flow.backend import (
    ActionDeliveryUncertain,
    KeyboardFrameChangedBeforeInput,
    StructuralResolutionRefused,
)
from openadapt_flow.backends.win_agent.keyboard_lease import (
    _context,
    _frame,
    _target,
    keyboard_operation,
)
from openadapt_flow.ir import ActionDeliveryReceipt


class NativeKeyboardLeaseClient:
    def __init__(
        self,
        post: Callable[[str, dict[str, Any]], Any],
        before_input: Callable[[], None],
    ) -> None:
        self._post, self._before_input = post, before_input
        self._observed: dict[str, Any] | None = None
        self._armed: dict[str, Any] | None = None

    def target_fingerprint(self) -> str | None:
        return self._armed["target"]["fingerprint"] if self._armed else None

    def cancel(self) -> None:
        self._observed = self._armed = None

    def frame(self) -> bytes:
        self.cancel()
        try:
            response = self._post("/keyboard/lease", {})
            data = response.json()
            if (
                response.status_code != 200
                or not isinstance(data, dict)
                or set(data)
                != {
                    "status",
                    "schema",
                    "lease_id",
                    "frame_sha256",
                    "frame_png",
                    "target",
                    "context",
                }
            ):
                raise ValueError("invalid lease envelope")
            if (
                data["status"] != "ok"
                or data["schema"] != "openadapt.native-keyboard-lease/v1"
                or not isinstance(data["lease_id"], str)
                or not re.fullmatch(r"[a-f0-9]{32}", data["lease_id"])
            ):
                raise ValueError("invalid lease identity")
            target = _target({"status": "ok", "target": data["target"]})
            _context({"status": "ok", **data["context"]})
            if (
                not isinstance(data["frame_png"], str)
                or len(data["frame_png"]) > 45 * 1024 * 1024
            ):
                raise ValueError("invalid lease frame")
            png = _frame(base64.b64decode(data["frame_png"], validate=True))
            if hashlib.sha256(png).hexdigest() != data["frame_sha256"]:
                raise ValueError("lease frame digest mismatch")
            self._observed = {**data, "target": target}
            return png
        except Exception as exc:
            raise StructuralResolutionRefused(
                "native keyboard lease observation unavailable"
            ) from exc

    def arm(self, x: int, y: int) -> None:
        if self._observed is None:
            self.frame()
        lease, self._observed = self._observed, None
        self._armed = None
        assert lease is not None
        left, top, right, bottom = lease["target"]["bounds"]
        if not (left <= x < right and top <= y < bottom):
            raise StructuralResolutionRefused(
                "native keyboard focus does not contain the resolved target"
            )
        self._armed = lease

    def deliver(
        self, payload: dict[str, Any], expected_frame_sha256: str
    ) -> ActionDeliveryReceipt:
        lease, self._armed = self._armed, None
        if lease is None:
            raise StructuralResolutionRefused(
                "native keyboard input has no armed lease"
            )
        operation = keyboard_operation(payload)
        self._before_input()
        try:
            response = self._post(
                "/keyboard/input",
                {
                    "lease_id": lease["lease_id"],
                    "expected_frame_sha256": expected_frame_sha256,
                    "input": payload,
                },
            )
            data = response.json()
        except Exception as exc:
            raise ActionDeliveryUncertain(
                operation=operation, native=False, cause_type=type(exc).__name__
            ) from exc
        if response.status_code == 409 and isinstance(data, dict):
            required = {
                "status",
                "reason",
                "lease_id",
                "operation",
                "no_keyboard_input_delivered",
            }
            valid = (
                set(data) in (required, required | {"frame_change"})
                and data["status"] == "refused"
                and data["lease_id"] == lease["lease_id"]
                and data["operation"] == operation
                and data["no_keyboard_input_delivered"] is True
                and isinstance(data["reason"], str)
                and data["reason"]
                in {
                    "expired",
                    "unverified_frame",
                    "stale_context",
                    "stale_focus",
                    "stale_frame",
                }
            )
            if valid:
                change = data.get("frame_change")
                if (
                    data["reason"] == "stale_frame"
                    and isinstance(change, dict)
                    and set(change)
                    == {"changed_pixel_count", "changed_bbox", "frame_size"}
                ):
                    try:
                        count = change["changed_pixel_count"]
                        if isinstance(count, bool) or not isinstance(count, int):
                            raise ValueError("invalid changed pixel count")
                        bbox, size = change["changed_bbox"], change["frame_size"]
                        if (
                            not isinstance(bbox, list)
                            or len(bbox) != 4
                            or not isinstance(size, list)
                            or len(size) != 2
                            or any(
                                isinstance(v, bool) or not isinstance(v, int)
                                for v in [*bbox, *size]
                            )
                            or size
                            != list(
                                struct.unpack(
                                    ">II", base64.b64decode(lease["frame_png"])[16:24]
                                )
                            )
                            or count > bbox[2] * bbox[3]
                        ):
                            raise ValueError("invalid native frame-change evidence")
                        refused = KeyboardFrameChangedBeforeInput(
                            operation=operation,
                            changed_pixel_count=count,
                            changed_bbox=tuple(bbox),
                            frame_size=tuple(size),
                            lease_sha256=hashlib.sha256(
                                lease["lease_id"].encode()
                            ).hexdigest(),
                            target_fingerprint=lease["target"]["fingerprint"],
                        )
                    except (KeyError, TypeError, ValueError):
                        pass
                    else:
                        raise refused
                raise StructuralResolutionRefused(
                    "native keyboard lease refused before input: " + data["reason"]
                )
        if (
            response.status_code == 200
            and isinstance(data, dict)
            and set(data) == {"status", "lease_id", "receipt"}
            and data["status"] == "ok"
            and data["lease_id"] == lease["lease_id"]
        ):
            try:
                receipt = ActionDeliveryReceipt.model_validate(data["receipt"])
                if (
                    receipt.operation != operation
                    or receipt.native
                    or receipt.outcome_verified
                    or receipt.target_fingerprint != lease["target"]["fingerprint"]
                ):
                    raise ValueError("mismatched actual keyboard receipt")
                return receipt
            except (TypeError, ValueError):
                pass
        raise ActionDeliveryUncertain(
            operation=operation,
            native=False,
            cause_type="KeyboardLeaseAcknowledgementInvalid",
        )
