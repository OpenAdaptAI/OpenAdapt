"""Session-owned RDP qualification using the existing guest pixel renderer.

The application session hook supplies its already-owned transport and guest
channel. Configuration labels are expectations: attachment requires the real
backend's atomic observation with a fresh guest witness on that exact frame.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from openadapt_flow.backends.rdp_backend import FreeRDPBackend
from openadapt_flow.backends.xfreerdp import XFreeRDPTransport
from openadapt_flow.qualify.qualification_pixel_fault_task import OwnedPixelTasks
from openadapt_flow.qualify.rdp_pairing import RdpPairing, Rect
from openadapt_flow.qualify.windows_pixel_fault_controller import _request_for


class PixelTaskOwner(Protocol):
    """Existing guest-task operations supplied by a trusted Python session."""

    def launch(self, request: dict[str, Any]) -> Any: ...

    def wait_ready(self, handle: Any, *, timeout_s: float = 15) -> str | None: ...

    def close(self) -> None: ...


@dataclass
class _SessionState:
    usable: bool = True
    closed: bool = False
    cleanup_failed: bool = False


def _observation_tuple(observation: Any) -> tuple[Any, ...]:
    return tuple(
        getattr(observation, name, None)
        for name in (
            "application_identity",
            "application_version",
            "session_identity_sha256",
            "environment_digest",
        )
    )


class RdpQualificationPixelFaultController:
    """Stage one fault only while the same paired session remains observable."""

    qualification_target_kind = "rdp"

    def __init__(
        self,
        backend: FreeRDPBackend,
        transport: XFreeRDPTransport,
        pairing: RdpPairing,
        tasks: PixelTaskOwner,
        *,
        guest_digest: str,
        environment_identity: tuple[str, str, str, str],
        witness_region: Rect,
        state: _SessionState,
    ) -> None:
        self._backend = backend
        self._transport = transport
        self._pairing = pairing
        self._tasks = tasks
        self._guest_digest = guest_digest
        self._environment_identity = environment_identity
        self._witness_region = witness_region
        self._state = state
        self._attempted = False
        self.session_identity_sha256 = environment_identity[2]
        if not self._current():
            raise ValueError("RDP controller has no current paired environment")

    def _current(self) -> bool:
        if (
            not self._state.usable
            or self._state.closed
            or type(self._backend) is not FreeRDPBackend
            or getattr(self._backend, "_transport", None) is not self._transport
            or not self._pairing.matches_transport(self._transport)
        ):
            return False
        try:
            return (
                self._backend.qualification_environment_identity()
                == self._environment_identity
            )
        except Exception:
            return False

    def matches_observation(self, backend: Any, observation: Any) -> bool:
        return (
            backend is self._backend
            and _observation_tuple(observation) == self._environment_identity
            and self._current()
        )

    def _refuse(self) -> bool:
        # This state is part of the backend's existing environment probe.
        # A declined mutation must not leave ordinary input eligible afterward.
        self._state.usable = False
        return False

    def _outside_witness(self, request: dict[str, Any]) -> bool:
        x, y, width, height = self._witness_region
        for name in ("target", "duplicate", "identifier_region"):
            region = request.get(name)
            if region is None:
                continue
            a, b, w, h = (region[key] for key in ("x", "y", "width", "height"))
            if x < a + w and a < x + width and y < b + h and b < y + height:
                return False
        return True

    def apply_screen_fault(self, context: Any, step: Any) -> bool:
        if (
            self._attempted
            or getattr(context, "backend", None) is not self._backend
            or getattr(context, "actuation_path", None) != "gui"
            or getattr(context, "step_id", None) != getattr(step, "id", None)
            or not callable(getattr(context, "capture_frame", None))
            or not self._current()
        ):
            return self._refuse()
        try:
            before = context.capture_frame()
            if (
                hashlib.sha256(before).hexdigest() != context.before_input_sha256
                or not self._pairing.environment_probe(before)
            ):
                return self._refuse()
            request = _request_for(context, step, uuid.uuid4().hex)
            if request is None or not self._outside_witness(request):
                return self._refuse()
            request["session_identity_sha256"] = self._guest_digest
            self._attempted = True
            task = self._tasks.launch(request)
            status = self._tasks.wait_ready(task)
            if status != f"ready:{request['nonce']}:{self._guest_digest}":
                return self._refuse()
            after = context.capture_frame()
            if (
                hashlib.sha256(after).hexdigest() == context.before_input_sha256
                or not self._pairing.environment_probe(after)
                or not self._current()
            ):
                return self._refuse()
            return True
        except Exception:
            return self._refuse()


def open_rdp_qualification_session(
    transport: XFreeRDPTransport,
    lab: Any,
    *,
    application_marker: str,
    application_probe: Callable[[bytes], bool],
    application_version_marker: str,
    application_version_probe: Callable[[bytes], bool],
    environment_marker: str,
    environment_probe: Callable[[bytes], bool],
    witness_region: Rect,
    excluded_regions: tuple[Rect, ...],
    guest_dir: str,
    close_owner: Callable[[], None],
    lifetime_seconds: int = 60,
    readiness_probe: Callable[[bytes], bool] | None = None,
    task_owner_factory: Callable[[str], PixelTaskOwner] | None = None,
) -> tuple[FreeRDPBackend, Callable[[], None]]:
    """Build an ``app.session`` result from an existing owned RDP session.

    No connection or credential acquisition happens here. After argument
    validation, this hook takes responsibility for calling ``close_owner`` on
    setup failure or close. That callback must dispose only the session resources
    its caller actually owns. Guest tasks are separately creation-receipt bound.
    A trusted Python session may supply ``task_owner_factory`` for its guest
    environment; this callable is never loaded from qualification YAML. Without
    it, the existing Windows guest task implementation is used unchanged.
    """
    if type(transport) is not XFreeRDPTransport or not callable(close_owner):
        raise ValueError("RDP qualification requires its existing owned transport")
    probes = (application_probe, application_version_probe, environment_probe)
    if not all(callable(probe) for probe in probes) or not all(
        (application_marker, application_version_marker, environment_marker)
    ):
        raise ValueError(
            "RDP qualification needs positive application/environment probes"
        )
    if task_owner_factory is not None and not callable(task_owner_factory):
        raise ValueError("task owner factory must come from the Python session hook")
    state = _SessionState()
    task_owners: list[PixelTaskOwner] = []

    def make_tasks(binding: str) -> PixelTaskOwner:
        owner = (
            OwnedPixelTasks(lab, guest_dir=guest_dir, session_binding_sha256=binding)
            if task_owner_factory is None
            else task_owner_factory(binding)
        )
        # Track the returned owner before validation so setup failure also closes it.
        task_owners.append(owner)
        if not all(callable(getattr(owner, name, None)) for name in
                   ("launch", "wait_ready", "close")):
            raise ValueError("session task owner does not implement the task lifecycle")
        return owner
    backend: FreeRDPBackend | None = None
    controller: RdpQualificationPixelFaultController | None = None

    def close() -> None:
        if state.closed:
            if state.cleanup_failed:
                raise RuntimeError("RDP qualification cleanup was not established")
            return
        state.usable = False
        state.closed = True
        if backend is not None and (
            getattr(backend, "qualification_pixel_fault_controller", None) is controller
        ):
            setattr(backend, "qualification_pixel_fault_controller", None)
        errors: list[Exception] = []
        for owner in reversed(task_owners):
            try:
                owner.close()
            except Exception as exc:
                errors.append(exc)
        try:
            close_owner()
        except Exception as exc:
            errors.append(exc)
        if errors:
            state.cleanup_failed = True
            raise RuntimeError(
                "RDP qualification cleanup was not established"
            ) from errors[0]

    try:
        _, width, height = transport.framebuffer()

        def original_environment(png: bytes) -> bool:
            return all(probe(png) is True for probe in probes)

        pairing = RdpPairing(
            transport,
            viewport=(width, height),
            region=witness_region,
            excluded_regions=excluded_regions,
            environment_marker=environment_marker,
            environment_probe=original_environment,
            lifetime_seconds=lifetime_seconds,
        )

        def paired_environment(png: bytes) -> bool:
            return state.usable and not state.closed and pairing.environment_probe(png)

        backend = FreeRDPBackend(
            transport,
            connect=False,
            viewport=(width, height),
            readiness_probe=readiness_probe,
            application_marker=application_marker,
            application_marker_probe=application_probe,
            application_version_marker=application_version_marker,
            application_version_marker_probe=application_version_probe,
            environment_marker=pairing.environment_marker,
            environment_marker_probe=paired_environment,
        )
        # Establish the original app/version/environment on a real backend
        # frame before the first guest task can alter even the witness region.
        prelaunch = backend.screenshot()
        if (
            not original_environment(prelaunch)
            or (readiness_probe is not None and readiness_probe(prelaunch) is not True)
            or not pairing.matches_transport(transport)
        ):
            raise ValueError(
                "RDP application environment is unavailable before pairing"
            )
        request = pairing.guest_request()
        witness_tasks = make_tasks(request["connection_digest"])
        witness = witness_tasks.launch(request)
        if not witness_tasks.wait_ready(witness, timeout_s=min(15, lifetime_seconds)):
            raise ValueError("RDP guest pairing did not become ready")
        status = witness.ready_status
        if not isinstance(status, str) or not pairing.accept_guest_ready(status):
            raise ValueError("RDP guest pairing readiness is invalid or expired")
        identity = backend.qualification_environment_identity()
        if identity is None or identity[2] != request["connection_digest"]:
            raise ValueError("RDP guest witness does not bind the observed environment")
        guest_digest = status.rsplit(":", 1)[1]
        fault_tasks = make_tasks(guest_digest)
        controller = RdpQualificationPixelFaultController(
            backend,
            transport,
            pairing,
            fault_tasks,
            guest_digest=guest_digest,
            environment_identity=identity,
            witness_region=witness_region,
            state=state,
        )
        setattr(backend, "qualification_pixel_fault_controller", controller)
        return backend, close
    except Exception:
        close()
        raise


__all__ = ["RdpQualificationPixelFaultController", "open_rdp_qualification_session"]
