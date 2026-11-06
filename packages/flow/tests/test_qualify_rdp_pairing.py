"""Exact PNG witnesses and refusal controls; no guest sessions or GUI."""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from openadapt_flow.qualify.rdp_pairing import RdpPairing, pairing_request

NONCE = "1" * 32
CONNECTION = "2" * 64
GUEST = "3" * 64
VIEWPORT = (200, 150)
REGION = (0, 0, 52, 52)
EXCLUDED = ((80, 80, 20, 20),)


class Transport:
    digest = CONNECTION

    def session_identity(self):
        return self.digest


def args(**changes):
    values = dict(
        nonce=NONCE,
        connection_digest=CONNECTION,
        viewport=VIEWPORT,
        region=REGION,
        excluded_regions=EXCLUDED,
    )
    values.update(changes)
    return values


def pairing(transport=None, probe=lambda png: True, **changes):
    return RdpPairing(
        transport or Transport(),
        guest_digest=GUEST,
        environment_marker="synthetic-environment",
        environment_probe=probe,
        **args(**changes),
    )


def frame(nonce=NONCE, guest=GUEST, connection=CONNECTION, mutate=None):
    # Independent renderer follows the guest contract, not helper internals.
    material = "\0".join(("openadapt.rdp-pairing/v1", nonce, connection, guest))
    bits = "".join(f"{byte:08b}" for byte in hashlib.sha256(material.encode()).digest())
    image = Image.new("RGB", VIEWPORT, (0, 0, 0))
    for index, bit in enumerate(bits):
        if bit == "1":
            left = 2 + (index % 16) * 3
            top = 2 + (index // 16) * 3
            for x in range(left, left + 3):
                for y in range(top, top + 3):
                    image.putpixel((x, y), (255, 255, 255))
    if mutate:
        mutate(image)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_existing_environment_and_live_witness_use_identical_png():
    png = frame()
    seen = []
    witness = pairing(probe=lambda value: seen.append(value) is None)
    assert witness.environment_probe(png)
    assert seen == [png]
    assert len(witness.environment_marker) <= 128


@pytest.mark.parametrize(
    "values", [{"nonce": "4" * 32}, {"guest": "4" * 64}, {"connection": "4" * 64}]
)
def test_wrong_nonce_guest_or_connection_is_not_pairing(values):
    assert not pairing().environment_probe(frame(**values))


def test_original_environment_refusal_cannot_be_replaced_by_valid_witness():
    assert not pairing(probe=lambda png: False).environment_probe(frame())


def test_one_pixel_change_and_border_change_refuse():
    for point in ((0, 0), (2, 2)):

        def alter(image):
            old = image.getpixel(point)[0]
            image.putpixel(point, (255 - old,) * 3)

        assert not pairing().environment_probe(frame(mutate=alter))


def test_changed_connection_during_probe_refuses():
    transport = Transport()

    def original(png):
        transport.digest = "4" * 64
        return True

    assert not pairing(transport, original).environment_probe(frame())


def test_unavailable_connection_or_different_transport_refuses():
    transport = Transport()
    witness = pairing(transport)
    assert not witness.matches_transport(Transport())
    transport.digest = None
    assert not witness.environment_probe(frame())


def test_configuration_and_readiness_alone_are_not_frame_proof():
    witness = pairing()
    assert not witness.environment_probe(b"ready:" + NONCE.encode())
    assert not witness.environment_probe(
        frame(mutate=lambda img: img.paste(0, (0, 0, *img.size)))
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"region": (80, 80, 52, 52)},
        {"region": (180, 0, 52, 52)},
        {"region": (0, 0, 51, 52)},
        {"excluded_regions": ()},
        {"excluded_regions": ((True, 0, 10, 10),)},
        {"lifetime_seconds": 0},
        {"lifetime_seconds": 301},
        {"lifetime_seconds": True},
        {"nonce": "old"},
    ],
)
def test_invalid_or_protected_geometry_and_unbounded_lifetime_refuse(changes):
    with pytest.raises(ValueError):
        pairing_request(**args(**changes))


def test_request_contains_only_bounded_witness_parameters():
    request = pairing_request(**args())
    assert request == {
        "schema": "openadapt.rdp-pairing/v1",
        "operation": "pairing_witness",
        "nonce": NONCE,
        "connection_digest": CONNECTION,
        "viewport": [200, 150],
        "region": [0, 0, 52, 52],
        "lifetime_seconds": 60,
    }
