"""Image downscaling for the grounder / state-verifier clients.

The end-to-end validation (benchmark/appliance_validation) found the served
4-bit VLM emits empty output on native-Retina screenshots, so the grounder and
state-verifier went inert. The clients now downscale full frames below the
model's ceiling before sending; the grounder maps the proposal back to the
original pixel space. These tests pin the scaling maths and the round-trip.
"""

from __future__ import annotations

import io

from PIL import Image

from openadapt_flow.runtime.remote_vlm import (
    RemoteGrounder,
    RemoteStateVerifier,
    _downscale_for_model,
    _MAX_MODEL_IMAGE_DIM,
)


def _png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 200, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _dims(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


class _StubClient:
    def __init__(self, *, ground=None, state=None):
        self._ground = ground
        self._state = state
        self.sent_dims: tuple[int, int] | None = None

    def ground(self, png, intent, ocr_text=None):
        self.sent_dims = _dims(png)
        return self._ground

    def verify_state(self, png, expected):
        self.sent_dims = _dims(png)
        return self._state


# --------------------------------------------------------------------------
# The helper
# --------------------------------------------------------------------------


def test_large_image_is_downscaled_below_the_ceiling():
    png, scale = _downscale_for_model(_png(2048, 3072))
    w, h = _dims(png)
    assert max(w, h) == _MAX_MODEL_IMAGE_DIM
    assert scale == _MAX_MODEL_IMAGE_DIM / 3072


def test_small_image_is_untouched():
    original = _png(300, 200)
    png, scale = _downscale_for_model(original)
    assert scale == 1.0
    assert png == original


def test_malformed_bytes_fail_open_to_original():
    png, scale = _downscale_for_model(b"not a png")
    assert png == b"not a png" and scale == 1.0


# --------------------------------------------------------------------------
# Grounder: sends a downscaled frame, maps the point back to original pixels
# --------------------------------------------------------------------------


def test_grounder_downscales_and_maps_point_back():
    # 2048 wide -> scaled to 1024 (scale 0.5). A model point at (500, 300) in
    # the downscaled frame is (1000, 600) in the original.
    stub = _StubClient(ground={"point": [500, 300], "confidence": 0.9})
    g = RemoteGrounder(stub)
    m = g.locate(_png(2048, 1024), "click Open", None)
    assert stub.sent_dims == (1024, 512)  # sent downscaled
    assert m is not None and m.point == (1000, 600)  # mapped back to original


def test_grounder_no_downscale_leaves_point_unchanged():
    stub = _StubClient(ground={"point": [42, 24], "confidence": 0.8})
    g = RemoteGrounder(stub)
    m = g.locate(_png(400, 300), "click Open", None)
    assert stub.sent_dims == (400, 300)
    assert m is not None and m.point == (42, 24)


# --------------------------------------------------------------------------
# State-verifier: downscales transparently, verdict unaffected
# --------------------------------------------------------------------------


def test_state_verifier_downscales_before_sending():
    stub = _StubClient(state={"holds": "yes"})
    v = RemoteStateVerifier(stub)
    assert v.verify(_png(2000, 2000), "the note is saved") == "yes"
    assert max(stub.sent_dims) == _MAX_MODEL_IMAGE_DIM
