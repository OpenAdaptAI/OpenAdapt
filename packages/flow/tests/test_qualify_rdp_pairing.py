"""Exact PNG witnesses and refusal controls; no guest sessions or GUI."""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from openadapt_flow.qualify import rdp_pairing
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
    values = args(**changes)
    nonce = values.pop("nonce")
    values.pop("connection_digest")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rdp_pairing.secrets, "token_hex", lambda count: nonce)
        witness = RdpPairing(
            transport or Transport(),
            environment_marker="synthetic-environment",
            environment_probe=probe,
            **values,
        )
    assert witness.accept_guest_ready(f"ready:{nonce}:{GUEST}")
    return witness


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


def test_fresh_sessions_share_stable_environment_but_not_witnesses():
    first = pairing()
    second_transport = Transport()
    second_transport.digest = "4" * 64
    second_nonce = "5" * 32
    second = pairing(second_transport, nonce=second_nonce)
    assert first.environment_marker == second.environment_marker == "synthetic-environment"
    first_frame = frame()
    second_frame = frame(nonce=second_nonce, connection=second_transport.digest)
    assert first.environment_probe(first_frame)
    assert second.environment_probe(second_frame)
    assert not first.environment_probe(second_frame)
    assert not second.environment_probe(first_frame)
    assert not second.matches_transport(Transport())


def test_deadline_starts_before_guest_launch_and_cannot_reset(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(rdp_pairing.time, "monotonic", lambda: now[0])
    witness = RdpPairing(
        Transport(),
        viewport=VIEWPORT,
        region=REGION,
        excluded_regions=EXCLUDED,
        environment_marker="synthetic-environment",
        environment_probe=lambda png: True,
        lifetime_seconds=5,
    )
    request = witness.guest_request()
    assert not witness.environment_probe(frame(nonce=request["nonce"]))
    now[0] = 14.0
    assert witness.accept_guest_ready(f"ready:{request['nonce']}:{GUEST}")
    assert witness.environment_probe(frame(nonce=request["nonce"]))
    now[0] = 15.0
    assert not witness.environment_probe(frame(nonce=request["nonce"]))
    assert not witness.accept_guest_ready(f"ready:{request['nonce']}:{GUEST}")
    with pytest.raises(ValueError, match="expired"):
        witness.guest_request()


def test_deadline_is_checked_after_original_probe(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(rdp_pairing.time, "monotonic", lambda: now[0])

    def slow_original(png):
        now[0] = 70.0
        return True

    witness = pairing(probe=slow_original)
    assert not witness.environment_probe(frame())


def test_late_ready_cannot_start_a_new_lifetime(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(rdp_pairing.time, "monotonic", lambda: now[0])
    witness = RdpPairing(
        Transport(),
        viewport=VIEWPORT,
        region=REGION,
        excluded_regions=EXCLUDED,
        environment_marker="synthetic-environment",
        environment_probe=lambda png: True,
    )
    request = witness.guest_request()
    now[0] = 70.0
    assert not witness.accept_guest_ready(f"ready:{request['nonce']}:{GUEST}")
    assert not witness.environment_probe(frame(nonce=request["nonce"]))


def test_new_pairing_has_new_nonce_and_status_cannot_rebind():
    def prepare():
        return RdpPairing(
            Transport(),
            viewport=VIEWPORT,
            region=REGION,
            excluded_regions=EXCLUDED,
            environment_marker="synthetic-environment",
            environment_probe=lambda png: True,
        )

    first, second = prepare(), prepare()
    a, b = first.guest_request(), second.guest_request()
    assert a["nonce"] != b["nonce"]
    assert not second.accept_guest_ready(f"ready:{a['nonce']}:{GUEST}")
    assert second.accept_guest_ready(f"ready:{b['nonce']}:{GUEST}")
    assert not second.accept_guest_ready(f"ready:{b['nonce']}:{'4' * 64}")
