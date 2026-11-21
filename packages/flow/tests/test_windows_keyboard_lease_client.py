"""Untrusted lease acknowledgements cannot create retry or delivery authority."""

from __future__ import annotations

import copy
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from openadapt_flow.backend import (
    ActionDeliveryUncertain,
    KeyboardFrameChangedBeforeInput,
    StructuralResolutionRefused,
)
from openadapt_flow.backends.win_agent.keyboard_lease import (
    KeyboardLeaseController,
    KeyboardLeaseRefused,
)
from openadapt_flow.backends.win_agent.server import _delivery_receipt
from openadapt_flow.backends.windows_keyboard_lease import NativeKeyboardLeaseClient


class _Peer:
    """Real controller + receipt factory, with a bounded synthetic desktop."""

    def __init__(self):
        stream = BytesIO()
        Image.new("RGB", (64, 48), "white").save(stream, format="PNG")
        self.png = stream.getvalue()
        self.target = {
            "fingerprint": "a" * 64,
            "point": [20, 20],
            "bounds": [10, 10, 50, 40],
        }
        self.context = {
            "status": "ok",
            "application": "synthetic-runtime",
            "application_version": "sha256:" + "b" * 64,
            "session": "c" * 64,
            "environment": "d" * 64,
            "workflow_state": None,
        }
        self.sent, self.delivered = [], []
        self.corrupt = lambda path, value: value
        self.controller = KeyboardLeaseController(
            lambda operation, data: {
                "status": "ok",
                "target": copy.deepcopy(self.target),
            },
            lambda: self.png,
            lambda: dict(self.context),
            self.input,
        )
        self.client = NativeKeyboardLeaseClient(self.post, lambda: None)

    def input(self, payload):
        self.delivered.append(copy.deepcopy(payload))
        return _delivery_receipt(
            "physical_type_text"
            if payload["action"] == "type_text"
            else "physical_press",
            native=False,
        )

    def post(self, path, data):
        self.sent.append((path, copy.deepcopy(data)))
        try:
            result = (
                self.controller.issue()
                if path == "/keyboard/lease"
                else self.controller.deliver(
                    data["lease_id"], data["expected_frame_sha256"], data["input"]
                )
            )
            status = 200
        except KeyboardLeaseRefused as exc:
            result, status = exc.payload(), 409
        result = self.corrupt(path, result)
        return SimpleNamespace(status_code=status, json=lambda: result)

    def arm(self):
        import hashlib

        frame = self.client.frame()
        self.client.arm(20, 20)
        return hashlib.sha256(frame).hexdigest()


INPUTS = [
    {"action": "type_text", "text": "synthetic"},
    {"action": "press", "keys": ["enter"]},
    {"action": "press", "keys": ["ctrl", "x"]},
]


@pytest.mark.parametrize("payload", INPUTS)
def test_real_acknowledgement_is_single_use_and_bound_to_actual_target(payload):
    peer = _Peer()
    digest = peer.arm()
    receipt = peer.client.deliver(payload, digest)
    assert receipt.target_fingerprint == peer.target["fingerprint"]
    assert receipt.status == "delivered" and receipt.outcome_verified is False
    assert peer.delivered == [payload]
    with pytest.raises(StructuralResolutionRefused, match="no armed lease"):
        peer.client.deliver(payload, digest)
    assert peer.delivered == [payload] and len(peer.sent) == 2


@pytest.mark.parametrize(
    "change", ["extra", "digest", "context", "target", "png", "geometry"]
)
def test_malformed_observation_never_arms_input(change):
    peer = _Peer()

    def corrupt(path, result):
        assert path == "/keyboard/lease"
        if change == "extra":
            result["untrusted"] = True
        elif change == "digest":
            result["frame_sha256"] = "e" * 64
        elif change == "context":
            result["context"]["application_version"] = None
        elif change == "target":
            result["target"]["fingerprint"] = "untrusted"
        elif change == "png":
            result["frame_png"] = "not base64"
        else:
            result["target"]["point"] = [True, 20]
        return result

    peer.corrupt = corrupt
    with pytest.raises(StructuralResolutionRefused, match="observation unavailable"):
        peer.client.frame()
    with pytest.raises(StructuralResolutionRefused, match="no armed lease"):
        peer.client.deliver(INPUTS[0], "a" * 64)
    assert not peer.delivered and len(peer.sent) == 1


@pytest.mark.parametrize("payload", INPUTS)
@pytest.mark.parametrize(
    "change",
    ["target", "operation", "native", "outcome", "lease", "extra", "status", "missing"],
)
def test_bad_acknowledgement_is_uncertain_after_exactly_one_actual_delivery(
    payload, change
):
    peer = _Peer()
    digest = peer.arm()

    def corrupt(path, result):
        assert path == "/keyboard/input"
        if change == "target":
            result["receipt"]["target_fingerprint"] = "e" * 64
        elif change == "operation":
            result["receipt"]["operation"] = "physical_click"
        elif change == "native":
            result["receipt"]["native"] = True
        elif change == "outcome":
            result["receipt"]["outcome_verified"] = True
        elif change == "lease":
            result["lease_id"] = "e" * 32
        elif change == "extra":
            result["receipt"]["delivery_attested"] = True
        elif change == "status":
            result["receipt"]["status"] = "refused"
        else:
            del result["receipt"]["receipt_id"]
        return result

    peer.corrupt = corrupt
    with pytest.raises(ActionDeliveryUncertain):
        peer.client.deliver(payload, digest)
    assert peer.delivered == [payload]
    with pytest.raises(StructuralResolutionRefused, match="no armed lease"):
        peer.client.deliver(payload, digest)
    assert peer.delivered == [payload] and len(peer.sent) == 2


@pytest.mark.parametrize("payload", INPUTS)
def test_lost_actual_acknowledgement_never_repeats_or_rearms_input(payload):
    peer = _Peer()
    digest = peer.arm()

    def lose_response(path, result):
        assert path == "/keyboard/input"
        raise ConnectionError("synthetic response lost after actual input")

    peer.corrupt = lose_response
    with pytest.raises(ActionDeliveryUncertain):
        peer.client.deliver(payload, digest)
    assert peer.delivered == [payload]
    with pytest.raises(StructuralResolutionRefused):
        peer.client.deliver(payload, digest)
    assert peer.delivered == [payload] and len(peer.sent) == 2


@pytest.mark.parametrize("payload", INPUTS)
def test_real_pixel_change_is_zero_input_and_consumes_the_lease(payload):
    peer = _Peer()
    digest = peer.arm()
    stream = BytesIO()
    Image.new("RGB", (64, 48), "black").save(stream, format="PNG")
    peer.png = stream.getvalue()
    with pytest.raises(KeyboardFrameChangedBeforeInput) as error:
        peer.client.deliver(payload, digest)
    assert error.value.changed_pixel_count == 64 * 48
    assert error.value.changed_bbox == (0, 0, 64, 48)
    assert error.value.target_fingerprint == peer.target["fingerprint"]
    assert not peer.delivered
    with pytest.raises(StructuralResolutionRefused):
        peer.client.deliver(payload, digest)
    assert not peer.delivered and len(peer.sent) == 2


@pytest.mark.parametrize(
    "change", ["zero", "bool_count", "bbox", "oversized", "no_input", "wrong_lease"]
)
def test_malformed_refusal_never_creates_a_retryable_frame_change(change):
    peer = _Peer()
    digest = peer.arm()
    peer.png += b"different PNG encoding"

    def corrupt(path, result):
        assert path == "/keyboard/input"
        result["frame_change"] = {
            "changed_pixel_count": 1,
            "changed_bbox": [0, 0, 1, 1],
            "frame_size": [64, 48],
        }
        if change == "zero":
            result["frame_change"]["changed_pixel_count"] = 0
        elif change == "bool_count":
            result["frame_change"]["changed_pixel_count"] = True
        elif change == "bbox":
            result["frame_change"]["changed_bbox"] = [64, 0, 1, 1]
        elif change == "oversized":
            result["frame_change"]["changed_pixel_count"] = 64 * 48 + 1
        elif change == "no_input":
            result["no_keyboard_input_delivered"] = False
        else:
            result["lease_id"] = "e" * 32
        return result

    peer.corrupt = corrupt
    expected = (
        ActionDeliveryUncertain
        if change in {"no_input", "wrong_lease"}
        else StructuralResolutionRefused
    )
    with pytest.raises(expected) as error:
        peer.client.deliver(INPUTS[0], digest)
    assert not isinstance(error.value, KeyboardFrameChangedBeforeInput)
    assert not peer.delivered and len(peer.sent) == 2
