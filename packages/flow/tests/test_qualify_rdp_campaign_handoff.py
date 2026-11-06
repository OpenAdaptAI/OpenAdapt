"""Campaign admission and replay handoff using the paired RDP session fixture.

No guest tasks, displays, network, or replay inputs run. The concrete backend,
pairing/controller and campaign admission are real; the existing fixture
intercepts guest/image boundaries. Ordinary-run tests stop at Replayer entry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openadapt_flow.backends.windows_backend import WindowsBackend
from openadapt_flow.qualify import campaign
from openadapt_flow.qualify.config import QualifyConfigError
from openadapt_flow.qualify.faults import NativePixelFaultDriver
from openadapt_flow.qualify.windows_pixel_fault_controller import (
    WindowsQualificationPixelFaultController,
)
from tests.test_qualify_declarative import _config, _workflow
from tests.test_qualify_rdp_pixel_fault_controller import CONNECTION, OTHER
from tests.test_qualify_rdp_pixel_fault_controller import rig as rig


def config_for(target="rdp", **changes):
    app = dict(
        target=target,
        name="Synthetic native qualification",
        version="synthetic-version",
        application_identity="synthetic-app",
        surface="remote_display" if target == "rdp" else "local",
        session="synthetic.hooks:open_session",
        reset={"python": "synthetic.hooks:reset"},
    )
    app.update(changes)
    return _config(app=app)


def components(backend, config, *, require_fault_driver=True):
    return campaign._campaign_session_components(
        backend,
        config,
        _workflow(),
        campaign_id="synthetic-campaign",
        require_fault_driver=require_fault_driver,
    )


def test_valid_rdp_configuration_passes_actual_campaign_readiness():
    config = config_for()
    workflow = _workflow()
    save, _ = campaign.apply_verify_config(workflow, config)
    assert campaign.check_ready(workflow, config, save, dict(workflow.params)) == []


def test_rdp_components_bind_shared_driver_same_controller_and_live_observation(rig):
    backend, close = rig.open()
    try:
        observer, observed, driver, required = components(backend, config_for())
        assert type(driver) is NativePixelFaultDriver
        assert driver._target_kind == "rdp"
        assert driver._controller is backend.qualification_pixel_fault_controller
        assert observed.target_kind == "rdp"
        assert observed.session_identity_sha256 == CONNECTION
        assert observer.observe(backend, "rdp") == observed
        assert driver._controller.matches_observation(backend, observed)
        assert {
            "qualification_fault_driver",
            "qualification_pixel_fault_controller",
            "qualification_environment_identity",
            "pixel_input",
            "pixel_frame_capture",
        } <= set(required)
    finally:
        close()


@pytest.mark.parametrize("wrong", ["protocol", "windows", "missing"])
def test_wrong_controller_never_gains_rdp_campaign_authority(rig, wrong):
    backend, close = rig.open()
    original = backend.qualification_pixel_fault_controller
    if wrong == "protocol":
        replacement = SimpleNamespace(
            qualification_target_kind="rdp",
            session_identity_sha256=CONNECTION,
            matches_observation=lambda *args: True,
            apply_screen_fault=original.apply_screen_fault,
        )
    elif wrong == "windows":
        replacement = WindowsQualificationPixelFaultController(
            backend,
            rig,
            environment_identity=backend.qualification_environment_identity(),
            guest_dir=r"C:\Synthetic\WrongController",
        )
    else:
        replacement = None
    backend.qualification_pixel_fault_controller = replacement
    try:
        with pytest.raises(QualifyConfigError, match="matching environment-owned"):
            components(backend, config_for())
    finally:
        backend.qualification_pixel_fault_controller = original
        close()


def test_protocol_shaped_backend_cannot_substitute_for_concrete_transport(rig):
    backend, close = rig.open()
    fake = SimpleNamespace(
        qualification_target_kind="rdp",
        qualification_environment_identity=backend.qualification_environment_identity,
        qualification_capabilities=backend.qualification_capabilities,
        qualification_pixel_fault_controller=backend.qualification_pixel_fault_controller,
    )
    try:
        with pytest.raises(ValueError, match="concrete FreeRDPBackend"):
            components(fake, config_for())
    finally:
        close()


@pytest.mark.parametrize("changed", ["session", "environment", "version", "application"])
def test_changed_observation_or_expected_identity_refuses(rig, changed):
    backend, close = rig.open()
    config = config_for()
    if changed == "session":
        rig.transport._session_digest = OTHER
    elif changed == "environment":
        rig.image.putpixel(rig.marker_points[2], (0, 0, 0))
    elif changed == "version":
        config.app.version = "different-version"
    else:
        config.app.application_identity = "different-application"
    try:
        with pytest.raises((ValueError, QualifyConfigError)):
            components(backend, config)
    finally:
        close()


def test_citrix_remains_refused_by_readiness_and_components(rig):
    config = config_for("citrix")
    workflow = _workflow()
    save, _ = campaign.apply_verify_config(workflow, config)
    assert any(
        "no implemented" in problem
        for problem in campaign.check_ready(workflow, config, save, dict(workflow.params))
    )
    backend, close = rig.open()
    backend.qualification_target_kind = "citrix"
    try:
        with pytest.raises(QualifyConfigError, match="no implemented"):
            components(backend, config)
    finally:
        close()


@pytest.mark.parametrize("target", ["windows", "rdp"])
def test_ordinary_native_run_hands_no_fault_driver_to_replayer(
    rig, monkeypatch, tmp_path, target
):
    if target == "rdp":
        backend, close = rig.open()
        config = config_for()
    else:
        backend = WindowsBackend("https://synthetic.invalid")
        payload = dict(
            application="synthetic.exe",
            application_version="sha256:" + OTHER,
            session=CONNECTION,
            environment=OTHER,
        )
        monkeypatch.setattr(backend, "_execution_context", lambda: payload)
        monkeypatch.setattr(backend, "agent_capabilities", lambda: frozenset())
        close = backend._session.close
        config = config_for(
            "windows",
            application_identity=payload["application"],
            version=payload["application_version"],
        )
    observer, observed, driver, required = components(
        backend, config, require_fault_driver=False
    )
    assert observed.target_kind == target
    assert driver is None
    assert "qualification_fault_driver" not in required
    assert "qualification_pixel_fault_controller" not in required
    seen = []
    closed = []

    def owned_close():
        close()
        closed.append(True)

    class AtReplayBoundary(Exception):
        pass

    def intercept_replayer(actual_backend, **kwargs):
        seen.append((actual_backend, kwargs))
        raise AtReplayBoundary

    from openadapt_flow import runtime

    monkeypatch.setattr(runtime, "Replayer", intercept_replayer)
    workflow = _workflow()
    ctx = campaign.TrialContext(
        "representative", "", "", 0, tmp_path, tmp_path, "synthetic-run", dict(workflow.params)
    )
    hooks = campaign.QualifyHooks(
        open_session=lambda: (backend, owned_close),
        make_verifier=lambda params: object(),
    )
    with pytest.raises(AtReplayBoundary):
        campaign._run(
            workflow=workflow,
            bundle_dir=tmp_path,
            config=config,
            hooks=hooks,
            ctx=ctx,
            setup=SimpleNamespace(observer=observer, fault_key=None),
            authorize=lambda verifier, kwargs: object(),
            fault_driver=None,
            marks={},
        )
    assert closed == [True]
    assert len(seen) == 1
    assert seen[0][0] is backend
    assert seen[0][1]["qualification_fault_driver"] is None
    assert not seen[0][1]["qualification_environment_observer"] is None
    assert len(rig.requests) == (1 if target == "rdp" else 0)
