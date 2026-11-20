"""Native campaign contracts, including refusal when native evidence is incomplete."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from openadapt_flow.ir import StructuralLocator
from openadapt_flow.qualification_faults import verify_fault_mutation_receipt
from openadapt_flow.qualify import surfaces
from openadapt_flow.qualify.campaign import (
    apply_verify_config,
    check_ready,
    configure_project,
)
from openadapt_flow.qualify.windows import (
    WindowsEnvironmentObserver,
    WindowsFaultDriver,
)
from openadapt_flow.verification import VerificationTier
from tests.test_qualify_declarative import APP, _config, _context, _workflow


def native_config():
    return _config(
        app={
            **APP,
            "target": "windows",
            "surface": "remote_in_session",
            "session": "fixture:open",
            "reset": {"command": "true"},
        }
    )


def test_native_readiness_requires_native_locator_and_session():
    workflow = _workflow()
    config = native_config()
    save, _ = apply_verify_config(workflow, config)
    assert "automation_id" in " ".join(
        check_ready(workflow, config, save, dict(workflow.params))
    )
    save.anchor.structural = StructuralLocator(automation_id="commit", role="button")
    assert check_ready(workflow, config, save, dict(workflow.params)) == []
    config.app.session = None
    assert "app.session" in " ".join(
        check_ready(workflow, config, save, dict(workflow.params))
    )


class Native:
    def __init__(self):
        self.identity = ("fixture-runtime", "sha256:" + "a" * 64, "b" * 64, "c" * 64)

    def qualification_environment_identity(self):
        return self.identity


def test_native_environment_observes_machine_image_and_session_each_time():
    observed = WindowsEnvironmentObserver(application_version="fixture-1")
    backend = Native()
    first = observed.observe(backend, "windows")
    assert first.application_identity == "fixture-runtime"
    assert first.application_version == "fixture-1"
    assert first.session_identity_sha256 == "b" * 64
    # Session is re-observed but not baked into the transferable environment.
    backend.identity = (
        backend.identity[0],
        backend.identity[1],
        "d" * 64,
        backend.identity[3],
    )
    next_session = observed.observe(backend, "windows")
    assert next_session.environment_digest == first.environment_digest
    assert next_session.session_identity_sha256 != first.session_identity_sha256
    for index, replacement in [
        (0, "other-runtime"),
        (1, "sha256:" + "d" * 64),
        (3, "d" * 64),
    ]:
        value = list(Native().identity)
        value[index] = replacement
        backend.identity = tuple(value)
        assert (
            observed.observe(backend, "windows").environment_digest
            != first.environment_digest
        )
    backend.identity = Native().identity
    assert (
        WindowsEnvironmentObserver(application_version="fixture-2")
        .observe(backend, "windows")
        .environment_digest
        != first.environment_digest
    )


@pytest.mark.parametrize(
    "identity", [None, ("runtime",), ("runtime", "v", "bad", "bad")]
)
def test_native_observer_refuses_incomplete_environment(identity):
    backend = Native()
    backend.identity = identity
    with pytest.raises(ValueError):
        WindowsEnvironmentObserver(application_version="1").observe(backend, "windows")


def test_native_project_binds_observed_environment_and_distinct_fault_contract():
    workflow, config = _workflow(), native_config()
    save, _ = apply_verify_config(workflow, config)
    save.anchor.structural = StructuralLocator(automation_id="commit", role="button")
    native = Native()
    native.stage_qualification_fault = lambda *_: None
    native.agent_capabilities = lambda: {
        "qualification_faults_v1",
        "uia_v1",
        "context_environment_v1",
    }
    environment = surfaces.environment_payload(native, config)
    setup = configure_project(
        workflow,
        config,
        save=save,
        params=dict(workflow.params),
        environment=environment,
        campaign_id="native-campaign",
        minimum_effect_tier=VerificationTier.INDEPENDENT_SYSTEM,
    )
    bound = workflow.qualification.environment
    assert bound.target_kind == "windows"
    assert bound.application_identity == native.identity[0]
    assert (
        bound.environment_digest
        == setup.observer.observe(native, "windows").environment_digest
    )
    assert "headless_chromium" not in bound.required_capabilities
    assert isinstance(setup.driver, WindowsFaultDriver)
    assert surfaces.execution_fields(native, config) == {
        "execution_target_kind": "windows"
    }


def test_native_fault_receipt_signs_captured_mutation_and_cleans_owned_overlay():
    workflow = _workflow()
    workflow.steps[-1].anchor.structural = StructuralLocator(
        automation_id="commit", role="button"
    )
    driver = WindowsFaultDriver(workflow)
    staged, cleared = [], []
    backend = SimpleNamespace(
        stage_qualification_fault=lambda kind, locator: staged.append((kind, locator))
        or "owned-id",
        clear_qualification_fault=cleared.append,
        screenshot=lambda: b"changed frame",
    )
    context = _context("wrong_identity", backend=backend)
    mutation = driver.mutate(context)
    assert mutation is not None
    assert (
        mutation.receipt.after_input_sha256
        == hashlib.sha256(b"changed frame").hexdigest()
    )
    assert verify_fault_mutation_receipt(
        mutation.receipt, trusted_public_key_base64=driver.public_key_base64
    )
    assert staged[0][0] == "wrong_identity"
    driver.cleanup(backend)
    driver.cleanup(backend)
    assert cleared == ["owned-id"]


def test_native_staging_failure_never_becomes_a_fault_receipt():
    driver = WindowsFaultDriver(_workflow())
    assert driver.mutate(_context("ambiguity", SimpleNamespace())) is None
    assert driver.declined == "native_fault_staging_unavailable"


def test_native_environment_refuses_disabled_fault_agent_before_representative_run():
    native = Native()
    native.stage_qualification_fault = lambda *_: None
    native.agent_capabilities = lambda: {"uia_v1", "context_environment_v1"}
    with pytest.raises(ValueError, match="enabled fault"):
        surfaces.environment_payload(native, native_config())
