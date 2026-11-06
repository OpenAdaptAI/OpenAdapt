"""RDP session composition with exact synthetic frames; no guest or display.

Task ownership and the PowerShell renderer have their own component controls.
Here the task boundary is intercepted, while the real backend, transport
liveness checks, pairing decoder, and request geometry remain in use.
"""

from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image, ImageGrab

from openadapt_flow.backends.rdp_backend import FreeRDPBackend
from openadapt_flow.backends.xfreerdp import RdpTarget, XFreeRDPTransport
from openadapt_flow.qualify import rdp_pairing
from openadapt_flow.qualify import rdp_pixel_fault_controller as module

CONNECTION = "a" * 64
GUEST = "b" * 64
OTHER = "c" * 64
FIELDS = (
    "application_identity",
    "application_version",
    "session_identity_sha256",
    "environment_digest",
)


@pytest.fixture
def rig(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(rdp_pairing.time, "monotonic", lambda: now[0])
    image = Image.new("RGB", (300, 200), (11, 22, 33))
    marker_points = [(290, 190), (291, 190), (292, 190)]
    for point in marker_points:
        image.putpixel(point, (1, 2, 3))
    child = SimpleNamespace(result=None)
    child.poll = lambda: child.result
    transport = XFreeRDPTransport(RdpTarget("synthetic.invalid"), password=lambda: "")
    transport._client_proc = child
    transport._session_client = child
    transport._session_digest = CONNECTION
    transport._display = 99
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: image.copy())

    def forbidden(*args, **kwargs):
        raise AssertionError("software controller test attempted native execution")

    monkeypatch.setattr(XFreeRDPTransport, "connect", forbidden)
    monkeypatch.setattr(XFreeRDPTransport, "_xdotool", forbidden)
    state = SimpleNamespace(
        now=now,
        image=image,
        child=child,
        transport=transport,
        owners=[],
        requests=[],
        closed=[],
        session_closes=[],
        mode="ok",
        marker_points=marker_points,
    )

    class Tasks:
        def __init__(self, lab, *, guest_dir, session_binding_sha256):
            assert lab is state
            assert guest_dir == r"C:\Synthetic\Owned"
            self.binding = session_binding_sha256
            self.handles = []
            state.owners.append(self)

        def launch(self, request):
            request = dict(request)
            state.requests.append(request)
            pairing = request["schema"] == "openadapt.rdp-pairing/v1"
            key = "connection_digest" if pairing else "session_identity_sha256"
            assert request[key] == self.binding
            if state.mode == "launch_failure" and not pairing:
                raise RuntimeError("synthetic uncertain staging")
            handle = SimpleNamespace(ready_status="", request=request)
            self.handles.append(handle)
            if pairing:
                nonce = request["nonce"]
                guest = OTHER if state.mode == "wrong_grid_guest" else GUEST
                connection = OTHER if state.mode == "wrong_grid_connection" else CONNECTION
                if state.mode == "wrong_grid_nonce":
                    nonce = "d" * 32
                material = "\0".join(("openadapt.rdp-pairing/v1", nonce, connection, guest))
                bits = "".join(f"{byte:08b}" for byte in hashlib.sha256(material.encode()).digest())
                image.paste((0, 0, 0), (0, 0, 52, 52))
                for index, bit in enumerate(bits):
                    if bit == "1":
                        x, y = 2 + (index % 16) * 3, 2 + (index // 16) * 3
                        image.paste((255, 255, 255), (x, y, x + 3, y + 3))
                handle.ready_status = f"ready:{request['nonce']}:{GUEST}"
                if state.mode == "no_grid":
                    image.paste((0, 0, 0), (0, 0, 52, 52))
                if state.mode == "wrong_status_nonce":
                    handle.ready_status = f"ready:{'e' * 32}:{GUEST}"
            elif state.mode != "unchanged":
                region = request.get("duplicate", request.get("identifier_region"))
                image.putpixel((region["x"], region["y"]), (255, 0, 0))
                if state.mode == "witness_lost":
                    image.putpixel((0, 0), (255, 255, 255))
            return handle

        def wait_ready(self, handle, *, expected_guest_digest=None, timeout_s=15):
            assert handle in self.handles
            if state.mode == "expired_wait":
                now[0] = 70.0
            pairing = handle.request["schema"] == "openadapt.rdp-pairing/v1"
            if state.mode == "wait_failure" and not pairing:
                return None
            if state.mode == "wrong_fault_guest" and not pairing:
                return f"ready:{handle.request['nonce']}:{OTHER}"
            return handle.ready_status if pairing else f"ready:{handle.request['nonce']}:{GUEST}"

        def close(self):
            state.closed.append(self)
            if state.mode == "close_failure":
                raise RuntimeError("synthetic creation receipt mismatch")

    monkeypatch.setattr(module, "OwnedPixelTasks", Tasks)

    def probe(point):
        def check(png):
            with Image.open(io.BytesIO(png)) as actual:
                return actual.getpixel(point) == (1, 2, 3)
        return check

    def open_session(**changes):
        values = dict(
            application_marker="synthetic-app",
            application_probe=probe(marker_points[0]),
            application_version_marker="synthetic-version",
            application_version_probe=probe(marker_points[1]),
            environment_marker="synthetic-environment",
            environment_probe=probe(marker_points[2]),
            witness_region=(0, 0, 52, 52),
            excluded_regions=((100, 60, 120, 80),),
            guest_dir=r"C:\Synthetic\Owned",
            close_owner=lambda: state.session_closes.append(True),
        )
        values.update(changes)
        return module.open_rdp_qualification_session(transport, state, **values)

    state.open = open_session
    return state


def context_for(backend, kind="ambiguity"):
    before = backend.screenshot()
    context = SimpleNamespace(
        backend=backend,
        actuation_path="gui",
        step_id="synthetic-step",
        fault_kind=kind,
        before_input_sha256=hashlib.sha256(before).hexdigest(),
        capture_frame=backend.screenshot,
    )
    step = SimpleNamespace(
        id=context.step_id,
        anchor=SimpleNamespace(
            region=(120, 80, 12, 12), search_pad=40, identifier_region=(170, 100, 12, 12)
        ),
    )
    return context, step


def observation(backend):
    return SimpleNamespace(**dict(zip(FIELDS, backend.qualification_environment_identity())))


@pytest.mark.parametrize("kind", ["ambiguity", "wrong_identity", "stale_identity"])
def test_paired_observation_stages_one_guest_bound_fault_and_closes_owners(rig, kind):
    backend, close = rig.open()
    assert type(backend) is FreeRDPBackend
    controller = backend.qualification_pixel_fault_controller
    observed = observation(backend)
    assert controller.matches_observation(backend, observed)
    assert observed.session_identity_sha256 == CONNECTION
    assert [owner.binding for owner in rig.owners] == [CONNECTION, GUEST]
    context, step = context_for(backend, kind)
    assert controller.apply_screen_fault(context, step)
    assert rig.requests[-1]["session_identity_sha256"] == GUEST
    assert rig.requests[-1]["kind"] == kind
    assert rig.requests[-1]["nonce"] != rig.requests[0]["nonce"]
    assert not controller.apply_screen_fault(context, step)
    assert len(rig.requests) == 2
    assert not controller.matches_observation(backend, observed)
    close()
    close()
    assert rig.closed == list(reversed(rig.owners))
    assert rig.session_closes == [True]
    assert backend.qualification_pixel_fault_controller is None


@pytest.mark.parametrize("index", [0, 1, 2])
def test_configured_labels_cannot_launch_a_witness_without_positive_frame(rig, index):
    rig.image.putpixel(rig.marker_points[index], (0, 0, 0))
    with pytest.raises(ValueError, match="before pairing"):
        rig.open()
    assert not rig.requests
    assert rig.session_closes == [True]


@pytest.mark.parametrize("mode", ["no_grid", "wrong_grid_guest", "wrong_grid_connection", "wrong_grid_nonce", "wrong_status_nonce", "expired_wait"])
def test_readiness_status_or_configuration_cannot_replace_exact_live_pairing(rig, mode):
    rig.mode = mode
    with pytest.raises(ValueError):
        rig.open()
    assert len(rig.requests) == 1
    assert len(rig.owners) == 1  # No guest mutation capability was created.
    assert rig.closed == rig.owners
    assert rig.session_closes == [True]


@pytest.mark.parametrize("field", FIELDS)
def test_observation_swap_is_refused(rig, field):
    backend, close = rig.open()
    controller = backend.qualification_pixel_fault_controller
    seen = observation(backend)
    setattr(seen, field, "unrelated")
    assert not controller.matches_observation(backend, seen)
    assert not controller.matches_observation(object(), observation(backend))
    close()


@pytest.mark.parametrize("change", ["expired", "dead", "connection", "transport", "environment"])
def test_changed_session_cannot_stage_fault(rig, change):
    backend, close = rig.open()
    controller = backend.qualification_pixel_fault_controller
    context, step = context_for(backend)
    if change == "expired":
        rig.now[0] = 70.0
    elif change == "dead":
        rig.child.result = 0
    elif change == "connection":
        rig.transport._session_digest = OTHER
    elif change == "transport":
        backend._transport = object()
    else:
        rig.image.putpixel(rig.marker_points[2], (0, 0, 0))
    assert not controller.apply_screen_fault(context, step)
    assert len(rig.requests) == 1
    close()


@pytest.mark.parametrize("change", ["hash", "step", "backend", "path", "witness_overlap", "unknown_fault"])
def test_fault_request_must_match_engine_frame_step_and_regions(rig, change):
    backend, close = rig.open()
    context, step = context_for(backend)
    if change == "hash":
        context.before_input_sha256 = OTHER
    elif change == "step":
        step.id = "another-step"
    elif change == "backend":
        context.backend = object()
    elif change == "path":
        context.actuation_path = "api"
    elif change == "witness_overlap":
        step.anchor.region = (10, 10, 12, 12)
    else:
        context.fault_kind = "unsupported"
    assert not backend.qualification_pixel_fault_controller.apply_screen_fault(context, step)
    assert len(rig.requests) == 1
    close()


@pytest.mark.parametrize("mode", ["launch_failure", "wait_failure", "wrong_fault_guest", "unchanged", "witness_lost"])
def test_uncertain_or_unproved_mutation_permanently_refuses_input_without_retry(rig, mode):
    backend, close = rig.open()
    controller = backend.qualification_pixel_fault_controller
    context, step = context_for(backend)
    rig.mode = mode
    assert not controller.apply_screen_fault(context, step)
    rig.mode = "ok"
    assert not controller.apply_screen_fault(context, step)
    assert len(rig.requests) == 2
    with pytest.raises(RuntimeError, match="qualified application"):
        backend.acquire_actuation_frame()
    close()


def test_close_receipt_failure_is_reported_and_not_retried(rig):
    backend, close = rig.open()
    rig.mode = "close_failure"
    with pytest.raises(RuntimeError, match="cleanup was not established"):
        close()
    with pytest.raises(RuntimeError, match="cleanup was not established"):
        close()
    assert rig.closed == list(reversed(rig.owners))
    assert rig.session_closes == [True]
    assert backend.qualification_environment_identity() is None


def test_invalid_arguments_do_not_take_ownership(rig):
    with pytest.raises(ValueError, match="positive"):
        rig.open(environment_probe=None)
    assert not rig.requests
    assert not rig.session_closes


@pytest.mark.parametrize("kind", ["ambiguity", "wrong_identity", "stale_identity"])
def test_existing_native_driver_signs_exact_rdp_frame_and_detector_binding(rig, kind):
    from openadapt_flow.qualification_faults import (
        QualificationFaultContext,
        expected_fault_detector,
        verify_fault_mutation_receipt,
    )
    from openadapt_flow.qualify.faults import NativePixelFaultDriver

    backend, close = rig.open()
    basic, step = context_for(backend, kind)
    context = QualificationFaultContext(
        project_id="synthetic-project",
        project_revision=1,
        project_contract_sha256=OTHER,
        campaign_id_sha256=OTHER,
        case_id_sha256=OTHER,
        case_input_sha256=OTHER,
        run_id_sha256=OTHER,
        step_id=step.id,
        actuation_path="gui",
        fault_kind=kind,
        gate=expected_fault_detector(kind)[0],
        before_input_sha256=basic.before_input_sha256,
        backend=backend,
        vision=None,
        effect_verifier=None,
        capture_frame=backend.screenshot,
    )
    driver = NativePixelFaultDriver(
        SimpleNamespace(program=None, steps=[step]),
        backend.qualification_pixel_fault_controller,
        target_kind="rdp",
    )
    mutation = driver.mutate(context)
    assert mutation is not None
    receipt = mutation.receipt
    assert verify_fault_mutation_receipt(receipt, trusted_public_key_base64=driver.public_key_base64)
    assert receipt.fault_kind == kind
    assert receipt.gate == expected_fault_detector(kind)[0]
    assert receipt.before_input_sha256 == basic.before_input_sha256
    assert receipt.after_input_sha256 == hashlib.sha256(backend.screenshot()).hexdigest()
    assert not mutation.replace_effect_verifier
    assert len(rig.requests) == 2
    close()
