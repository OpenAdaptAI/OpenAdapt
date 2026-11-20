"""Typed job identity changes semantic authority, never grants it."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from openadapt_flow import bundle_validation as bundles
from openadapt_flow.ir import Workflow
from openadapt_flow.job_authoring import (
    JOB_AUTHORING_SCHEMA,
    JobAuthoringBinding,
    JobAuthoringError,
    match_job_authoring,
    private_job_intent_sha256,
    require_supported_workflow,
    verified_job_authoring,
)
from openadapt_flow.runtime.replayer import Replayer

BINDING = {
    "schema": JOB_AUTHORING_SCHEMA,
    "job_id": "11111111-1111-4111-8111-111111111111",
    "org_id": "22222222-2222-4222-8222-222222222222",
    "owner_principal_id": "33333333-3333-4333-8333-333333333333",
    "intent_sha256": "a" * 64,
}


def authored() -> Workflow:
    return Workflow(
        name="synthetic bound program",
        schema_version=3,
        created_at="2026-01-01T00:00:00+00:00",
        job_authoring=JobAuthoringBinding.model_validate(BINDING),
        params={"job_authoring": "ordinary semantic user data"},
    )


def test_closed_wire_roundtrip_and_independent_v3_canonical_projection():
    binding = JobAuthoringBinding.model_validate(BINDING)
    assert binding.model_dump() == BINDING
    assert JobAuthoringBinding.model_validate(binding).model_dump() == BINDING
    workflow = authored()
    wire = workflow.model_dump(mode="json")
    assert wire["job_authoring"] == BINDING
    assert Workflow.model_validate_json(workflow.model_dump_json()).model_dump() == wire
    raw = json.loads(bundles.source_canonical_payload(wire, {}))
    assert raw["workflow"]["job_authoring"] == BINDING
    assert raw["workflow"]["params"]["job_authoring"] == "ordinary semantic user data"
    assert (
        bundles.compute_content_digest(workflow, {})
        == __import__("hashlib")
        .sha256(bundles.source_canonical_payload(wire, {}))
        .hexdigest()
    )


@pytest.mark.parametrize("field", list(BINDING))
def test_every_binding_field_is_required(field):
    missing = {k: v for k, v in BINDING.items() if k != field}
    with pytest.raises(ValueError):
        JobAuthoringBinding.model_validate(missing)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", "openadapt.job-authoring-binding/v2"),
        ("job_id", "11111111111141118111111111111111"),
        ("org_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
        ("owner_principal_id", " 33333333-3333-4333-8333-333333333333"),
        ("intent_sha256", "sha256:" + "a" * 64),
        ("intent_sha256", "A" * 64),
        ("intent_sha256", "a" * 64 + "\n"),
        ("job_id", 1),
        ("org_id", True),
        ("owner_principal_id", None),
    ],
)
def test_invalid_binding_is_closed_and_does_not_echo_identity(field, value):
    with pytest.raises(ValueError) as error:
        JobAuthoringBinding.model_validate({**BINDING, field: value})
    assert BINDING["owner_principal_id"] not in str(error.value)


def test_unknown_binding_and_workflow_fields_refuse():
    with pytest.raises(ValueError):
        JobAuthoringBinding.model_validate({**BINDING, "approved": True})
    with pytest.raises(ValueError):
        Workflow.model_validate({**authored().model_dump(), "approved": True})


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_absence_is_omitted_and_nonnull_binding_never_drops(version):
    plain = Workflow(name="legacy", schema_version=version)
    assert "job_authoring" not in plain.model_dump()
    assert (
        "job_authoring"
        not in Workflow.model_validate(
            {**plain.model_dump(), "job_authoring": None}
        ).model_dump()
    )
    with pytest.raises(ValueError):
        Workflow.model_validate({**plain.model_dump(), "job_authoring": BINDING})
    with pytest.raises(ValueError):
        Workflow.model_validate_json(
            json.dumps({**plain.model_dump(), "job_authoring": BINDING})
        )


@pytest.mark.parametrize("version", [True, False, 2.0, "3", 0, 4])
def test_direct_model_version_check_matches_loader(version):
    with pytest.raises(ValueError):
        Workflow.model_validate({"name": "invalid", "schema_version": version})


@pytest.mark.parametrize(
    "version,binding", [(1, BINDING), (2, BINDING), (3, None), (4, BINDING)]
)
@pytest.mark.parametrize("resume", [None, 0])
def test_preconstructed_and_mutated_models_refuse_before_runtime_output(
    tmp_path, version, binding, resume
):
    workflow = Workflow.model_construct(
        name="invalid", schema_version=version, job_authoring=binding
    )
    replayer = Replayer.__new__(Replayer)
    with pytest.raises(ValueError):
        replayer.run(
            workflow, bundle_dir=tmp_path, run_dir=tmp_path / "run", resume_from=resume
        )
    assert not (tmp_path / "run").exists()
    with pytest.raises(ValueError):
        workflow.model_dump()


def test_old_consumer_shaped_object_cannot_claim_schema3_support(tmp_path):
    old = SimpleNamespace(
        schema_version=3,
        job_authoring=JobAuthoringBinding.model_validate(BINDING),
        manifest=None,
    )
    with pytest.raises(JobAuthoringError):
        require_supported_workflow(old, for_execution=True, bundle_dir=tmp_path)


def test_actual_seal_load_and_expected_identity_match(tmp_path):
    workflow = authored()
    workflow.save(tmp_path / "bundle")
    assert workflow.manifest.schema_version == 3
    loaded = Workflow.load(tmp_path / "bundle")
    assert (
        verified_job_authoring(
            loaded, tmp_path / "bundle", expected_binding=BINDING
        ).model_dump()
        == BINDING
    )
    assert loaded.manifest.provenance.certified is False
    audit_only = Workflow.load(tmp_path / "bundle", verify_integrity=False)
    with pytest.raises(JobAuthoringError):
        verified_job_authoring(audit_only, tmp_path / "bundle")
    wrong = {**BINDING, "owner_principal_id": "44444444-4444-4444-8444-444444444444"}
    with pytest.raises(JobAuthoringError):
        verified_job_authoring(loaded, tmp_path / "bundle", expected_binding=wrong)
    with pytest.raises(JobAuthoringError):
        verified_job_authoring(loaded, tmp_path / "other")
    with pytest.raises(JobAuthoringError):
        verified_job_authoring(
            Workflow.model_validate(loaded.model_dump()), tmp_path / "bundle"
        )


@pytest.mark.parametrize(
    "field", ["job_id", "org_id", "owner_principal_id", "intent_sha256"]
)
def test_binding_mutation_changes_digest_and_refuses_old_loaded_authority(
    tmp_path, field
):
    workflow = authored()
    workflow.save(tmp_path / "source")
    source = Workflow.load(tmp_path / "source")
    replacement = (
        "b" * 64 if field == "intent_sha256" else "44444444-4444-4444-8444-444444444444"
    )
    binding = JobAuthoringBinding.model_validate({**BINDING, field: replacement})
    changed = source.model_copy(update={"job_authoring": binding})
    assert bundles.compute_content_digest(changed, {}) != source.manifest.content_digest
    with pytest.raises(bundles.BundleIntegrityError):
        verified_job_authoring(changed, tmp_path / "source")
    derivative = bundles.derive_authored_workflow(
        source, update={"job_authoring": binding}
    )
    assert derivative.manifest.provenance.certified is False
    derivative.save(tmp_path / "derived")
    admitted_source = Workflow.load(tmp_path / "derived")
    assert (
        verified_job_authoring(admitted_source, tmp_path / "derived").model_dump()[
            field
        ]
        == replacement
    )
    assert admitted_source.manifest.content_digest != source.manifest.content_digest
    assert Workflow.load(tmp_path / "source").job_authoring.model_dump() == BINDING


def test_intent_hash_uses_exact_bounded_utf8_without_normalizing():
    import hashlib

    assert (
        private_job_intent_sha256("hello")
        == hashlib.sha256(b"openadapt.private-job-intent/v1\0hello").hexdigest()
    )
    assert private_job_intent_sha256("é") != private_job_intent_sha256("e\u0301")
    assert private_job_intent_sha256(" hello") != private_job_intent_sha256("hello")
    assert private_job_intent_sha256("x" * 8192)
    for value in (b"\xff", "x" * 8193, "\ud800"):
        with pytest.raises(JobAuthoringError):
            private_job_intent_sha256(value)
    assert match_job_authoring(BINDING, dict(BINDING)).model_dump() == BINDING


@pytest.mark.parametrize(
    "update",
    [
        {"approved": True},
        {"binding_schema": "wrong"},
        {"owner_principal_id": "invalid"},
    ],
)
def test_binding_copy_cannot_bypass_validation_or_serialization(update):
    binding = JobAuthoringBinding.model_validate(BINDING).model_copy(update=update)
    with pytest.raises(ValueError):
        JobAuthoringBinding.model_validate(binding)
    with pytest.raises(ValueError):
        binding.model_dump()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_schema3_nonfinite_values_refuse_before_parser_and_serializer_loss(value):
    from openadapt_flow.ir import ActionKind, ApiBinding, Step

    step = Step(
        id="write",
        intent="synthetic request",
        action=ActionKind.CLICK,
        api_binding=ApiBinding(
            url_template="/records", body_template={"amount": value}
        ),
    )
    with pytest.raises(ValueError):
        Workflow(name="bad", schema_version=3, job_authoring=BINDING, steps=[step])
    workflow = authored()
    step.api_binding.body_template["amount"] = 1.0
    workflow.steps.append(step)
    step.api_binding.body_template["amount"] = value
    with pytest.raises(ValueError):
        workflow.model_dump()
    with pytest.raises(ValueError):
        workflow.model_dump_json()
    with pytest.raises(ValueError):
        bundles.compute_content_digest(workflow, {})


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":3,"schema_version":2,"name":"invalid","job_authoring":null}',
        '{"schema_version":3,"name":"invalid","name":"ambiguous"}',
        '{"schema_version":3,"name":"invalid","rate":1e-7}',
    ],
)
def test_schema3_json_cannot_hide_duplicate_fields_or_noncanonical_numbers(payload):
    with pytest.raises(ValueError):
        Workflow.model_validate_json(payload)


def test_exact_binding_json_cannot_repeat_an_identity():
    payload = json.dumps(BINDING).replace(
        '"job_id":', '"job_id":"44444444-4444-4444-8444-444444444444","job_id":', 1
    )
    with pytest.raises(ValueError):
        JobAuthoringBinding.model_validate_json(payload)


def test_schema3_writer_and_loader_share_number_lexemes(tmp_path):
    from openadapt_flow.ir import ActionKind, ApiBinding, Step

    workflow = authored()
    workflow.steps.append(
        Step(
            id="read",
            intent="synthetic read",
            action=ActionKind.WAIT,
            api_binding=ApiBinding(
                method="GET",
                url_template="/records",
                body_template={
                    "small": 1e-7,
                    "large": 1e20,
                    "integer": 1,
                    "float": 1.0,
                    "zero": -0.0,
                },
            ),
        )
    )
    wire = workflow.model_dump_json()
    assert "1e-07" in wire and "1e+20" in wire
    assert '"float":1.0' in wire and '"integer":1' in wire and '"zero":-0.0' in wire
    assert Workflow.model_validate_json(wire).model_dump() == workflow.model_dump()
    workflow.save(tmp_path / "numeric")
    loaded = Workflow.load(tmp_path / "numeric")
    assert verified_job_authoring(loaded, tmp_path / "numeric").model_dump() == BINDING
    raw = json.loads((tmp_path / "numeric" / "workflow.json").read_text())
    assert (
        __import__("hashlib")
        .sha256(bundles.source_canonical_payload(raw, {}))
        .hexdigest()
        == loaded.manifest.content_digest
    )


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_canonical_and_migration_helpers_cannot_discard_invalid_job_data(version):
    raw = {
        "name": "bad",
        "schema_version": version,
        "job_authoring": BINDING if version != 3 else None,
    }
    for function in (
        bundles.source_canonical_payload,
        lambda value, files: bundles.migrate_bundle_dict(value),
    ):
        with pytest.raises(bundles.BundleSchemaError):
            function(raw, {})
    workflow = Workflow.model_construct(
        name="bad", schema_version=version, job_authoring=raw["job_authoring"]
    )
    assert not bundles.validate_workflow(workflow).ok


def test_new_canonical_vectors_roundtrip_without_updating_legacy_vectors():
    import hashlib
    from pathlib import Path

    vectors = json.loads(
        (
            Path(__file__).parent / "fixtures" / "job-authoring-v3" / "vectors.json"
        ).read_text()
    )
    for vector in vectors:
        workflow = Workflow.model_validate(vector["workflow"])
        assert (
            bundles.source_canonical_payload(vector["workflow"], vector["files"])
            == vector["canonical_utf8"].encode()
        )
        assert (
            bundles.compute_content_digest(workflow, vector["files"])
            == vector["content_digest"]
        )
        assert (
            hashlib.sha256(vector["canonical_utf8"].encode()).hexdigest()
            == vector["content_digest"]
        )
        assert workflow.model_dump_json() == vector["workflow_json"]
        assert (
            Workflow.model_validate_json(vector["workflow_json"]).model_dump()
            == workflow.model_dump()
        )


def test_loader_rejects_ambiguous_schema3_transport_before_seal_checks(tmp_path):
    payload = (
        '{"schema_version":3,"schema_version":2,"name":"invalid","job_authoring":null}'
    )
    (tmp_path / "workflow.json").write_text(payload)
    with pytest.raises(bundles.BundleSchemaError):
        Workflow.load(tmp_path)
