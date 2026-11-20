"""Qualification mutations require opt-in and real observations, never a claim."""

from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest
import requests

from openadapt_flow.backends.win_agent import AgentConfig, create_server
from openadapt_flow.backends.win_agent.qualification_faults import (
    FaultStagingError,
    QualificationFaultController,
)
from openadapt_flow.backends.windows_backend import WindowsBackend
from openadapt_flow.ir import StructuralLocator

PNG = b"\x89PNG\r\n\x1a\n"
TOKEN = "a" * 32
LOCATOR = {
    "automation_id": "save",
    "role": "button",
    "name": "Save",
    "window_name": "Test",
}


class Process:
    stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def wait(self, timeout):
        assert self.stopped
        return 0


class Surface:
    """A deterministic input source; assertions test the observation requirement."""

    staged = False
    change_pixels = True
    change_uia = True
    truncated = False
    process = None

    def launch(self, payload):
        assert payload["kind"] in {"ambiguity", "wrong_identity", "stale_identity"}
        self.staged = True
        self.process = Process()
        return self.process

    def capture(self):
        return PNG + (b"changed" if self.staged and self.change_pixels else b"before")

    def uia(self, operation, payload):
        changed = self.staged and self.change_uia
        if operation == "find":
            return {
                "status": "ok",
                "match": "ambiguous" if changed else "unique",
                "candidate_count": 3 if changed else 1,
                "truncated": self.truncated,
                "candidates": [
                    {**LOCATOR, "bounds": [10, 10, 110, 50], "point": [60, 30]}
                ],
            }
        assert operation == "element-identity-at"
        return {"status": "ok", "identity": {"name": "altered" if changed else "Save"}}

    def controller(self):
        return QualificationFaultController(
            self.uia, self.capture, launch=self.launch, timeout_s=0.02
        )


@pytest.mark.parametrize("kind", ["ambiguity", "wrong_identity", "stale_identity"])
def test_staging_requires_changed_surface_and_uia_then_cleans_only_its_process(kind):
    surface = Surface()
    controller = surface.controller()
    receipt = controller.stage(kind, LOCATOR)
    assert receipt["kind"] == kind
    assert len(receipt["mutation_id"]) == 32
    with pytest.raises(FaultStagingError, match="not_owned"):
        controller.clear(TOKEN)
    assert not surface.process.stopped
    assert controller.clear(receipt["mutation_id"]) == {"status": "ok", "cleared": True}
    assert surface.process.stopped


@pytest.mark.parametrize("missing", ["change_pixels", "change_uia"])
@pytest.mark.parametrize("kind", ["ambiguity", "wrong_identity", "stale_identity"])
def test_unobserved_mutation_declines_and_removes_overlay(kind, missing):
    surface = Surface()
    setattr(surface, missing, False)
    with pytest.raises(FaultStagingError, match="not_observed"):
        surface.controller().stage(kind, LOCATOR)
    assert surface.process.stopped


def test_incomplete_census_never_launches_a_fault():
    surface = Surface()
    surface.truncated = True
    with pytest.raises(FaultStagingError, match="not_unique"):
        surface.controller().stage("ambiguity", LOCATOR)
    assert not surface.staged


def test_server_shutdown_removes_overlay_and_concurrent_stage_is_refused():
    surface = Surface()
    controller = surface.controller()
    controller.stage("ambiguity", LOCATOR)
    with pytest.raises(FaultStagingError, match="mutation_active"):
        controller.stage("wrong_identity", LOCATOR)
    controller.close()
    assert surface.process.stopped


class Faults:
    def __init__(self):
        self.calls = []

    def stage(self, kind, locator):
        self.calls.append((kind, locator))
        return {"status": "ok", "mutation_id": TOKEN, "kind": kind}

    def clear(self, token):
        self.calls.append(("clear", token))
        return {"status": "ok", "cleared": True}

    def close(self):
        self.calls.append(("close",))


@contextmanager
def agent(*, enabled=True, faults=None):
    config = AgentConfig(
        host="127.0.0.1", port=0, token="test-token", allow_qualification_faults=enabled
    )
    server = create_server(config, fault_controller=faults or Faults())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_fault_routes_are_absent_and_unadvertised_by_default():
    with agent(enabled=False) as url:
        assert (
            "qualification_faults_v1"
            not in requests.get(url + "/health").json()["capabilities"]
        )
        assert requests.post(url + "/qualification/fault", json={}).status_code == 404


def test_opt_in_requires_authentication_configuration_and_each_request():
    with pytest.raises(ValueError, match="authenticated"):
        AgentConfig(allow_qualification_faults=True)
    faults = Faults()
    with agent(faults=faults) as url:
        assert requests.post(url + "/qualification/fault", json={}).status_code == 401
        assert not faults.calls


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "ambiguity", "locator": LOCATOR, "command": "anything"},
        {"kind": "click", "locator": LOCATOR},
        {"kind": [], "locator": LOCATOR},
        {"kind": "ambiguity", "locator": {"selector": "#save"}},
        {"kind": "ambiguity", "locator": {"automation_id": "x" * 513}},
    ],
)
def test_route_rejects_unbounded_or_untyped_requests_before_controller(payload):
    faults = Faults()
    with agent(faults=faults) as url:
        response = requests.post(
            url + "/qualification/fault",
            json=payload,
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 400
        assert not faults.calls


def test_backend_roundtrip_stages_and_clears_without_delivering_input():
    faults = Faults()
    with agent(faults=faults) as url:
        with pytest.warns(UserWarning, match="plaintext"):
            backend = WindowsBackend(url, auth_token="test-token")

        def forbidden():
            pytest.fail("a qualification mutation is not an application input")

        backend._qualification_input_guard = forbidden
        assert (
            backend.stage_qualification_fault("ambiguity", StructuralLocator(**LOCATOR))
            == TOKEN
        )
        backend.clear_qualification_fault(TOKEN)
    assert faults.calls == [("ambiguity", LOCATOR), ("clear", TOKEN), ("close",)]


def test_backend_refuses_disabled_fault_route():
    with agent(enabled=False) as url:
        with pytest.warns(UserWarning, match="plaintext"):
            backend = WindowsBackend(url, auth_token="test-token")
        with pytest.raises(RuntimeError, match="refused"):
            backend.stage_qualification_fault("ambiguity", StructuralLocator(**LOCATOR))


def test_backend_rejects_a_mismatched_receipt():
    class WrongFaults(Faults):
        def stage(self, kind, locator):
            return {"status": "ok", "mutation_id": TOKEN, "kind": "stale_identity"}

    with agent(faults=WrongFaults()) as url:
        with pytest.warns(UserWarning, match="plaintext"):
            backend = WindowsBackend(url, auth_token="test-token")
        with pytest.raises(RuntimeError, match="receipt is invalid"):
            backend.stage_qualification_fault("ambiguity", StructuralLocator(**LOCATOR))


def test_failed_cleanup_retains_ownership_and_blocks_another_overlay():
    surface = Surface()
    controller = surface.controller()
    receipt = controller.stage("ambiguity", LOCATOR)
    original = surface.process.terminate

    def unavailable():
        raise OSError("process cleanup temporarily unavailable")

    surface.process.terminate = unavailable
    with pytest.raises(OSError):
        controller.clear(receipt["mutation_id"])
    with pytest.raises(FaultStagingError, match="mutation_active"):
        controller.stage("wrong_identity", LOCATOR)
    surface.process.terminate = original
    assert controller.clear(receipt["mutation_id"])["cleared"]


class RecordSurface(Surface):
    record_changes = True

    def launch(self, payload):
        self.launched = payload
        return super().launch(payload)

    def uia(self, operation, payload):
        if operation == "element-identity-at" and payload != {"x": 60, "y": 30}:
            changed = self.staged and self.record_changes
            return {
                "status": "ok",
                "identity": {
                    "name": "Different record 8675309"
                    if changed
                    else "Original record",
                    "automation_id": "qualification-altered-identity"
                    if changed
                    else "record-header",
                },
            }
        return super().uia(operation, payload)


@pytest.mark.parametrize(
    "offset", [(-50, -20, 120, 30), (100, 80, 300, 45), (-200, 200, 200, 60)]
)
def test_wrong_identity_mutates_recorded_region_at_live_resolved_save(offset):
    surface = RecordSurface()
    controller = surface.controller()
    receipt = controller.stage("wrong_identity", LOCATOR, identity_offset=offset)
    dx, dy, width, height = offset
    assert surface.launched["identity_bounds"] == [
        60 + dx,
        30 + dy,
        60 + dx + width,
        30 + dy + height,
    ]
    assert surface.launched["bounds"] == [10, 10, 110, 50]
    controller.clear(receipt["mutation_id"])


def test_changed_save_label_cannot_attest_unchanged_record_identity():
    surface = RecordSurface()
    surface.record_changes = False
    with pytest.raises(FaultStagingError, match="not_observed"):
        surface.controller().stage(
            "wrong_identity", LOCATOR, identity_offset=(150, -25, 300, 30)
        )
    assert surface.process.stopped


@pytest.mark.parametrize(
    "kind,offset",
    [
        ("wrong_identity", [0, 0, 0, 10]),
        ("wrong_identity", [0, 0, True, 10]),
        ("wrong_identity", [0, 0, 1.0, 10]),
        ("wrong_identity", [0, 0, 8193, 10]),
        ("wrong_identity", [0, 0, 8192, 8192]),
        ("wrong_identity", [32769, 0, 1, 1]),
        ("wrong_identity", [0, 0, 1, 1, 1]),
        ("ambiguity", [0, 0, 120, 20]),
        ("stale_identity", [0, 0, 120, 20]),
    ],
)
def test_identity_rectangle_schema_refuses_unbounded_and_unrelated_mutations(
    kind, offset
):
    faults = Faults()
    with agent(faults=faults) as url:
        response = requests.post(
            url + "/qualification/fault",
            json={
                "kind": kind,
                "locator": LOCATOR,
                "identity_offset": offset,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 400
        assert not faults.calls


def test_backend_delivers_only_recorded_relative_identity_rectangle():
    class RecordFaults(Faults):
        def stage(self, kind, locator, *, identity_offset=None):
            self.calls.append((kind, locator, identity_offset))
            return {"status": "ok", "mutation_id": TOKEN, "kind": kind}

    faults = RecordFaults()
    with agent(faults=faults) as url:
        with pytest.warns(UserWarning, match="plaintext"):
            backend = WindowsBackend(url, auth_token="test-token")
        backend.stage_qualification_fault(
            "wrong_identity",
            StructuralLocator(**LOCATOR),
            identity_offset=(-80, -160, 300, 30),
        )
        backend.clear_qualification_fault(TOKEN)
    assert faults.calls[0] == ("wrong_identity", LOCATOR, (-80, -160, 300, 30))


def test_identity_translation_cannot_escape_absolute_geometry_contract():
    surface = RecordSurface()
    with pytest.raises(FaultStagingError, match="geometry_unavailable"):
        surface.controller().stage(
            "wrong_identity", LOCATOR, identity_offset=(32700, 0, 400, 40)
        )
    assert not surface.staged


def test_small_identity_box_far_from_target_cannot_allocate_unbounded_union_overlay():
    surface = RecordSurface()
    with pytest.raises(FaultStagingError, match="geometry_unavailable"):
        surface.controller().stage(
            "wrong_identity", LOCATOR, identity_offset=(-20000, -20000, 100, 30)
        )
    assert not surface.staged
