"""Invariants for the clinic inbox, outbox, and MCP result contracts."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types.clinic_job import (
    CLINIC_INBOX_SCHEMA,
    CLINIC_MCP_TOOL_CATALOG,
    CLINIC_MCP_TOOL_NAMES,
    CLINIC_OUTBOX_SCHEMA,
    CLINIC_TOOL_RESULT_SCHEMA,
    ClinicActuationDecisionV1,
    ClinicActuationHeld,
    ClinicBoundToolCallV1,
    ClinicInboxV1,
    ClinicMcpToolCatalogV1,
    ClinicMcpToolNameV1,
    ClinicOutboxActionV1,
    ClinicOutboxV1,
    ClinicToolResultV1,
    ClinicToolStatusV1,
    ClinicToolUnknown,
    bind_clinic_tool_call,
    bind_clinic_tool_result,
    clinic_mcp_tool_spec,
    decide_actuation,
    is_verified_success,
    parse_clinic_inbox,
    planner_status,
    planner_visible_payload,
    require_actuation_dispatch,
)


def _inbox_fields(**updates: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "patient_token": "tok_aaaaaaaa",
        "artifact_path": "fax/job_001.tif",
        "source": "fax",
        "recorded_at": "2026-09-01T12:00:00Z",
    }
    fields.update(updates)
    return fields


def _outbox_fields(**updates: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "action": "attach_fax",
        "template": "tmpl_attach01",
        "needs_human": False,
    }
    fields.update(updates)
    return fields


def _unconstrained_string_paths(schema: dict[str, object]) -> list[str]:
    unconstrained: list[str] = []

    def visit(node: object, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "string" and not (
                {"pattern", "const", "enum"} & set(node)
            ):
                unconstrained.append(path)
            for key, value in node.items():
                visit(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}/{index}")

    visit(schema, "")
    return unconstrained


def test_inbox_rejects_a_missing_patient_token() -> None:
    payload = _inbox_fields()
    del payload["patient_token"]
    with pytest.raises(ValidationError, match="patient_token"):
        parse_clinic_inbox(payload)


def test_inbox_rejects_a_name_or_other_identity_field() -> None:
    inbox = ClinicInboxV1.model_validate(_inbox_fields())
    assert inbox.schema_version == CLINIC_INBOX_SCHEMA
    payload = inbox.model_dump(mode="json")
    for field, value in {
        "patient_name": "Jane Doe",
        "name": "Jane Doe",
        "mrn": "0093211",
        "screenshot": "data:image/png;base64,secret",
        "ocr": "referral text",
    }.items():
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ClinicInboxV1.model_validate({**payload, field: value})


@pytest.mark.parametrize(
    "token",
    ["Jane Doe", "patient jd", "jd", "tok", ""],
)
def test_inbox_rejects_a_name_shaped_patient_token(token: str) -> None:
    with pytest.raises(ValidationError):
        parse_clinic_inbox(_inbox_fields(patient_token=token))


@pytest.mark.parametrize(
    "path",
    [
        "/Users/jane/fax.pdf",
        "../secret.tif",
        "C:\\Users\\Jane\\fax.pdf",
        "Jane Doe fax.pdf",
        "",
    ],
)
def test_inbox_rejects_a_path_that_could_carry_a_name(path: str) -> None:
    with pytest.raises(ValidationError):
        parse_clinic_inbox(_inbox_fields(artifact_path=path))


def test_outbox_with_needs_human_true_must_not_dispatch_actuation() -> None:
    outbox = ClinicOutboxV1.model_validate(
        _outbox_fields(needs_human=True, schema_version=CLINIC_OUTBOX_SCHEMA)
    )
    assert decide_actuation(outbox) is ClinicActuationDecisionV1.HOLD_FOR_HUMAN
    with pytest.raises(ClinicActuationHeld, match="do not dispatch actuation"):
        require_actuation_dispatch(outbox)
    with pytest.raises(ClinicActuationHeld, match="do not dispatch actuation"):
        bind_clinic_tool_call(
            "run_attach_fax",
            _inbox_fields(),
            _outbox_fields(needs_human=True),
        )


def test_outbox_without_a_human_stamp_may_bind_an_admitted_tool() -> None:
    call = bind_clinic_tool_call(
        ClinicMcpToolNameV1.RUN_ATTACH_FAX,
        _inbox_fields(),
        _outbox_fields(needs_human=False),
    )
    assert call.outbox is not None
    assert decide_actuation(call.outbox) is ClinicActuationDecisionV1.DISPATCH
    require_actuation_dispatch(call.outbox)


def test_halt_must_not_map_to_success() -> None:
    call = bind_clinic_tool_call(
        "run_harvest",
        _inbox_fields(),
    )
    halted = bind_clinic_tool_result(call, ClinicToolStatusV1.HALTED)
    reconciled = bind_clinic_tool_result(
        call, ClinicToolStatusV1.RECONCILIATION_REQUIRED
    )
    verified = bind_clinic_tool_result(call, ClinicToolStatusV1.VERIFIED)

    assert halted.status is ClinicToolStatusV1.HALTED
    assert halted.ok is False
    assert is_verified_success(halted) is False
    assert planner_status(halted) is ClinicToolStatusV1.HALTED
    assert planner_visible_payload(halted)["ok"] is False
    assert planner_visible_payload(halted)["status"] == "HALTED"

    assert reconciled.ok is False
    assert is_verified_success(reconciled) is False
    assert planner_visible_payload(reconciled)["status"] == (
        "RECONCILIATION_REQUIRED"
    )

    assert verified.ok is True
    assert is_verified_success(verified) is True
    assert planner_visible_payload(verified)["status"] == "VERIFIED"


def test_tool_result_rejects_a_success_flag_that_could_launder_halt() -> None:
    result = ClinicToolResultV1(
        tool=ClinicMcpToolNameV1.RUN_HARVEST,
        status=ClinicToolStatusV1.HALTED,
        patient_token="tok_aaaaaaaa",
    )
    payload = result.model_dump(mode="json")
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClinicToolResultV1.model_validate({**payload, "success": True})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClinicToolResultV1.model_validate({**payload, "ok": True})


def test_catalog_is_exactly_the_three_admitted_tools() -> None:
    names = tuple(spec.name for spec in CLINIC_MCP_TOOL_CATALOG.tools)
    assert names == CLINIC_MCP_TOOL_NAMES
    assert names == (
        ClinicMcpToolNameV1.RUN_HARVEST,
        ClinicMcpToolNameV1.RUN_ATTACH_FAX,
        ClinicMcpToolNameV1.RUN_CREATE_TRIAGE_TASK,
    )
    assert clinic_mcp_tool_spec("run_harvest").requires_outbox is False
    assert clinic_mcp_tool_spec("run_attach_fax").requires_outbox is True
    assert clinic_mcp_tool_spec("run_create_triage_task").requires_outbox is True


@pytest.mark.parametrize(
    "name",
    [
        "run_decide_urgency",
        "run_write_followup",
        "extract",
        "review",
        "ask",
        "audit",
        "schema_pack",
    ],
)
def test_unknown_or_clinical_decision_tools_are_refused(name: str) -> None:
    with pytest.raises(ClinicToolUnknown, match="not an admitted clinic tool"):
        clinic_mcp_tool_spec(name)


def test_attach_and_triage_require_an_outbox() -> None:
    with pytest.raises(ValidationError, match="requires an outbox"):
        bind_clinic_tool_call("run_attach_fax", _inbox_fields())
    with pytest.raises(ValidationError, match="requires an outbox"):
        bind_clinic_tool_call("run_create_triage_task", _inbox_fields())


def test_outbox_action_must_match_the_tool() -> None:
    with pytest.raises(ValidationError, match="outbox action must match"):
        bind_clinic_tool_call(
            "run_create_triage_task",
            _inbox_fields(),
            _outbox_fields(action="attach_fax", needs_human=False),
        )


def test_template_rejects_follow_up_copy() -> None:
    with pytest.raises(ValidationError):
        ClinicOutboxV1.model_validate(
            _outbox_fields(template="see in two weeks")
        )
    payload = ClinicOutboxV1.model_validate(_outbox_fields()).model_dump(
        mode="json"
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClinicOutboxV1.model_validate(
            {**payload, "follow_up_copy": "see in two weeks"}
        )


def test_result_keeps_the_inbox_patient_token() -> None:
    call = bind_clinic_tool_call("run_harvest", _inbox_fields())
    result = bind_clinic_tool_result(call, ClinicToolStatusV1.VERIFIED)
    assert result.patient_token == call.inbox.patient_token
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClinicToolResultV1.model_validate(
            {
                **result.model_dump(mode="json"),
                "patient_name": "Jane Doe",
            }
        )


def test_bound_call_cannot_rewrite_identity_on_the_result() -> None:
    call = ClinicBoundToolCallV1(
        tool=ClinicMcpToolNameV1.RUN_HARVEST,
        inbox=ClinicInboxV1.model_validate(_inbox_fields()),
    )
    result = bind_clinic_tool_result(call, ClinicToolStatusV1.HALTED)
    assert result.patient_token == "tok_aaaaaaaa"
    with pytest.raises(ValidationError):
        ClinicToolResultV1(
            tool=call.tool,
            status=ClinicToolStatusV1.HALTED,
            patient_token="Jane Doe",
        )


@pytest.mark.parametrize(
    "model,filename,schema_version",
    [
        (ClinicInboxV1, "clinic-inbox-v1.json", CLINIC_INBOX_SCHEMA),
        (ClinicOutboxV1, "clinic-outbox-v1.json", CLINIC_OUTBOX_SCHEMA),
        (
            ClinicToolResultV1,
            "clinic-tool-result-v1.json",
            CLINIC_TOOL_RESULT_SCHEMA,
        ),
        (ClinicMcpToolCatalogV1, "clinic-mcp-tools-v1.json", None),
    ],
)
def test_packaged_json_schemas_are_strict_and_match_the_models(
    model: type[ClinicInboxV1]
    | type[ClinicOutboxV1]
    | type[ClinicToolResultV1]
    | type[ClinicMcpToolCatalogV1],
    filename: str,
    schema_version: str | None,
) -> None:
    schema = model.model_json_schema()
    assert schema["additionalProperties"] is False
    assert _unconstrained_string_paths(schema) == []
    encoded = json.dumps(schema).lower()
    for term in (
        "screenshot",
        "ocr",
        "yolo",
        "mrn",
        "patient_name",
        "schema_pack",
        "mockmed",
    ):
        assert term not in encoded
    packaged = files("openadapt_types.schemas").joinpath(filename)
    assert json.loads(packaged.read_text()) == schema
    if schema_version is not None:
        assert schema_version in json.dumps(schema)


def test_tool_result_schema_states_that_halt_is_not_success() -> None:
    schema = ClinicToolResultV1.model_json_schema()
    assert "not success" in schema["x-openadapt-success-rule"].lower()
    assert "HALTED" in json.dumps(schema)


def test_catalog_refuses_a_fourth_tool() -> None:
    harvest, attach, triage = CLINIC_MCP_TOOL_CATALOG.tools
    with pytest.raises(ValidationError, match="exactly the three admitted"):
        ClinicMcpToolCatalogV1(tools=(harvest, attach, harvest))
    assert attach.action is ClinicOutboxActionV1.ATTACH_FAX
    assert triage.action is ClinicOutboxActionV1.CREATE_TRIAGE_TASK
