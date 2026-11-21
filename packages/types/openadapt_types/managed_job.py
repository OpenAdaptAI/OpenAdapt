"""Thin inbox, outbox, and MCP result contracts for managed jobs.

An upstream workbench owns extraction, review, and planning. This package does
not reimplement those. OpenAdapt is the execution layer: a compiled, admitted
program that halts on target identity mismatch. An agent may call only the
named tools in the catalog, and only against admitted programs.

Identity on this wire is ``subject_token`` only. A personal name, account
number, or other live identifier has no field to travel in. Wrong-record halt
is the existing OpenAdapt identity gate. This adapter does not invent a visual
click.

There is no tool that makes domain decisions or writes unreviewed free-text
copy into a system of record. ``run_create_task`` creates a task the
organization already defined, and only after ``needs_human`` is false because
a human approved it.
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

MANAGED_INBOX_SCHEMA: Literal["openadapt.managed-inbox/v1"] = (
    "openadapt.managed-inbox/v1"
)
MANAGED_OUTBOX_SCHEMA: Literal["openadapt.managed-outbox/v1"] = (
    "openadapt.managed-outbox/v1"
)
MANAGED_TOOL_RESULT_SCHEMA: Literal["openadapt.managed-tool-result/v1"] = (
    "openadapt.managed-tool-result/v1"
)
MANAGED_MCP_TOOLS_SCHEMA: Literal["openadapt.managed-mcp-tools/v1"] = (
    "openadapt.managed-mcp-tools/v1"
)

_OPAQUE_TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_SOURCE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$"
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


class ManagedActuationHeld(ValueError):
    """Outbox still needs a human approval; actuation must not dispatch."""


class ManagedToolUnknown(ValueError):
    """Caller named a tool this catalog does not admit."""


class ManagedOutboxActionV1(str, Enum):
    COLLECT = "collect"
    ATTACH_ARTIFACT = "attach_artifact"
    CREATE_TASK = "create_task"


class ManagedMcpToolNameV1(str, Enum):
    RUN_COLLECT = "run_collect"
    RUN_ATTACH_ARTIFACT = "run_attach_artifact"
    RUN_CREATE_TASK = "run_create_task"


class ManagedToolStatusV1(str, Enum):
    VERIFIED = "VERIFIED"
    HALTED = "HALTED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class ManagedActuationDecisionV1(str, Enum):
    DISPATCH = "dispatch"
    HOLD_FOR_HUMAN = "hold_for_human"


MANAGED_ACTION_TO_TOOL: dict[ManagedOutboxActionV1, ManagedMcpToolNameV1] = {
    ManagedOutboxActionV1.COLLECT: ManagedMcpToolNameV1.RUN_COLLECT,
    ManagedOutboxActionV1.ATTACH_ARTIFACT: ManagedMcpToolNameV1.RUN_ATTACH_ARTIFACT,
    ManagedOutboxActionV1.CREATE_TASK: ManagedMcpToolNameV1.RUN_CREATE_TASK,
}

MANAGED_TOOL_TO_ACTION: dict[ManagedMcpToolNameV1, ManagedOutboxActionV1] = {
    tool: action for action, tool in MANAGED_ACTION_TO_TOOL.items()
}

MANAGED_MCP_TOOL_NAMES: tuple[ManagedMcpToolNameV1, ...] = (
    ManagedMcpToolNameV1.RUN_COLLECT,
    ManagedMcpToolNameV1.RUN_ATTACH_ARTIFACT,
    ManagedMcpToolNameV1.RUN_CREATE_TASK,
)

# Closed copy for an MCP host. Not a domain decision, not free-text copy,
# not an extract/review/ask/audit tool.
MANAGED_MCP_TOOL_DESCRIPTIONS: dict[ManagedMcpToolNameV1, str] = {
    ManagedMcpToolNameV1.RUN_COLLECT: (
        "Run the admitted collect program for this inbox job. Returns "
        "VERIFIED, HALTED, or RECONCILIATION_REQUIRED. Halt is halt."
    ),
    ManagedMcpToolNameV1.RUN_ATTACH_ARTIFACT: (
        "Attach the artifact and create the organization-defined task. "
        "Returns VERIFIED, HALTED, or RECONCILIATION_REQUIRED. Halt is halt."
    ),
    ManagedMcpToolNameV1.RUN_CREATE_TASK: (
        "Create the organization-defined task after a human approval. "
        "Returns VERIFIED, HALTED, or RECONCILIATION_REQUIRED. Halt is halt."
    ),
}

_TOOLS_THAT_REQUIRE_OUTBOX = frozenset(
    {
        ManagedMcpToolNameV1.RUN_ATTACH_ARTIFACT,
        ManagedMcpToolNameV1.RUN_CREATE_TASK,
    }
)


class ManagedInboxV1(_StrictContract):
    """Workbench to OpenAdapt job. Opaque token only; never a live name."""

    schema_version: Literal["openadapt.managed-inbox/v1"] = MANAGED_INBOX_SCHEMA
    subject_token: StrictStr = Field(pattern=_OPAQUE_TOKEN_PATTERN)
    artifact_path: StrictStr = Field(
        pattern=_ARTIFACT_PATH_PATTERN,
        min_length=1,
        max_length=512,
    )
    source: StrictStr = Field(pattern=_SOURCE_PATTERN)
    recorded_at: StrictStr = Field(pattern=_TIMESTAMP_PATTERN)

    @model_validator(mode="after")
    def _artifact_path_has_no_relative_segments(self) -> "ManagedInboxV1":
        for segment in self.artifact_path.split("/"):
            if segment in {".", ".."} or ".." in segment:
                raise ValueError("artifact_path must not contain relative segments")
        return self


class ManagedOutboxV1(_StrictContract):
    """Actuation intent. OpenAdapt applies the template only after a human approval."""

    schema_version: Literal["openadapt.managed-outbox/v1"] = MANAGED_OUTBOX_SCHEMA
    action: ManagedOutboxActionV1
    template: StrictStr = Field(pattern=_OPAQUE_TOKEN_PATTERN)
    needs_human: StrictBool


class ManagedToolResultV1(_StrictContract):
    """Typed MCP result. Status is the outcome; there is no success flag to set."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "x-openadapt-success-rule": _SUCCESS_STATUSES_NOTE,
        },
    )

    schema_version: Literal["openadapt.managed-tool-result/v1"] = (
        MANAGED_TOOL_RESULT_SCHEMA
    )
    tool: ManagedMcpToolNameV1
    status: ManagedToolStatusV1
    subject_token: StrictStr = Field(pattern=_OPAQUE_TOKEN_PATTERN)

    @property
    def ok(self) -> bool:
        return self.status is ManagedToolStatusV1.VERIFIED


class ManagedMcpToolSpecV1(_StrictContract):
    name: ManagedMcpToolNameV1
    action: ManagedOutboxActionV1
    requires_outbox: StrictBool
    destructive: Literal[True] = True
    read_only: Literal[False] = False

    @model_validator(mode="after")
    def _action_matches_name(self) -> "ManagedMcpToolSpecV1":
        expected = MANAGED_TOOL_TO_ACTION[self.name]
        if self.action is not expected:
            raise ValueError("tool name and action must match")
        required = self.name in _TOOLS_THAT_REQUIRE_OUTBOX
        if self.requires_outbox is not required:
            raise ValueError("requires_outbox must match the tool")
        return self


class ManagedMcpToolCatalogV1(_StrictContract):
    schema_version: Literal["openadapt.managed-mcp-tools/v1"] = MANAGED_MCP_TOOLS_SCHEMA
    tools: tuple[ManagedMcpToolSpecV1, ManagedMcpToolSpecV1, ManagedMcpToolSpecV1]

    @model_validator(mode="after")
    def _exactly_the_three_admitted_tools(self) -> "ManagedMcpToolCatalogV1":
        names = tuple(spec.name for spec in self.tools)
        if names != MANAGED_MCP_TOOL_NAMES:
            raise ValueError("catalog must be exactly the three admitted tools")
        return self


class ManagedBoundToolCallV1(_StrictContract):
    tool: ManagedMcpToolNameV1
    inbox: ManagedInboxV1
    outbox: ManagedOutboxV1 | None = None

    @model_validator(mode="after")
    def _outbox_matches_tool(self) -> "ManagedBoundToolCallV1":
        if self.tool in _TOOLS_THAT_REQUIRE_OUTBOX and self.outbox is None:
            raise ValueError(f"{self.tool.value} requires an outbox")
        if self.outbox is not None:
            expected = MANAGED_TOOL_TO_ACTION[self.tool]
            if self.outbox.action is not expected:
                raise ValueError("outbox action must match the tool")
        return self


MANAGED_MCP_TOOL_CATALOG = ManagedMcpToolCatalogV1(
    tools=(
        ManagedMcpToolSpecV1(
            name=ManagedMcpToolNameV1.RUN_COLLECT,
            action=ManagedOutboxActionV1.COLLECT,
            requires_outbox=False,
        ),
        ManagedMcpToolSpecV1(
            name=ManagedMcpToolNameV1.RUN_ATTACH_ARTIFACT,
            action=ManagedOutboxActionV1.ATTACH_ARTIFACT,
            requires_outbox=True,
        ),
        ManagedMcpToolSpecV1(
            name=ManagedMcpToolNameV1.RUN_CREATE_TASK,
            action=ManagedOutboxActionV1.CREATE_TASK,
            requires_outbox=True,
        ),
    )
)


def parse_managed_inbox(payload: Mapping[str, Any]) -> ManagedInboxV1:
    """Parse a workbench inbox. Missing ``subject_token`` is a validation error."""

    return ManagedInboxV1.model_validate(payload)


def parse_managed_outbox(payload: Mapping[str, Any]) -> ManagedOutboxV1:
    return ManagedOutboxV1.model_validate(payload)


def decide_managed_actuation(outbox: ManagedOutboxV1) -> ManagedActuationDecisionV1:
    if outbox.needs_human:
        return ManagedActuationDecisionV1.HOLD_FOR_HUMAN
    return ManagedActuationDecisionV1.DISPATCH


def require_managed_actuation_dispatch(outbox: ManagedOutboxV1) -> None:
    """Refuse to dispatch when human approval is still required."""

    if outbox.needs_human:
        raise ManagedActuationHeld(
            "outbox.needs_human is true; do not dispatch actuation"
        )


def is_managed_verified_success(result: ManagedToolResultV1) -> bool:
    """True only for ``VERIFIED``. Halt and reconciliation are not success."""

    return result.status is ManagedToolStatusV1.VERIFIED


def managed_planner_status(result: ManagedToolResultV1) -> ManagedToolStatusV1:
    """Return the typed status unchanged. Halt is halt."""

    return result.status


def managed_planner_visible_payload(
    result: ManagedToolResultV1,
) -> dict[str, object]:
    """Planner-facing dict. ``ok`` is derived from status and cannot be set."""

    return {
        "tool": result.tool.value,
        "status": result.status.value,
        "subject_token": result.subject_token,
        "ok": is_managed_verified_success(result),
    }


def managed_mcp_tool_spec(
    name: ManagedMcpToolNameV1 | str,
) -> ManagedMcpToolSpecV1:
    label = name.value if isinstance(name, ManagedMcpToolNameV1) else name
    if not isinstance(label, str):
        raise ManagedToolUnknown("tool name is not an admitted managed tool")
    try:
        tool = ManagedMcpToolNameV1(label)
    except ValueError as exc:
        raise ManagedToolUnknown(f"{label!r} is not an admitted managed tool") from exc
    for spec in MANAGED_MCP_TOOL_CATALOG.tools:
        if spec.name is tool:
            return spec
    raise ManagedToolUnknown(f"{label!r} is not an admitted managed tool")


def bind_managed_tool_call(
    tool: ManagedMcpToolNameV1 | str,
    inbox: Mapping[str, Any],
    outbox: Mapping[str, Any] | None = None,
) -> ManagedBoundToolCallV1:
    """Validate inbox/outbox and refuse actuation that still needs a human."""

    spec = managed_mcp_tool_spec(tool)
    parsed_inbox = parse_managed_inbox(inbox)
    parsed_outbox: ManagedOutboxV1 | None
    if outbox is None:
        parsed_outbox = None
    else:
        parsed_outbox = parse_managed_outbox(outbox)
        require_managed_actuation_dispatch(parsed_outbox)
    return ManagedBoundToolCallV1(
        tool=spec.name,
        inbox=parsed_inbox,
        outbox=parsed_outbox,
    )


def bind_managed_tool_result(
    call: ManagedBoundToolCallV1,
    status: ManagedToolStatusV1,
) -> ManagedToolResultV1:
    """Stamp the inbox token onto the result. Identity does not get rewritten."""

    return ManagedToolResultV1(
        tool=call.tool,
        status=status,
        subject_token=call.inbox.subject_token,
    )


__all__ = [
    "MANAGED_ACTION_TO_TOOL",
    "MANAGED_INBOX_SCHEMA",
    "MANAGED_MCP_TOOL_CATALOG",
    "MANAGED_MCP_TOOL_DESCRIPTIONS",
    "MANAGED_MCP_TOOL_NAMES",
    "MANAGED_MCP_TOOLS_SCHEMA",
    "MANAGED_OUTBOX_SCHEMA",
    "MANAGED_TOOL_RESULT_SCHEMA",
    "MANAGED_TOOL_TO_ACTION",
    "ManagedActuationDecisionV1",
    "ManagedActuationHeld",
    "ManagedBoundToolCallV1",
    "ManagedInboxV1",
    "ManagedMcpToolCatalogV1",
    "ManagedMcpToolNameV1",
    "ManagedMcpToolSpecV1",
    "ManagedOutboxActionV1",
    "ManagedOutboxV1",
    "ManagedToolResultV1",
    "ManagedToolStatusV1",
    "ManagedToolUnknown",
    "bind_managed_tool_call",
    "bind_managed_tool_result",
    "decide_managed_actuation",
    "is_managed_verified_success",
    "managed_mcp_tool_spec",
    "managed_planner_status",
    "managed_planner_visible_payload",
    "parse_managed_inbox",
    "parse_managed_outbox",
    "require_managed_actuation_dispatch",
]
