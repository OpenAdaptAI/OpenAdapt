"""Raw JSON boundaries are explicit across Pydantic's alternate entrypoints."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, TypeAdapter, field_validator
from pydantic_core import PydanticSerializationError

from openadapt_flow.ir import ActionKind, ApiBinding, BundleManifest, Step, Workflow
from openadapt_flow.job_authoring import (
    JobAuthoringBinding,
    validate_authoring_model_json,
)
from tests.test_job_authoring_schema3 import BINDING, authored


def _numeric_workflow():
    workflow = authored()
    workflow.steps.append(
        Step(
            id="read",
            intent="synthetic read",
            action=ActionKind.WAIT,
            api_binding=ApiBinding(
                method="GET",
                url_template="/records",
                body_template={"small": 1e-7, "large": 1e20, "zero": -0.0},
            ),
        )
    )
    return workflow


def _model(kind):
    if kind == "workflow":
        return _numeric_workflow()
    if kind == "manifest":
        return BundleManifest(schema_version=3)
    return JobAuthoringBinding.model_validate(BINDING)


@pytest.mark.parametrize("kind", ["workflow", "manifest", "binding"])
@pytest.mark.parametrize("entrypoint", ["adapter", "core", "base", "nested"])
def test_alternate_raw_json_requires_supported_parser(kind, entrypoint):
    model = _model(kind)
    model_type = type(model)
    wire = model.model_dump_json()
    assert model_type.model_validate_json(wire).model_dump() == model.model_dump()
    # A caller's metadata is not the process-local checked-byte scope.
    context = {"authoring_json_parse": True, "checked": True}
    with pytest.raises(ValueError, match="requires the supported model parser"):
        if entrypoint == "adapter":
            TypeAdapter(model_type).validate_json(wire, context=context)
        elif entrypoint == "core":
            model.__pydantic_validator__.validate_json(wire, context=context)
        elif entrypoint == "base":
            BaseModel.model_validate_json.__func__(model_type, wire, context=context)
        else:
            TypeAdapter(list[model_type]).validate_json(f"[{wire}]", context=context)


@pytest.mark.parametrize("kind", ["workflow", "manifest"])
@pytest.mark.parametrize(
    "entrypoint", ["adapter_json", "adapter_projection", "core", "base"]
)
def test_alternate_json_writers_cannot_choose_a_different_number_profile(
    kind, entrypoint
):
    model = _model(kind)
    assert type(model).model_validate_json(model.model_dump_json()).model_dump() == (
        model.model_dump(mode="json")
    )
    with pytest.raises(PydanticSerializationError, match="supported model writer"):
        if entrypoint == "adapter_json":
            TypeAdapter(type(model)).dump_json(model)
        elif entrypoint == "adapter_projection":
            TypeAdapter(type(model)).dump_python(model, mode="json")
        elif entrypoint == "core":
            model.__pydantic_serializer__.to_json(model)
        else:
            BaseModel.model_dump_json(model)
    # Returning Python values makes no raw-JSON numeric spelling promise.
    assert TypeAdapter(type(model)).dump_python(model) == model.model_dump()


def test_supported_model_json_keeps_pydantic_json_strict_semantics():
    workflow = _numeric_workflow()
    wire = workflow.model_dump_json()
    assert "1e-07" in wire and "1e+20" in wire and "-0.0" in wire
    assert (
        Workflow.model_validate_json(wire, strict=True).steps[0].action
        is ActionKind.WAIT
    )
    binding = JobAuthoringBinding.model_validate_json(json.dumps(BINDING), strict=True)
    assert binding.model_dump() == BINDING
    assert json.loads(TypeAdapter(JobAuthoringBinding).dump_json(binding)) == BINDING


def test_validation_and_projection_failures_do_not_leave_an_encoding_scope_active():
    workflow = _numeric_workflow()
    valid = workflow.model_dump_json()
    invalid = workflow.model_dump()
    invalid.pop("name")
    with pytest.raises(ValueError):
        Workflow.model_validate_json(json.dumps(invalid))
    with pytest.raises(ValueError, match="supported model parser"):
        TypeAdapter(Workflow).validate_json(valid)
    malformed = workflow.model_copy(update={"job_authoring": None})
    with pytest.raises(PydanticSerializationError):
        malformed.model_dump(mode="json")
    with pytest.raises(PydanticSerializationError, match="supported model writer"):
        TypeAdapter(Workflow).dump_json(workflow)


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_alternate_json_entrypoints_keep_their_original_profile(version):
    workflow = _numeric_workflow().model_copy(
        update={"schema_version": version, "job_authoring": None}
    )
    wire = workflow.model_dump_json()
    assert "1e-7" in wire and "1e-07" not in wire
    adapter = TypeAdapter(Workflow)
    assert adapter.dump_json(workflow).decode() == wire
    assert adapter.validate_json(wire).model_dump() == workflow.model_dump()
    assert BaseModel.model_validate_json.__func__(Workflow, wire).model_dump() == (
        workflow.model_dump()
    )


@pytest.mark.parametrize("version", ["3", "3.0", 3.0])
def test_manifest_cannot_coerce_an_unchecked_raw_profile_to_schema3(version):
    wire = json.dumps({"schema_version": version})
    for parse in [
        BundleManifest.model_validate_json,
        TypeAdapter(BundleManifest).validate_json,
    ]:
        with pytest.raises(ValueError):
            parse(wire)


def test_permissive_root_cannot_establish_a_checked_nested_json_scope():
    class Envelope(BaseModel):
        workflow: Workflow

    wire = '{"workflow":' + _numeric_workflow().model_dump_json() + "}"
    noncanonical = wire.replace("1e-07", "1e-7")
    for raw in (wire, noncanonical):
        with pytest.raises(ValueError, match="supported model parser"):
            validate_authoring_model_json(Envelope, raw)
    # Explicitly checking the entire raw document is sufficient for its nested
    # models' encoding, but still supplies no source verification/admission.
    checked = validate_authoring_model_json(Envelope, wire, strict_profile=True)
    assert checked.workflow.name == _numeric_workflow().name
    with pytest.raises(ValueError, match="not canonical"):
        validate_authoring_model_json(Envelope, noncanonical, strict_profile=True)

    class CheckedOuter(BaseModel):
        schema_version: int
        trigger: str
        workflow: Workflow

        @field_validator("trigger")
        @classmethod
        def check_nested_call(cls, value):
            # This legacy-root invocation must temporarily clear the parent's
            # checked scope, then restore it for the actual remaining fields.
            with pytest.raises(ValueError, match="supported model parser"):
                validate_authoring_model_json(Envelope, noncanonical)
            return value

    outer = (
        '{"schema_version":3,"trigger":"synthetic","workflow":'
        + _numeric_workflow().model_dump_json()
        + "}"
    )
    assert (
        validate_authoring_model_json(CheckedOuter, outer).workflow.schema_version == 3
    )
