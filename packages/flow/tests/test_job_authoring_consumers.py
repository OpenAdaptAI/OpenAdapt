"""Actual parser, signer and entrypoint controls for explicit schema 3."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from openadapt_flow import bundle_validation as bundles
from openadapt_flow.ir import Workflow
from openadapt_flow.job_authoring import (
    JobAuthoringBinding,
    JobAuthoringError,
    verified_job_authoring,
)
from openadapt_flow.qualification_admission_v2 import (
    QualificationAdmissionError,
    QualificationAdmissionExpected,
    QualificationAdmissionPayload,
    sign_qualification_admission,
    verify_qualification_admission,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_job_authoring_schema3 import BINDING, authored
from tests.test_qualification_admission_v2 import (
    NOW,
    _expected,
    _payload,
    _private_key,
    _registry,
)


@pytest.mark.parametrize("encrypt", [False, True])
def test_verified_source_helpers_handle_actual_plain_and_encrypted_bundles(
    tmp_path, encrypt
):
    workflow = authored()
    key = "synthetic schema 3 test key" if encrypt else None
    workflow.save(tmp_path / "source", encrypt=encrypt, key=key)
    loaded = Workflow.load(tmp_path / "source", key=key)
    assert loaded.manifest.schema_version == 3
    assert verified_job_authoring(loaded, tmp_path / "source").model_dump() == BINDING
    assert loaded.manifest.provenance.certified is False


@pytest.mark.parametrize(
    "raw",
    [
        {"name": "invalid", "schema_version": 2, "job_authoring": BINDING},
        {"name": "invalid", "schema_version": 3},
        {"name": "invalid", "schema_version": 4, "job_authoring": BINDING},
    ],
)
def test_actual_cli_refuses_before_backend_build(tmp_path, monkeypatch, raw):
    import openadapt_flow.backends.factory as factory
    from openadapt_flow.__main__ import main

    source = tmp_path / "source"
    source.mkdir()
    (source / "workflow.json").write_text(json.dumps(raw))
    calls = []

    def never_build(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("an unsupported input must not build a backend")

    monkeypatch.setattr(factory, "build_backend", never_build)
    try:
        result = main(
            [
                "replay",
                str(source),
                "--url",
                "http://example.invalid",
                "--run-dir",
                str(tmp_path / "run"),
            ]
        )
    except (ValueError, SystemExit) as error:
        if isinstance(error, SystemExit):
            assert error.code != 0
    else:
        assert result != 0
    assert calls == []
    assert not (tmp_path / "run").exists()


def test_actual_deployment_factory_retains_shared_run_refusal(tmp_path, monkeypatch):
    from openadapt_flow.deployment import build_replayer

    # Preserve the real factory and Replayer.run. Avoid optional OCR/overlay
    # construction: invalid input must stop before any such state is needed.
    monkeypatch.setattr(Replayer, "__init__", lambda self, backend, **kw: None)
    replayer = build_replayer(
        object(),
        allow_egress=False,
        effect_verifier=None,
        api_actuator=None,
        durable=False,
        use_structural=False,
    )
    invalid = Workflow.model_construct(
        name="invalid", schema_version=3, job_authoring=None
    )
    with pytest.raises(JobAuthoringError):
        replayer.run(invalid, bundle_dir=tmp_path, run_dir=tmp_path / "run")
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize(
    "field", ["job_id", "org_id", "owner_principal_id", "intent_sha256"]
)
def test_existing_signed_admission_refuses_a_changed_binding_digest(field):
    registry = _registry()
    workflow = authored()
    original_digest = bundles.compute_content_digest(workflow, {})
    payload_data = _payload(registry).model_dump(mode="json")
    payload_data["bundle_content_digest"] = original_digest
    payload_data["evidence_identity"]["bundle_content_digest"] = original_digest
    payload_data["evidence_identity"]["workflow_digest"] = original_digest
    payload = QualificationAdmissionPayload.model_validate(payload_data)
    expected_data = _expected(payload).model_dump(mode="json")
    expected_data["bundle_content_digest"] = original_digest
    expected = QualificationAdmissionExpected.model_validate(expected_data)
    envelope = sign_qualification_admission(payload, _private_key())
    assert verify_qualification_admission(
        envelope, registry=registry, expected=expected, now=NOW
    )

    replacement = (
        "b" * 64 if field == "intent_sha256" else "44444444-4444-4444-8444-444444444444"
    )
    changed = workflow.model_copy(
        update={
            "job_authoring": JobAuthoringBinding.model_validate(
                {**BINDING, field: replacement}
            )
        }
    )
    changed_digest = bundles.compute_content_digest(changed, {})
    assert changed_digest != original_digest
    changed_expected = QualificationAdmissionExpected.model_validate(
        {**expected_data, "bundle_content_digest": changed_digest}
    )
    with pytest.raises(QualificationAdmissionError, match="bundle_content_digest"):
        verify_qualification_admission(
            envelope, registry=registry, expected=changed_expected, now=NOW
        )
    # Replacing signed payload values does not repair the old signature.
    payload_data["bundle_content_digest"] = changed_digest
    payload_data["evidence_identity"]["bundle_content_digest"] = changed_digest
    payload_data["evidence_identity"]["workflow_digest"] = changed_digest
    forged = envelope.model_copy(
        update={"payload": QualificationAdmissionPayload.model_validate(payload_data)}
    )
    with pytest.raises(QualificationAdmissionError, match="signature"):
        verify_qualification_admission(
            forged, registry=registry, expected=changed_expected, now=NOW
        )


class JobBindingTransitions(RuleBasedStateMachine):
    """Serialization, copying and reauthoring never inherit verified source by metadata."""

    def __init__(self):
        super().__init__()
        self.storage = TemporaryDirectory(prefix="job-binding-transitions-")
        self.root = Path(self.storage.name)
        self.sequence = 0
        self.source = self.root / "initial"
        authored().save(self.source)
        self.current = Workflow.load(self.source)
        self.expected = dict(BINDING)
        self.state = "verified"

    @rule(json_roundtrip=st.booleans())
    def roundtrip(self, json_roundtrip):
        self.current = (
            Workflow.model_validate_json(self.current.model_dump_json())
            if json_roundtrip
            else Workflow.model_validate(self.current.model_dump())
        )
        self.state = "metadata_only"

    @rule()
    def copy(self):
        self.current = self.current.model_copy(deep=True)

    @rule(
        field=st.sampled_from(
            ["job_id", "org_id", "owner_principal_id", "intent_sha256"]
        )
    )
    def mutate(self, field):
        self.sequence += 1
        value = (
            f"{self.sequence:064x}"
            if field == "intent_sha256"
            else f"{self.sequence:08x}-4444-4444-8444-444444444444"
        )
        wire = self.current.job_authoring.model_dump()
        wire[field] = value
        self.current = self.current.model_copy(
            update={"job_authoring": JobAuthoringBinding.model_validate(wire)}
        )
        self.state = "changed"

    @rule()
    def restore_actual_source(self):
        self.current = Workflow.load(self.source)
        self.state = "verified"

    @rule()
    def explicit_new_derivative(self):
        self.sequence += 1
        source = Workflow.load(self.source)
        prior = source.job_authoring.model_dump()
        wire = {**prior, "intent_sha256": f"{self.sequence:064x}"}
        derivative = bundles.derive_authored_workflow(
            source, update={"job_authoring": JobAuthoringBinding.model_validate(wire)}
        )
        assert not derivative.manifest.provenance.certified
        destination = self.root / f"derivative-{self.sequence}"
        derivative.save(destination)
        assert Workflow.load(self.source).job_authoring.model_dump() == prior
        self.source = destination
        self.current = Workflow.load(destination)
        self.expected = wire
        self.state = "verified"

    @invariant()
    def current_authority_requires_actual_unchanged_source(self):
        if self.state == "verified":
            assert (
                verified_job_authoring(
                    self.current, self.source, expected_binding=self.expected
                ).model_dump()
                == self.expected
            )
        else:
            with pytest.raises((JobAuthoringError, bundles.BundleIntegrityError)):
                verified_job_authoring(self.current, self.source)
            replayer = Replayer.__new__(Replayer)
            with pytest.raises((JobAuthoringError, bundles.BundleIntegrityError)):
                replayer.run(
                    self.current,
                    bundle_dir=self.source,
                    run_dir=self.root / "refused-run",
                )
            assert not (self.root / "refused-run").exists()

    def teardown(self):
        self.storage.cleanup()


TestJobBindingTransitions = JobBindingTransitions.TestCase
TestJobBindingTransitions.settings = settings(
    max_examples=15, stateful_step_count=8, deadline=None
)
