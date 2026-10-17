"""Delegated approval tools: the approval mode, and answering as an agent.

The operator, never the calling agent, decides whether the agent may answer
a waiting save: ``serve --delegate-approvals AGENT_ID``. Without it these
tools aren't listed, except the read-only ``get_approval_mode``.

Even then the agent decides nothing on its own authority. Its answer goes to
``openadapt-flow item-approval`` as ``decided_by: automation`` from the
``mcp`` route, naming the agent, and Flow accepts it only where the run's
approver policy allows an agent for that step. The default policy allows
only a person, and a clinical write needs the customer's pinned opt-in.
Flow records the agent as the approver, and its decision never becomes a
training label.

There is no tool to change the mode: an agent that could lower its own
approval gate would make the gate pointless. There is no tool to admit a
demonstrated check either; a person admits those.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from openadapt_flow.agent.contract import APPROVAL_MODES

#: The opaque ids Flow accepts for a delegated approver.
DELEGATE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$"
_REQUEST_ID = r"^[0-9a-f]{32}$"
_SHA256 = r"^sha256:[0-9a-f]{64}$"
DECLINE_REASONS = ("wrong_values", "wrong_record", "screen_problem")
#: The only request fields an agent sees. No step words, no screenshot.
_REQUEST_FIELDS = (
    "request_id",
    "request_sha256",
    "status",
    "step_id",
    "step_index",
    "step_class",
    "mode",
    "created_at",
    "expires_at",
)
_RUN_ID = {
    "type": "string",
    "description": "The run_id from run_workflow.",
}
_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_ANSWER = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": True,
}

GET_MODE_DESCRIPTION = (
    "Say how saves are approved on this server: the approval mode the "
    "operator chose (each_item, problems_only, not_gated, or flow_default, "
    "where each save waits for approval), and whether you may answer a "
    "waiting save yourself. You can't change either."
)
LIST_DESCRIPTION = (
    "List the saves in one run that wait for an approval, without values "
    "or screenshots: request_id, request_sha256, step and step class, and "
    "when each expires. Use it when run_workflow says waiting_for_approval."
)
ANSWER_DESCRIPTION = (
    "Approve or decline one waiting save as this agent. The operator allowed "
    "it for this server, and Flow accepts it only where the run's approver "
    "policy allows an agent for that step (never a clinical write without "
    "the customer's opt-in). Flow records you as the approver. Send the "
    "request_id and request_sha256 from list_approval_requests. A person "
    "can still answer in OpenAdapt Desktop first."
)


def mode_view(
    approval_mode: Optional[str], delegate_id: Optional[str]
) -> dict[str, Any]:
    return {
        "approval_mode": approval_mode
        if approval_mode in APPROVAL_MODES
        else "flow_default",
        "delegated_approvals": delegate_id is not None,
        "delegate_id": delegate_id,
    }


def list_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"run_id": _RUN_ID},
        "required": ["run_id"],
        "additionalProperties": False,
    }


def answer_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "run_id": _RUN_ID,
            "request_id": {"type": "string", "pattern": _REQUEST_ID},
            "request_sha256": {"type": "string", "pattern": _SHA256},
            "decision": {"type": "string", "enum": ["approve", "decline"]},
            "decline_reason": {"type": "string", "enum": list(DECLINE_REASONS)},
        },
        "required": ["run_id", "request_id", "request_sha256", "decision"],
        "additionalProperties": False,
    }


def project_requests(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only the allow-listed, PHI-free fields of Flow's request list."""

    if payload.get("ok") is False:
        return {"ok": False, "code": str(payload.get("code", "flow_refused"))}
    rows = payload.get("requests")
    if not isinstance(rows, list):
        return {"ok": False, "code": "flow_unreadable"}
    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append({k: row[k] for k in _REQUEST_FIELDS if k in row})
    return {"ok": True, "requests": out}


class DelegationRefused(ValueError):
    pass


def answer_args(arguments: dict[str, Any], delegate_id: str) -> tuple[str, list[str]]:
    """Check the tool arguments and build Flow's item-approval arguments."""

    request_id = arguments.get("request_id")
    digest = arguments.get("request_sha256")
    decision = arguments.get("decision")
    reason = arguments.get("decline_reason")
    if not isinstance(request_id, str) or not re.match(_REQUEST_ID, request_id):
        raise DelegationRefused(
            "request_id must be the 32-hex id from list_approval_requests"
        )
    if not isinstance(digest, str) or not re.match(_SHA256, digest):
        raise DelegationRefused(
            "request_sha256 must be the digest from list_approval_requests"
        )
    if decision not in ("approve", "decline"):
        raise DelegationRefused("decision must be approve or decline")
    if (decision == "decline") != (reason is not None):
        raise DelegationRefused("a decline needs exactly one decline_reason")
    if reason is not None and reason not in DECLINE_REASONS:
        raise DelegationRefused("decline_reason is not one of the closed reasons")
    args = [
        "--request-id",
        request_id,
        "--request-sha256",
        digest,
        "--decided-by",
        "automation",
        "--route",
        "mcp",
        "--delegate-kind",
        "agent",
        "--delegate-id",
        delegate_id,
    ]
    if reason is not None:
        args += ["--reason", reason]
    return decision, args


__all__ = [
    "ANSWER_DESCRIPTION",
    "DECLINE_REASONS",
    "DELEGATE_ID_PATTERN",
    "DelegationRefused",
    "GET_MODE_DESCRIPTION",
    "LIST_DESCRIPTION",
    "answer_args",
    "answer_schema",
    "list_schema",
    "mode_view",
    "project_requests",
]
