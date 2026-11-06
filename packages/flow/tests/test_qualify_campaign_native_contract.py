"""Offline campaign binding controls; these do not qualify a native app.

The stand-ins expose protocol observations only. No native input is delivered,
and no successful native campaign or fault proof is manufactured here.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.backends.windows_backend import WindowsBackend
from openadapt_flow.ir import ActionKind, Anchor, Step, StructuralLocator, Workflow
from openadapt_flow.qualification import EnvironmentBoundary, QualificationCaseKind
from openadapt_flow.qualification_faults import (
    FaultMutationReceipt,
    QualificationFaultContext,
    QualificationFaultMutation,
    expected_fault_detector,
    fault_detector_contract_error,
    sha256_bytes,
    sign_fault_mutation_receipt,
    verify_fault_mutation_receipt,
)
from openadapt_flow.qualify import QualifyConfigError, parse_verify_config, qualify
from openadapt_flow.qualify.campaign import (
    CampaignSetup,
    QualifyHooks,
    TrialContext,
    _campaign_session_components,
    _run,
    apply_verify_config,
    check_ready,
)
from openadapt_flow.qualify.environment import (
    WebEnvironmentObserver,
    observe_campaign_environment,
    web_environment_digest,
    web_environment_payload,
)
from openadapt_flow.qualify.faults import WebFaultDriver, WindowsPixelFaultDriver
from openadapt_flow.qualify.keys import new_local_test_key
from openadapt_flow.qualify.windows_pixel_fault_controller import (
    WindowsQualificationPixelFaultController,
)
from openadapt_flow.runtime.replayer import Replayer
from tests import test_qualification_fault_contract as fault_contract
from tests.test_qualification_fault_contract import _FaultDriver, _run_fault

VERSION = "sha256:" + "4" * 64


class _ContextSession:
    """Typed synthetic HTTP observations; no real agent or input endpoint."""

    def __init__(self):
        self.identity = ("synthetic-app", VERSION, "1" * 64, "2" * 64)
        self.identity_reads = 0
        self.input_posts = 0

    def post(self, url, **_kwargs):
        if not url.endswith("/context/identity"):
            self.input_posts += 1
            raise AssertionError("observation-only control cannot send an input")
        self.identity_reads += 1
        payload = {"status": "ok"}
        if isinstance(self.identity, tuple):
            payload.update(
                zip(
                    ("application", "application_version", "session", "environment"),
                    self.identity,
                )
            )
        return SimpleNamespace(status_code=200, json=lambda: payload)

    def get(self, url, **_kwargs):
        if url.endswith("/health"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"status": "ok", "capabilities": []},
            )
        raise AssertionError("observation-only control must not capture a native app")


def _observed_backend():
    session = _ContextSession()
    with pytest.warns(UserWarning, match="loopback dev"):
        backend = WindowsBackend(
            "http://127.0.0.1:1", viewport=(1280, 800), session=session
        )
    return backend, session


def _config(target="web", **app_expectations):
    return parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "app": {
                "target": target,
                "name": "Synthetic app",
                "url": "http://127.0.0.1:9301/",
                "version": "1",
                "reset": {"command": "true"},
                **app_expectations,
            },
            "verifier": {
                "kind": "rest",
                "base_url": "http://127.0.0.1:9300",
                "records_path": "/records",
            },
            "record": {
                "name": "record",
                "match": ["record_key"],
                "fields": {"record_key": "record_key"},
            },
        },
        env={},
    )


def _workflow():
    return Workflow(
        name="contract-control",
        params={"record_key": "synthetic-record"},
        steps=[
            Step(
                id="save",
                intent="Save record",
                action=ActionKind.CLICK,
                risk="irreversible",
                identity_armed=True,
                anchor=Anchor(
                    template="templates/save.png",
                    region=(0, 0, 80, 30),
                    click_point=(40, 15),
                    structural=StructuralLocator(selector="#save"),
                    context_text="Save record",
                ),
            )
        ],
    )


@pytest.mark.parametrize("target", ["windows", "macos", "linux", "rdp", "citrix"])
def test_a_native_label_without_session_cannot_start_a_campaign(tmp_path, target):
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    Image.new("RGB", (80, 30), "white").save(bundle / "templates/save.png")
    _workflow().save(bundle)
    calls = []

    def forbidden(*_args):
        calls.append("called")
        raise AssertionError("a declared target is not native qualification evidence")

    with pytest.raises(QualifyConfigError):
        qualify(
            bundle,
            _config(target, application_identity="synthetic-app"),
            out=tmp_path / "out",
            hooks=QualifyHooks(
                open_session=forbidden,
                reset=forbidden,
                make_verifier=forbidden,
                before_trial=forbidden,
            ),
            log=lambda _line: None,
        )
    assert calls == []
    assert not (tmp_path / "out" / "cases").exists()


@pytest.mark.parametrize("declared_driver", [False, True])
def test_observed_native_metadata_does_not_replace_a_real_session_driver(
    declared_driver,
):
    backend, _session = _observed_backend()
    config = _config(
        "windows",
        application_identity="synthetic-app",
        version=VERSION,
        session="synthetic_contract:open_session",
    )
    workflow = _workflow()
    apply_verify_config(workflow, config)
    if declared_driver:
        # A real web driver implements the fault protocol and has a signing
        # identity, but it is not a native screen controller for this session.
        backend.qualification_pixel_fault_controller = WebFaultDriver(workflow)
    with pytest.raises(QualifyConfigError, match="driver|controller|native"):
        _campaign_session_components(
            backend, config, workflow, campaign_id="offline-refusal-control"
        )
    assert workflow.qualification is None
    assert backend._qualification_input_guard is None


def test_an_atomic_tuple_cannot_supply_a_missing_backend_target():
    backend = SimpleNamespace(
        qualification_environment_identity=lambda: (
            "synthetic-app",
            VERSION,
            "1" * 64,
            "2" * 64,
        )
    )
    with pytest.raises(ValueError):
        observe_campaign_environment(
            backend,
            target_kind="windows",
            application_version=VERSION,
            application_identity="synthetic-app",
        )


def test_native_target_does_not_replace_a_missing_atomic_observer():
    backend = SimpleNamespace(qualification_target_kind="windows")
    with pytest.raises(ValueError):
        observe_campaign_environment(
            backend,
            target_kind="windows",
            application_version=VERSION,
            application_identity="synthetic-app",
        )


@pytest.mark.parametrize("replacement", ["backend", "caller_target"])
def test_native_observer_is_bound_to_its_exact_session_and_target(replacement):
    backend, _session = _observed_backend()
    observer, _observed = observe_campaign_environment(
        backend,
        target_kind="windows",
        application_version=VERSION,
        application_identity="synthetic-app",
    )
    other, other_session = _observed_backend()
    with pytest.raises(ValueError):
        observer.observe(
            other if replacement == "backend" else backend,
            "rdp" if replacement == "caller_target" else "windows",
        )
    assert other_session.identity_reads == 0


@pytest.mark.parametrize(
    "incomplete",
    [None, ("synthetic-app", VERSION, "1" * 64), ("", VERSION, "1" * 64, "2" * 64)],
)
def test_an_incomplete_native_observation_cannot_be_bound(incomplete):
    backend, session = _observed_backend()
    session.identity = incomplete
    with pytest.raises(ValueError):
        observe_campaign_environment(
            backend,
            target_kind="windows",
            application_version=VERSION,
            application_identity="synthetic-app",
        )
    assert backend._qualification_input_guard is None


@pytest.mark.parametrize(
    "expectation",
    [
        {"target_kind": "rdp"},
        {"application_version": "2"},
        {"application_identity": "different-app"},
    ],
)
def test_native_observation_must_match_the_declared_boundary(expectation):
    backend, _session = _observed_backend()
    expected = {
        "target_kind": "windows",
        "application_version": VERSION,
        "application_identity": "synthetic-app",
        **expectation,
    }
    with pytest.raises(ValueError):
        observe_campaign_environment(backend, **expected)
    assert backend._qualification_input_guard is None


@pytest.mark.parametrize("changed", ["target", "session", "environment"])
def test_bound_native_observation_is_rechecked_before_an_input(changed):
    backend, session = _observed_backend()
    observer, observed = observe_campaign_environment(
        backend,
        target_kind="windows",
        application_version=VERSION,
        application_identity="synthetic-app",
    )
    boundary = EnvironmentBoundary(
        target_kind="windows",
        application="Synthetic app",
        application_identity=observed.application_identity,
        application_version=observed.application_version,
        environment_observer_id=observer.observer_id,
        environment_observer_contract_sha256=observer.contract_sha256,
        environment_digest=observed.environment_digest,
        runtime_version="unit-control",
        required_capabilities=[],
    )
    runner = Replayer(
        backend,
        vision=SimpleNamespace(),
        qualification_environment_observer=observer,
    )
    refusal, _observed, _ids = runner._bind_qualification_boundary(boundary)
    assert refusal is None
    assert backend._qualification_input_guard is not None
    if changed == "target":
        backend.qualification_target_kind = "rdp"
    else:
        values = list(session.identity)
        values[2 if changed == "session" else 3] = "3" * 64
        session.identity = tuple(values)
    with pytest.raises(StructuralResolutionRefused):
        backend.click(1, 1)
    assert session.input_posts == 0


class _UnchangedScreenClaim(_FaultDriver):
    """A signed claim which deliberately does not change the detector input."""

    receipt = None
    before_capture = None

    def mutate(self, context):
        self.calls += 1
        self.before_capture = context.before_input_sha256
        return self._signed_claim(context, "f" * 64)

    def _signed_claim(self, context, after):
        receipt = FaultMutationReceipt(
            project_id=context.project_id,
            project_revision=context.project_revision,
            project_contract_sha256=context.project_contract_sha256,
            campaign_id_sha256=context.campaign_id_sha256,
            case_id_sha256=context.case_id_sha256,
            case_input_sha256=context.case_input_sha256,
            run_id_sha256=context.run_id_sha256,
            step_id_sha256=sha256_bytes(context.step_id.encode()),
            actuation_path=context.actuation_path,
            fault_kind=context.fault_kind,
            gate=context.gate,
            driver_id=self._driver_id,
            driver_contract_sha256=self._contract_sha256,
            before_input_sha256=context.before_input_sha256,
            after_input_sha256=after,
            mutation_artifact_sha256=sha256_bytes(b"unrealized-screen-change"),
            attestation_key_id=self.key_id,
        )
        self.receipt = sign_fault_mutation_receipt(
            receipt, private_key=self.private_key
        )
        return QualificationFaultMutation(receipt=self.receipt)


@pytest.mark.parametrize(
    "kind",
    [
        QualificationCaseKind.AMBIGUITY,
        QualificationCaseKind.WRONG_IDENTITY,
        QualificationCaseKind.STALE_IDENTITY,
    ],
)
def test_a_signed_screen_claim_is_not_proof_when_the_capture_is_unchanged(
    tmp_path, kind
):
    # Exercise the shared runtime validator through its existing synthetic WEB
    # fixture. This is not native execution or a native qualification result.
    driver = _UnchangedScreenClaim(kind)
    report, backend = _run_fault(tmp_path, kind, driver)

    assert driver.calls == 1
    assert driver.receipt is not None
    assert verify_fault_mutation_receipt(
        driver.receipt, trusted_public_key_base64=driver.public_key_base64
    )
    assert driver.before_capture == sha256_bytes(backend.screenshot())
    assert driver.receipt.after_input_sha256 != driver.before_capture
    assert report.execution_outcome in {"FAILED", "HALTED"}
    assert report.qualification_fault_mutations == []
    assert backend.actions == []
    assert "qualification_fault_receipt_binding_invalid" in (
        report.results[0].error or ""
    )


class _UnrelatedScreenClaim(_UnchangedScreenClaim):
    def __init__(self, kind, monkeypatch):
        super().__init__(kind)
        self.monkeypatch = monkeypatch

    def mutate(self, context):
        self.calls += 1
        self.before_capture = context.before_input_sha256
        original = fault_contract._screen_png

        def unrelated_corner(**kwargs):
            image = Image.open(io.BytesIO(original(**kwargs))).convert("RGB")
            # Outside both the fixture's action target and identity band.
            image.putpixel((0, 0), (7, 11, 13))
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()

        self.monkeypatch.setattr(fault_contract, "_screen_png", unrelated_corner)
        return self._signed_claim(context, sha256_bytes(context.capture_frame()))


@pytest.mark.parametrize(
    "kind", [QualificationCaseKind.AMBIGUITY, QualificationCaseKind.WRONG_IDENTITY]
)
def test_unrelated_changed_pixels_do_not_prove_the_named_screen_fault(
    tmp_path, monkeypatch, kind
):
    # Shared WEB fixture only. A real changed capture and authentic driver
    # signature still require the ordinary named detector's refusal.
    driver = _UnrelatedScreenClaim(kind, monkeypatch)
    report, backend = _run_fault(tmp_path, kind, driver)

    assert driver.calls == 1
    assert driver.receipt is not None
    assert driver.before_capture != sha256_bytes(backend.screenshot())
    assert report.qualification_fault_mutations == [driver.receipt]
    assert verify_fault_mutation_receipt(
        driver.receipt, trusted_public_key_base64=driver.public_key_base64
    )
    assert backend.ambiguous is False
    assert backend.wrong_identity is False
    assert backend.actions == []
    assert fault_detector_contract_error(report, driver.receipt) == (
        "fault_detector_refusal_not_observed"
    )


@pytest.mark.parametrize("kind", ["ambiguity", "wrong_identity", "stale_identity"])
@pytest.mark.parametrize("controller_result", [False, True, "error"])
def test_native_driver_declines_failure_or_an_unchanged_captured_input(
    kind, controller_result
):
    calls = []
    captures = []

    def apply_screen_fault(_context, _step):
        calls.append("apply")
        if controller_result == "error":
            raise RuntimeError("synthetic controller failure")
        return controller_result

    def capture():
        captures.append("capture")
        return b"unchanged-synthetic-frame"

    driver = WindowsPixelFaultDriver(
        _workflow(),
        SimpleNamespace(
            qualification_target_kind="windows",
            apply_screen_fault=apply_screen_fault,
        ),
    )
    gate, _code = expected_fault_detector(kind)
    context = QualificationFaultContext(
        project_id="offline-control",
        project_revision=1,
        project_contract_sha256="1" * 64,
        campaign_id_sha256="2" * 64,
        case_id_sha256="3" * 64,
        case_input_sha256="4" * 64,
        run_id_sha256="5" * 64,
        step_id="save",
        actuation_path="gui",
        fault_kind=kind,
        gate=gate,
        before_input_sha256=sha256_bytes(b"unchanged-synthetic-frame"),
        backend=object(),
        vision=None,
        effect_verifier=None,
        capture_frame=capture,
    )
    assert driver.mutate(context) is None
    assert calls == ["apply"]
    assert captures == (["capture"] if controller_result is True else [])


def test_a_fresh_session_gets_its_own_observer_without_changing_the_source_contract():
    first, _first_session = _observed_backend()
    second, second_session = _observed_backend()
    second_session.identity = ("synthetic-app", VERSION, "3" * 64, "2" * 64)
    options = {
        "target_kind": "windows",
        "application_version": VERSION,
        "application_identity": "synthetic-app",
    }
    first_observer, first_observation = observe_campaign_environment(first, **options)
    second_observer, second_observation = observe_campaign_environment(
        second, **options
    )

    assert first_observer is not second_observer
    assert first_observer.observer_id == second_observer.observer_id
    assert first_observer.contract_sha256 == second_observer.contract_sha256
    assert first_observation.session_identity_sha256 != (
        second_observation.session_identity_sha256
    )
    with pytest.raises(ValueError):
        first_observer.observe(second, "windows")


@pytest.mark.parametrize(
    "changed", ["backend", "target", "application", "version", "session", "environment"]
)
def test_controller_binding_drift_refuses_before_any_lab_method(changed):
    calls = []

    class ForbiddenLab:
        def __getattr__(self, _name):
            calls.append("lab")
            raise AssertionError("binding refusal must precede any lab operation")

    backend, session = _observed_backend()
    controller = WindowsQualificationPixelFaultController(
        backend,
        ForbiddenLab(),
        environment_identity=session.identity,
        guest_dir=r"C:\Temp\qualification-unit",
    )
    active = backend
    if changed == "backend":
        active, _other_session = _observed_backend()
    elif changed == "target":
        backend.qualification_target_kind = "rdp"
    else:
        index = {"application": 0, "version": 1, "session": 2, "environment": 3}[
            changed
        ]
        values = list(session.identity)
        values[index] = (
            "different-app"
            if index == 0
            else "sha256:" + "5" * 64
            if index == 1
            else "3" * 64
        )
        session.identity = tuple(values)

    assert (
        controller.apply_screen_fault(
            SimpleNamespace(backend=active, actuation_path="gui"), _workflow().steps[0]
        )
        is False
    )
    assert calls == []


class _WebPage:
    url = "http://127.0.0.1:9301/"
    context = SimpleNamespace(browser=SimpleNamespace(version="unit-browser"))

    def evaluate(self, expression):
        if expression == "location.origin":
            return "http://127.0.0.1:9301"
        if expression == "navigator.userAgent":
            return "unit-agent"
        if expression == "window.devicePixelRatio":
            return 1
        if "performance.timeOrigin" in expression:
            return "fixed-synthetic-session"
        raise AssertionError("unexpected browser observation")


def test_supported_web_observation_and_readiness_remain_compatible():
    backend = SimpleNamespace(page=_WebPage(), viewport=(1280, 800))
    observer, observed = observe_campaign_environment(
        backend,
        target_kind="web",
        application_version="1",
        application_identity="http://127.0.0.1:9301",
    )
    assert isinstance(observer, WebEnvironmentObserver)
    assert observed.target_kind == "web"
    assert observed.application_identity == "http://127.0.0.1:9301"
    assert observed.environment_digest == web_environment_digest(
        web_environment_payload(backend, application_version="1", headless=True)
    )
    workflow = _workflow()
    config = _config()
    save, _notes = apply_verify_config(workflow, config)
    assert check_ready(workflow, config, save, dict(workflow.params)) == []


@pytest.mark.parametrize("target", ["web", "windows"])
@pytest.mark.parametrize("inject_fault", [False, True])
def test_trial_handoff_keeps_faults_opt_in_and_rebinds_the_live_session(
    tmp_path, monkeypatch, target, inject_fault
):
    """Exercise _run's actual consumer handoff without executing any input."""
    workflow = _workflow()
    key = new_local_test_key("handoff-control")
    config = _config(target)

    class ForbiddenLab:
        def __getattr__(self, _name):
            raise AssertionError("handoff-only control cannot operate a native lab")

    def opened_backend(session_digit):
        if target == "web":
            return SimpleNamespace(page=_WebPage(), viewport=(1280, 800))
        backend, session = _observed_backend()
        session.identity = (
            *session.identity[:2],
            session_digit * 64,
            session.identity[3],
        )
        backend.qualification_pixel_fault_controller = (
            WindowsQualificationPixelFaultController(
                backend,
                ForbiddenLab(),
                environment_identity=session.identity,
                guest_dir=r"C:\Temp\qualification-unit",
            )
        )
        return backend

    if target == "windows":
        config = _config(
            target,
            application_identity="synthetic-app",
            version=VERSION,
            session="synthetic_contract:open_session",
        )
    preflight_backend = opened_backend("1")
    observer, environment, driver, capabilities = _campaign_session_components(
        preflight_backend,
        config,
        workflow,
        campaign_id="handoff-control",
        fault_key=key,
    )
    setup = CampaignSetup(
        runner_key=new_local_test_key("runner-control"),
        fault_key=key,
        driver=driver,
        observer=observer,
        environment=environment,
        required_capabilities=capabilities,
    )
    trial_backend = opened_backend("3")
    if target == "windows" and not inject_fault:
        # Ordinary trials must not require a fault-injection controller.
        del trial_backend.qualification_pixel_fault_controller
    closed = []
    verifier = object()
    report = object()
    captured = {}

    class HandoffRecorder:
        def __init__(self, backend, **kwargs):
            captured.update(backend=backend, **kwargs)

        def run(self, _workflow, **kwargs):
            captured["run_kwargs"] = kwargs
            return report

    # Only the consumer is observed: the real _run opens and binds the fresh
    # trial session, authorizes it and supplies the Replayer constructor.
    monkeypatch.setattr("openadapt_flow.runtime.Replayer", HandoffRecorder)
    ctx = TrialContext(
        case_id="handoff-control",
        expected="VERIFIED",
        oracle_kind="exact_record",
        trial=1,
        trial_dir=tmp_path / "trial",
        run_dir=tmp_path / "run",
        run_id="handoff-unit",
        params=dict(workflow.params),
    )
    actual_report, actual_verifier = _run(
        workflow=workflow,
        bundle_dir=tmp_path / "bundle",
        config=config,
        hooks=QualifyHooks(
            open_session=lambda: (trial_backend, lambda: closed.append(True)),
            make_verifier=lambda _params: verifier,
        ),
        ctx=ctx,
        setup=setup,
        authorize=lambda _verifier, _kwargs: object(),
        fault_driver=driver if inject_fault else None,
        marks={},
    )

    assert actual_report is report and actual_verifier is verifier
    assert closed == [True]
    assert captured["backend"] is trial_backend
    assert captured["run_kwargs"]["execution_target_kind"] == target
    bound_observer = captured["qualification_environment_observer"]
    if target == "windows":
        assert bound_observer is not observer
        assert (
            bound_observer.observe(
                trial_backend, target_kind=target
            ).session_identity_sha256
            == "3" * 64
        )
        with pytest.raises(ValueError):
            bound_observer.observe(preflight_backend, target_kind=target)
    if inject_fault:
        delivered_driver = captured["qualification_fault_driver"]
        assert delivered_driver is not None
        assert delivered_driver.driver_id == driver.driver_id
        assert delivered_driver.contract_sha256 == driver.contract_sha256
        assert delivered_driver.public_key_base64 == driver.public_key_base64
    else:
        assert captured["qualification_fault_driver"] is None
