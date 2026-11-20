"""One-shot keyboard authority: only observed target/context/frame may receive input."""

from __future__ import annotations

import struct
from copy import deepcopy

import pytest

from openadapt_flow.backends.win_agent.keyboard_lease import (
    KeyboardLeaseController,
    KeyboardLeaseError,
    KeyboardLeaseRefused,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 640, 480)


class Surface:
    def __init__(self):
        self.target = {
            "fingerprint": "a" * 64,
            "point": [20, 20],
            "bounds": [10, 10, 100, 40],
            "role": "textbox",
        }
        self.context = {
            "status": "ok",
            "application": "test-runtime",
            "application_version": "sha256:" + "a" * 64,
            "session": "b" * 64,
            "environment": "c" * 64,
            "workflow_state": None,
        }
        self.frame, self.now, self.calls = PNG, 0.0, []
        self.error = None
        self.controller = KeyboardLeaseController(
            self.uia,
            lambda: self.frame,
            lambda: dict(self.context),
            self.deliver,
            clock=lambda: self.now,
        )

    def uia(self, operation, payload):
        assert operation == "focused-target" and payload == {}
        return {"status": "ok", "target": deepcopy(self.target)}

    def deliver(self, payload):
        self.calls.append(payload)
        if self.error:
            raise self.error
        from openadapt_flow.backends.win_agent.server import _delivery_receipt

        return _delivery_receipt(
            "physical_type_text"
            if payload["action"] == "type_text"
            else "physical_press",
            native=False,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "type_text", "text": "example"},
        {"action": "press", "keys": ["enter"]},
        {"action": "press", "keys": ["ctrl", "s"]},
    ],
)
def test_actual_acknowledgement_binds_exact_target_and_consumes_lease(payload):
    surface = Surface()
    lease = surface.controller.issue()
    assert not surface.calls
    delivered = surface.controller.deliver(
        lease["lease_id"], lease["frame_sha256"], payload
    )
    assert delivered["receipt"]["target_fingerprint"] == lease["target"]["fingerprint"]
    assert surface.calls == [payload]
    with pytest.raises(KeyboardLeaseError, match="not_owned"):
        surface.controller.deliver(lease["lease_id"], lease["frame_sha256"], payload)
    assert surface.calls == [payload]


@pytest.mark.parametrize(
    "change", ["frame", "focus", "context", "expired", "unverified"]
)
def test_every_changed_binding_returns_zero_keyboard_input_only_before_delivery(change):
    surface = Surface()
    lease = surface.controller.issue()
    expected = lease["frame_sha256"]
    reason = {
        "frame": "stale_frame",
        "focus": "stale_focus",
        "context": "stale_context",
        "expired": "expired",
        "unverified": "unverified_frame",
    }[change]
    if change == "frame":
        surface.frame += b"changed"
    if change == "focus":
        surface.target["fingerprint"] = "d" * 64
    if change == "context":
        surface.context["session"] = "d" * 64
    if change == "expired":
        surface.now = 16
    if change == "unverified":
        expected = "d" * 64
    with pytest.raises(KeyboardLeaseRefused) as error:
        surface.controller.deliver(
            lease["lease_id"], expected, {"action": "type_text", "text": "example"}
        )
    assert error.value.reason == reason
    assert error.value.payload()["no_keyboard_input_delivered"] is True
    assert not surface.calls
    with pytest.raises(KeyboardLeaseError, match="not_owned"):
        surface.controller.deliver(
            lease["lease_id"],
            lease["frame_sha256"],
            {"action": "press", "keys": ["enter"]},
        )


def test_later_observation_invalidates_prior_lease_and_callback_failure_is_uncertain():
    surface = Surface()
    first = surface.controller.issue()
    second = surface.controller.issue()
    with pytest.raises(KeyboardLeaseError, match="not_owned"):
        surface.controller.deliver(
            first["lease_id"],
            first["frame_sha256"],
            {"action": "press", "keys": ["enter"]},
        )
    # A rejected request consumed the current lease too; no replay after an unknown token.
    with pytest.raises(KeyboardLeaseError, match="not_owned"):
        surface.controller.deliver(
            second["lease_id"],
            second["frame_sha256"],
            {"action": "press", "keys": ["enter"]},
        )
    lease = surface.controller.issue()
    surface.error = OSError("delivery may have begun")
    with pytest.raises(OSError):
        surface.controller.deliver(
            lease["lease_id"],
            lease["frame_sha256"],
            {"action": "press", "keys": ["enter"]},
        )
    assert len(surface.calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "click"},
        {"action": "type_text", "text": "x" * 65537},
        {"action": "press", "keys": [True]},
        {"action": "type_text", "text": "x", "interval_s": True},
    ],
)
def test_malformed_keyboard_data_never_reaches_input(bad):
    surface = Surface()
    lease = surface.controller.issue()
    with pytest.raises(KeyboardLeaseError):
        surface.controller.deliver(lease["lease_id"], lease["frame_sha256"], bad)
    assert not surface.calls


def test_deadline_during_actual_delivery_is_uncertain_not_zero_input():
    surface = Surface()
    def deliver(payload):
        surface.calls.append(payload)
        surface.now = 20.0
        surface.controller.require_current_target()
        raise AssertionError("expired delivery cannot acknowledge")
    surface.controller._deliver = deliver
    lease = surface.controller.issue()
    with pytest.raises(RuntimeError) as caught:
        surface.controller.deliver(lease["lease_id"], lease["frame_sha256"], {"action": "press", "keys": ["enter"]})
    assert not isinstance(caught.value, KeyboardLeaseRefused)
    assert len(surface.calls) == 1


def test_changed_frame_geometry_is_measured_from_actual_pixels():
    from io import BytesIO

    from PIL import Image

    from openadapt_flow.backends.win_agent.keyboard_lease import frame_change

    def png(image):
        stream = BytesIO()
        image.save(stream, format="PNG")
        return stream.getvalue()
    before = Image.new("RGB", (10, 10), "white")
    after = before.copy()
    after.putpixel((2, 3), (255, 255, 254))
    assert frame_change(png(before), png(after)) == {
        "changed_pixel_count": 1, "changed_bbox": [2, 3, 1, 1], "frame_size": [10, 10]
    }
    assert frame_change(png(before), png(before)) is None
