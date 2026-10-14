"""Thin inbox, outbox, and MCP result contracts for clinic jobs.

The workbench owns schema, extract, review, and NL2SQL. This package does
not reimplement those. OpenAdapt is the hands: a compiled, admitted
program that halts on the wrong patient. Claude Code may call only the
three named tools, and only against admitted programs.

Identity on this wire is ``patient_token`` only. A name, MRN, or other
live identifier has no field to travel in. Wrong-patient halt is the
existing OpenAdapt identity gate. This adapter does not invent a visual
click.

SaMD: there is no tool that decides urgency or writes follow-up copy
into a chart. ``run_create_triage_task`` creates a task the clinic
already defined, and only after ``needs_human`` is false because a
human accepted.

Fax-first: ``run_attach_fax`` is attach plus task create. OCR stays
out of this package.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)

CLINIC_INBOX_SCHEMA: Literal["openadapt.clinic-inbox/v1"] = (
    "openadapt.clinic-inbox/v1"
)
CLINIC_OUTBOX_SCHEMA: Literal["openadapt.clinic-outbox/v1"] = (
    "openadapt.clinic-outbox/v1"
)
CLINIC_TOOL_RESULT_SCHEMA: Literal["openadapt.clinic-tool-result/v1"] = (
    "openadapt.clinic-tool-result/v1"
)
CLINIC_MCP_TOOLS_SCHEMA: Literal["openadapt.clinic-mcp-tools/v1"] = (
    "openadapt.clinic-mcp-tools/v1"
)

_OPAQUE_TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_SOURCE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_TIMESTAMP_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$"
)
# Relative POSIX path of opaque segments. Each segment starts with an
# alphanumeric or underscore. Dots only appear as an extension separator,
# so `..`, a leading slash, and a drive letter have no legal shape.
_ARTIFACT_PATH_PATTERN = (
    r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*(?:/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)*$"
)

_SUCCESS_STATUSES_NOTE = (
    "Only status VERIFIED is success. HALTED and RECONCILIATION_REQUIRED "
    "are not success and must not be summarized as success."
)


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClinicActuationHeld(ValueError):
    """Outbox still needs a human stamp; actuation must not dispatch."""


class ClinicToolUnknown(ValueError):
    """Caller named a tool this catalog does not admit."""


class ClinicOutboxActionV1(str, Enum):
    HARVEST = "harvest"
    ATTACH_FAX = "attach_fax"
    CREATE_TRIAGE_TASK = "create_triage_task"


class ClinicMcpToolNameV1(str, Enum):
    RUN_HARVEST = "run_harvest"
    RUN_ATTACH_FAX = "run_attach_fax"
    RUN_CREATE_TRIAGE_TASK = "run_create_triage_task"


class ClinicToolStatusV1(str, Enum):
    VERIFIED = "VERIFIED"
    HALTED = "HALTED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class ClinicActuationDecisionV1(str, Enum):
    DISPATCH = "dispatch"
    HOLD_FOR_HUMAN = "hold_for_human"


ACTION_TO_TOOL: dict[ClinicOutboxActionV1, ClinicMcpToolNameV1] = {
    ClinicOutboxActionV1.HARVEST: ClinicMcpToolNameV1.RUN_HARVEST,
    ClinicOutboxActionV1.ATTACH_FAX: ClinicMcpToolNameV1.RUN_ATTACH_FAX,
    ClinicOutboxActionV1.CREATE_TRIAGE_TASK: (
        ClinicMcpToolNameV1.RUN_CREATE_TRIAGE_TASK
    ),
}

TOOL_TO_ACTION: dict[ClinicMcpToolNameV1, ClinicOutboxActionV1] = {
    tool: action for action, tool in ACTION_TO_TOOL.items()
}

CLINIC_MCP_TOOL_NAMES: tuple[ClinicMcpToolNameV1, ...] = (
    ClinicMcpToolNameV1.RUN_HARVEST,
    ClinicMcpToolNameV1.RUN_ATTACH_FAX,
    ClinicMcpToolNameV1.RUN_CREATE_TRIAGE_TASK,
)

# Closed copy for a later MCP host. Not a clinical decision, not a
# follow-up sentence, not an extract/review/ask/audit tool.
CLINIC_MCP_TOOL_DESCRIPTIONS: dict[ClinicMcpToolNameV1, str] = {
    ClinicMcpToolNameV1.RUN_HARVEST: (
        "Run the admitted harvest program for this inbox job. Returns "
        "VERIFIED, HALTED, or RECONCILIATION_REQUIRED. Halt is halt."
    ),
    ClinicMcpToolNameV1.RUN_ATTACH_FAX: (
        "Attach the fax artifact and create the clinic-defined task. "
        "Returns VERIFIED, HALTED, or RECONCILIATION_REQUIRED. Halt is halt."
    ),
    ClinicMcpToolNameV1.RUN_CREATE_TRIAGE_TASK: (
        "Create the clinic-defined triage task after a human stamp. "
        "Returns VERIFIED, HALTED, or RECONCILIATION_REQUIRED. Halt is halt."
    ),
}

_TOOLS_THAT_REQUIRE_OUTBOX = frozenset(
    {
        ClinicMcpToolNameV1.RUN_ATTACH_FAX,
        ClinicMcpToolNameV1.RUN_CREATE_TRIAGE_TASK,
    }
)


class ClinicInboxV1(_StrictContract):
    """Workbench to OpenAdapt job. Token only; never a name."""

    schema_version: Literal[CLINIC_INBOX_SCHEMA] = CLINIC_INBOX_SCHEMA
    patient_token: StrictStr = Field(pattern=_OPAQUE_TOKEN_PATTERN)
    artifact_path: StrictStr = Field(
        pattern=_ARTIFACT_PATH_PATTERN,
        min_length=1,
        max_length=512,
    )
    source: StrictStr = Field(pattern=_SOURCE_PATTERN)
    recorded_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)

    @model_validator(mode="after")
    def _artifact_path_has_no_relative_segments(self) -> "ClinicInboxV1":
        for segment in self.artifact_path.split("/"):
            if segment in {".", ".."} or ".." in segment:
                raise ValueError("artifact_path must not contain relative segments")
        return self


class ClinicOutboxV1(_StrictContract):
    """Actuation intent. OpenAdapt types the template only after a human stamp."""

    schema_version: Literal[CLINIC_OUTBOX_SCHEMA] = CLINIC_OUTBOX_SCHEMA
    action: ClinicOutboxActionV1
    template: StrictStr = Field(pattern=_OPAQUE_TOKEN_PATTERN)
    needs_human: StrictBool


class ClinicToolResultV1(_StrictContract):
    """Typed MCP result. Status is the outcome; there is no success flag to set."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-openadapt-success-rule": _SUCCESS_STATUSES_NOTE,
        },
    )

    schema_version: Literal[CLINIC_TOOL_RESULT_SCHEMA] = (
        CLINIC_TOOL_RESULT_SCHEMA
    )
    tool: ClinicMcpToolNameV1
    status: ClinicToolStatusV1
    patient_token: StrictStr = Field(pattern=_OPAQUE_TOKEN_PATTERN)

    @property
    def ok(self) -> bool:
        return self.status is ClinicToolStatusV1.VERIFIED


class ClinicMcpToolSpecV1(_StrictContract):
    name: ClinicMcpToolNameV1
    action: ClinicOutboxActionV1
    requires_outbox: StrictBool
    destructive: Literal[True] = True
    read_only: Literal[False] = False

    @model_validator(mode="after")
    def _action_matches_name(self) -> "ClinicMcpToolSpecV1":
        expected = TOOL_TO_ACTION[self.name]
        if self.action is not expected:
            raise ValueError("tool name and action must match")
        required = self.name in _TOOLS_THAT_REQUIRE_OUTBOX
        if self.requires_outbox is not required:
            raise ValueError("requires_outbox must match the tool")
        return self


class ClinicMcpToolCatalogV1(_StrictContract):
    schema_version: Literal[CLINIC_MCP_TOOLS_SCHEMA] = CLINIC_MCP_TOOLS_SCHEMA
    tools: tuple[ClinicMcpToolSpecV1, ClinicMcpToolSpecV1, ClinicMcpToolSpecV1]

    @model_validator(mode="after")
    def _exactly_the_three_admitted_tools(self) -> "ClinicMcpToolCatalogV1":
        names = tuple(spec.name for spec in self.tools)
        if names != CLINIC_MCP_TOOL_NAMES:
            raise ValueError("catalog must be exactly the three admitted tools")
        return self


class ClinicBoundToolCallV1(_StrictContract):
    tool: ClinicMcpToolNameV1
    inbox: ClinicInboxV1
    outbox: ClinicOutboxV1 | None = None

    @model_validator(mode="after")
    def _outbox_matches_tool(self) -> "ClinicBoundToolCallV1":
        if self.tool in _TOOLS_THAT_REQUIRE_OUTBOX and self.outbox is None:
            raise ValueError(f"{self.tool.value} requires an outbox")
        if self.outbox is not None:
            expected = TOOL_TO_ACTION[self.tool]
            if self.outbox.action is not expected:
                raise ValueError("outbox action must match the tool")
        return self


CLINIC_MCP_TOOL_CATALOG = ClinicMcpToolCatalogV1(
    tools=(
        ClinicMcpToolSpecV1(
            name=ClinicMcpToolNameV1.RUN_HARVEST,
            action=ClinicOutboxActionV1.HARVEST,
            requires_outbox=False,
        ),
        ClinicMcpToolSpecV1(
            name=ClinicMcpToolNameV1.RUN_ATTACH_FAX,
            action=ClinicOutboxActionV1.ATTACH_FAX,
            requires_outbox=True,
        ),
        ClinicMcpToolSpecV1(
            name=ClinicMcpToolNameV1.RUN_CREATE_TRIAGE_TASK,
            action=ClinicOutboxActionV1.CREATE_TRIAGE_TASK,
            requires_outbox=True,
        ),
    )
)


def parse_clinic_inbox(payload: Mapping[str, Any]) -> ClinicInboxV1:
    """Parse a workbench inbox. Missing ``patient_token`` is a validation error."""

    return ClinicInboxV1.model_validate(payload)


def parse_clinic_outbox(payload: Mapping[str, Any]) -> ClinicOutboxV1:
    return ClinicOutboxV1.model_validate(payload)


def decide_actuation(outbox: ClinicOutboxV1) -> ClinicActuationDecisionV1:
    if outbox.needs_human:
        return ClinicActuationDecisionV1.HOLD_FOR_HUMAN
    return ClinicActuationDecisionV1.DISPATCH


def require_actuation_dispatch(outbox: ClinicOutboxV1) -> None:
    """Refuse to dispatch when the human stamp is still required."""

    if outbox.needs_human:
        raise ClinicActuationHeld(
            "outbox.needs_human is true; do not dispatch actuation"
        )


def is_verified_success(result: ClinicToolResultV1) -> bool:
    """True only for ``VERIFIED``. Halt and reconciliation are not success."""

    return result.status is ClinicToolStatusV1.VERIFIED


def planner_status(result: ClinicToolResultV1) -> ClinicToolStatusV1:
    """Return the typed status unchanged. Halt is halt."""

    return result.status


def planner_visible_payload(result: ClinicToolResultV1) -> dict[str, object]:
    """Planner-facing dict. ``ok`` is derived from status and cannot be set."""

    return {
        "tool": result.tool.value,
        "status": result.status.value,
        "patient_token": result.patient_token,
        "ok": is_verified_success(result),
    }


def clinic_mcp_tool_spec(name: ClinicMcpToolNameV1 | str) -> ClinicMcpToolSpecV1:
    label = name.value if isinstance(name, ClinicMcpToolNameV1) else name
    if not isinstance(label, str):
        raise ClinicToolUnknown("tool name is not an admitted clinic tool")
    try:
        tool = ClinicMcpToolNameV1(label)
    except ValueError as exc:
        raise ClinicToolUnknown(
            f"{label!r} is not an admitted clinic tool"
        ) from exc
    for spec in CLINIC_MCP_TOOL_CATALOG.tools:
        if spec.name is tool:
            return spec
    raise ClinicToolUnknown(f"{label!r} is not an admitted clinic tool")


def bind_clinic_tool_call(
    tool: ClinicMcpToolNameV1 | str,
    inbox: Mapping[str, Any],
    outbox: Mapping[str, Any] | None = None,
) -> ClinicBoundToolCallV1:
    """Validate inbox/outbox and refuse actuation that still needs a human."""

    spec = clinic_mcp_tool_spec(tool)
    parsed_inbox = parse_clinic_inbox(inbox)
    parsed_outbox: ClinicOutboxV1 | None
    if outbox is None:
        parsed_outbox = None
    else:
        parsed_outbox = parse_clinic_outbox(outbox)
        require_actuation_dispatch(parsed_outbox)
    return ClinicBoundToolCallV1(
        tool=spec.name,
        inbox=parsed_inbox,
        outbox=parsed_outbox,
    )


def bind_clinic_tool_result(
    call: ClinicBoundToolCallV1,
    status: ClinicToolStatusV1,
) -> ClinicToolResultV1:
    """Stamp the inbox token onto the result. Identity does not get rewritten."""

    return ClinicToolResultV1(
        tool=call.tool,
        status=status,
        patient_token=call.inbox.patient_token,
    )
