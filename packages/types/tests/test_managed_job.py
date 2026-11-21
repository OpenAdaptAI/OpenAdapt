"""Invariants for the domain-neutral managed inbox, outbox, and MCP contracts."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types.managed_job import (
    MANAGED_INBOX_SCHEMA,
    MANAGED_MCP_TOOL_CATALOG,
    MANAGED_MCP_TOOL_NAMES,
    MANAGED_OUTBOX_SCHEMA,
    MANAGED_TOOL_RESULT_SCHEMA,
    ManagedActuationDecisionV1,
    ManagedActuationHeld,
    ManagedBoundToolCallV1,
    ManagedInboxV1,
    ManagedMcpToolCatalogV1,
    ManagedMcpToolNameV1,
    ManagedOutboxActionV1,
    ManagedOutboxV1,
    ManagedToolResultV1,
    ManagedToolStatusV1,
    ManagedToolUnknown,
    bind_managed_tool_call,
    bind_managed_tool_result,
    decide_managed_actuation,
    is_managed_verified_success,
    managed_mcp_tool_spec,
    managed_planner_status,
    managed_planner_visible_payload,
    parse_managed_inbox,
    require_managed_actuation_dispatch,
)


def _inbox_fields(**updates: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "subject_token": "tok_aaaaaaaa",
        "artifact_path": "intake/job_001.pdf",
        "source": "intake",
        "recorded_at": "2026-09-01T12:00:00Z",
    }
    fields.update(updates)
    return fields


def _outbox_fields(**updates: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "action": "attach_artifact",
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


def test_inbox_rejects_missing_subject_token() -> None:
    payload = _inbox_fields()
    del payload["subject_token"]
    with pytest.raises(ValidationError, match="subject_token"):
        parse_managed_inbox(payload)


def test_inbox_rejects_name_or_other_extra_fields() -> None:
    inbox = ManagedInboxV1.model_validate(_inbox_fields())
    assert inbox.schema_version == MANAGED_INBOX_SCHEMA
    payload = inbox.model_dump(mode="json")
    for field, value in {
        "name": "Jane Doe",
        "account_number": "0093211",
        "screenshot": "data:image/png;base64,secret",
        "ocr": "raw document text",
    }.items():
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ManagedInboxV1.model_validate({**payload, field: value})


@pytest.mark.parametrize(
    "token",
    ["Jane Doe", "record jd", "jd", "tok", ""],
)
def test_inbox_rejects_name_shaped_subject_token(token: str) -> None:
    with pytest.raises(ValidationError):
        parse_managed_inbox(_inbox_fields(subject_token=token))


@pytest.mark.parametrize(
    "path",
    [
        "/Users/jane/doc.pdf",
        "../secret.pdf",
        "C:\\Users\\Jane\\doc.pdf",
        "Jane Doe doc.pdf",
        "",
    ],
)
def test_inbox_rejects_unsafe_artifact_path(path: str) -> None:
    with pytest.raises(ValidationError):
        parse_managed_inbox(_inbox_fields(artifact_path=path))


def test_outbox_with_needs_human_refuses_dispatch() -> None:
    outbox = ManagedOutboxV1.model_validate(
        _outbox_fields(needs_human=True, schema_version=MANAGED_OUTBOX_SCHEMA)
    )
    assert decide_managed_actuation(outbox) is ManagedActuationDecisionV1.HOLD_FOR_HUMAN
    with pytest.raises(ManagedActuationHeld):
        require_managed_actuation_dispatch(outbox)
    with pytest.raises(ManagedActuationHeld):
        bind_managed_tool_call(
            ManagedMcpToolNameV1.RUN_ATTACH_ARTIFACT,
            _inbox_fields(),
            _outbox_fields(needs_human=True),
        )


def test_catalog_exposes_exactly_three_domain_neutral_tools() -> None:
    assert tuple(spec.name for spec in MANAGED_MCP_TOOL_CATALOG.tools) == (
        ManagedMcpToolNameV1.RUN_COLLECT,
        ManagedMcpToolNameV1.RUN_ATTACH_ARTIFACT,
        ManagedMcpToolNameV1.RUN_CREATE_TASK,
    )
    assert tuple(MANAGED_MCP_TOOL_NAMES) == (
        ManagedMcpToolNameV1.RUN_COLLECT,
        ManagedMcpToolNameV1.RUN_ATTACH_ARTIFACT,
        ManagedMcpToolNameV1.RUN_CREATE_TASK,
    )
    with pytest.raises(ManagedToolUnknown):
        managed_mcp_tool_spec("run_arbitrary")


def test_bind_call_and_result_preserve_subject_token_and_status_semantics() -> None:
    call = bind_managed_tool_call(
        ManagedMcpToolNameV1.RUN_CREATE_TASK,
        _inbox_fields(),
        _outbox_fields(action=ManagedOutboxActionV1.CREATE_TASK.value),
    )
    assert isinstance(call, ManagedBoundToolCallV1)

    verified = bind_managed_tool_result(call, ManagedToolStatusV1.VERIFIED)
    assert verified.schema_version == MANAGED_TOOL_RESULT_SCHEMA
    assert verified.subject_token == "tok_aaaaaaaa"
    assert verified.ok is True
    assert is_managed_verified_success(verified) is True
    assert managed_planner_status(verified) is ManagedToolStatusV1.VERIFIED
    assert managed_planner_visible_payload(verified) == {
        "tool": "run_create_task",
        "status": "VERIFIED",
        "subject_token": "tok_aaaaaaaa",
        "ok": True,
    }

    for non_success in (
        ManagedToolStatusV1.HALTED,
        ManagedToolStatusV1.RECONCILIATION_REQUIRED,
    ):
        result = bind_managed_tool_result(call, non_success)
        assert result.ok is False
        assert is_managed_verified_success(result) is False
        assert managed_planner_status(result) is non_success
        assert managed_planner_visible_payload(result)["ok"] is False


@pytest.mark.parametrize(
    ("model", "filename", "schema_version"),
    [
        (ManagedInboxV1, "managed-inbox-v1.json", MANAGED_INBOX_SCHEMA),
        (ManagedOutboxV1, "managed-outbox-v1.json", MANAGED_OUTBOX_SCHEMA),
        (
            ManagedToolResultV1,
            "managed-tool-result-v1.json",
            MANAGED_TOOL_RESULT_SCHEMA,
        ),
        (
            ManagedMcpToolCatalogV1,
            "managed-mcp-tools-v1.json",
            "openadapt.managed-mcp-tools/v1",
        ),
    ],
)
def test_exported_schemas_match_models_and_forbid_unconstrained_strings(
    model: type,
    filename: str,
    schema_version: str,
) -> None:
    shipped = json.loads(
        files("openadapt_types.schemas").joinpath(filename).read_text(encoding="utf-8")
    )
    assert shipped == model.model_json_schema()
    assert shipped["properties"]["schema_version"]["const"] == schema_version
    assert _unconstrained_string_paths(shipped) == []


def test_catalog_schema_and_model_reject_invalid_tool_combinations() -> None:
    shipped = json.loads(
        files("openadapt_types.schemas")
        .joinpath("managed-mcp-tools-v1.json")
        .read_text(encoding="utf-8")
    )
    prefix_items = shipped["properties"]["tools"]["prefixItems"]
    assert len(prefix_items) == 3
    defs = shipped["$defs"]
    expected = [
        ("run_collect", "collect", False),
        ("run_attach_artifact", "attach_artifact", True),
        ("run_create_task", "create_task", True),
    ]
    for item, (name, action, requires_outbox) in zip(
        prefix_items, expected, strict=True
    ):
        ref_name = item["$ref"].rsplit("/", 1)[-1]
        spec_schema = defs[ref_name]
        assert spec_schema["properties"]["name"]["const"] == name
        assert spec_schema["properties"]["action"]["const"] == action
        assert spec_schema["properties"]["requires_outbox"]["const"] is requires_outbox
        assert {"name", "action", "requires_outbox"} <= set(spec_schema["required"])

    valid_tools = MANAGED_MCP_TOOL_CATALOG.model_dump(mode="json")["tools"]
    invalid_catalogs = [
        # Repeated tool name in slot 1
        {
            "schema_version": "openadapt.managed-mcp-tools/v1",
            "tools": [valid_tools[0], valid_tools[0], valid_tools[2]],
        },
        # Wrong requires_outbox on run_collect
        {
            "schema_version": "openadapt.managed-mcp-tools/v1",
            "tools": [
                {**valid_tools[0], "requires_outbox": True},
                valid_tools[1],
                valid_tools[2],
            ],
        },
        # Mismatched action for run_attach_artifact
        {
            "schema_version": "openadapt.managed-mcp-tools/v1",
            "tools": [
                valid_tools[0],
                {**valid_tools[1], "action": "create_task"},
                valid_tools[2],
            ],
        },
        # Missing required fields in slot 0
        {
            "schema_version": "openadapt.managed-mcp-tools/v1",
            "tools": [{}, valid_tools[1], valid_tools[2]],
        },
    ]
    for bad in invalid_catalogs:
        with pytest.raises(ValidationError):
            ManagedMcpToolCatalogV1.model_validate(bad)
        # Verify the same fixture violates the published JSON Schema constraints
        slot_mismatches = 0
        for tool_obj, (name, action, requires_outbox) in zip(
            bad["tools"], expected, strict=True
        ):
            if (
                tool_obj.get("name") != name
                or tool_obj.get("action") != action
                or tool_obj.get("requires_outbox") is not requires_outbox
            ):
                slot_mismatches += 1
        assert slot_mismatches > 0
