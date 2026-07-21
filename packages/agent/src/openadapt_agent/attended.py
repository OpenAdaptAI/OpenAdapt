"""Local, capability-bound bridge to openadapt-flow's attended run contract.

This module does not implement pause, resume, verification, or audit semantics.
It projects Flow's PHI-safe Needs Attention DTOs and submits exact
``AttendedActionRequest`` values back to Flow's durable engine API.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "AttendedBridge",
    "AttendedBridgeError",
    "AttendedTool",
]

_ATTENTION_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_CAPABILITY_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{16,200}$")
_LOG = logging.getLogger(__name__)
_SAFE_DECISION_MESSAGES = {
    "completed": (
        "Flow verified the attended outcome and completed the admitted "
        "transition. Review the local run report for protected details."
    ),
    "halted": (
        "Flow admitted the attended transition, then halted safely at a later "
        "condition. Review the refreshed local Needs Attention queue."
    ),
    "refused": (
        "Flow refused the action because the signed pause, authorization, or "
        "current live state did not verify. Reload the item before deciding "
        "what to do next."
    ),
    "needs_demonstration": (
        "The governed request for a corrective demonstration was recorded. "
        "The durable pause remains available."
    ),
    "escalated": (
        "The escalation was recorded and the durable pause remains available "
        "for qualified assistance."
    ),
}


def _windows_operator_identity() -> str:
    """Read the effective Windows token username without environment lookup."""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(257)
        size = ctypes.c_ulong(len(buffer))
        if ctypes.windll.advapi32.GetUserNameW(buffer, ctypes.byref(size)):
            return buffer.value
    except (AttributeError, OSError, ValueError):
        pass
    return ""


def _local_operator_identity() -> str:
    """Use the effective OS account, never caller-controlled environment vars."""
    if os.name == "nt":
        return _windows_operator_identity()
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError, OSError):
        return ""


class AttendedBridgeError(Exception):
    """A bounded attended-operation error safe to return to a local MCP user."""


@dataclass(frozen=True)
class AttendedTool:
    """One action-specific MCP tool contract."""

    action: str
    confirmation: str
    disposition: str
    description: str


ATTENDED_TOOLS: dict[str, AttendedTool] = {
    "continue_attention": AttendedTool(
        action="continue",
        confirmation="human_completed",
        disposition="completed_by_operator",
        description=(
            "After the local operator completes the paused task in the live "
            "application, ask OpenAdapt to verify the exact postconditions and "
            "independent effects, checkpoint it as human-completed, and resume "
            "without actuating that task again."
        ),
    ),
    "skip_attention": AttendedTool(
        action="skip",
        confirmation="confirmed_not_applicable",
        disposition="not_applicable",
        description=(
            "Apply the workflow's already-declared non-consequential skip path. "
            "Flow rechecks the exact guard and refuses any undeclared, "
            "consequential, stale, or ambiguous skip."
        ),
    ),
    "teach_attention": AttendedTool(
        action="teach",
        confirmation="request_demonstration",
        disposition="teach_requested",
        description=(
            "Record an audited request for a corrective demonstration. The "
            "existing governed teach/revision gate still decides whether the "
            "demonstration is accepted, banked as progress, or refused."
        ),
    ),
    "escalate_attention": AttendedTool(
        action="escalate",
        confirmation="request_assistance",
        disposition="needs_assistance",
        description=(
            "Record an audited escalation while preserving the exact durable "
            "pause for a qualified operator."
        ),
    ),
}


def action_input_schema(tool: AttendedTool) -> dict[str, Any]:
    """Exact, free-text-free schema for one attended action tool."""
    return {
        "type": "object",
        "properties": {
            "attention_id": {
                "type": "string",
                "pattern": _ATTENTION_ID_RE.pattern,
                "description": "Opaque id returned by list_needs_attention.",
            },
            "capability_digest": {
                "type": "string",
                "pattern": _CAPABILITY_DIGEST_RE.pattern,
                "description": (
                    "Exact current capability digest returned for this attention "
                    "item. A stale or changed digest is refused."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "minLength": 16,
                "maxLength": 200,
                "pattern": _IDEMPOTENCY_KEY_RE.pattern,
                "description": (
                    "Caller-stable request id. Reuse the exact same value only "
                    "when retrying this exact action."
                ),
            },
            tool.confirmation: {
                "type": "boolean",
                "const": True,
                "description": (
                    "Explicit local-operator confirmation required before this "
                    "governed decision is submitted."
                ),
            },
        },
        "required": [
            "attention_id",
            "capability_digest",
            "idempotency_key",
            tool.confirmation,
        ],
        "additionalProperties": False,
    }


class AttendedBridge:
    """Expose Flow's local Needs Attention and attended-action APIs safely."""

    def __init__(
        self,
        runs_dir: Path,
        *,
        allow_actions: bool = False,
        service: Optional[Any] = None,
        operator: Optional[str] = None,
    ) -> None:
        self.runs_dir = Path(runs_dir)
        self.allow_actions = allow_actions
        self.service = service
        self.operator = (operator if operator is not None else _local_operator_identity()).strip()
        if allow_actions and not self.operator:
            raise AttendedBridgeError(
                "attended actions require a server-derived local operator identity"
            )

    @property
    def live_actions_ready(self) -> bool:
        """Continue/Skip need Flow's deployment-bound live executor."""
        return self.allow_actions and self.service is not None

    def enabled_action_tools(self) -> tuple[str, ...]:
        if not self.allow_actions:
            return ()
        tools = ["teach_attention", "escalate_attention"]
        if self.live_actions_ready:
            tools[0:0] = ["continue_attention", "skip_attention"]
        return tuple(tools)

    def list(self) -> dict[str, Any]:
        from openadapt_flow.console.attention import list_attention

        items = (
            list_attention(self.runs_dir)
            if self.runs_dir.is_dir() and not self.runs_dir.is_symlink()
            else []
        )
        return {
            "open_count": len(items),
            "actions_enabled": self.allow_actions,
            "live_actions_ready": self.live_actions_ready,
            "items": [item.model_dump(mode="json") for item in items],
        }

    def _resolve(self, attention_id: str) -> tuple[Path, Any]:
        if not isinstance(attention_id, str) or not _ATTENTION_ID_RE.fullmatch(attention_id):
            raise AttendedBridgeError("attention_id must be an exact opaque queue id")
        if not self.runs_dir.is_dir() or self.runs_dir.is_symlink():
            raise AttendedBridgeError("the configured runs directory is unavailable")

        from openadapt_flow.console.attention import resolve_attention

        resolved = resolve_attention(self.runs_dir, attention_id)
        if resolved is None:
            raise AttendedBridgeError("no current attention item has that id; reload the queue")
        return resolved

    def get(self, attention_id: str) -> dict[str, Any]:
        _path, item = self._resolve(attention_id)
        return item.model_dump(mode="json")

    def for_run_dir(self, run_dir: Path | str | None) -> Optional[dict[str, Any]]:
        """Return the PHI-safe attention projection for a just-finished run."""
        if not run_dir:
            return None
        if self.runs_dir.is_symlink():
            return None
        original = Path(run_dir)
        if original.is_symlink():
            return None
        try:
            root = self.runs_dir.resolve(strict=True)
            path = original.resolve(strict=True)
            path.relative_to(root)
        except (FileNotFoundError, OSError, ValueError):
            return None
        if path.is_symlink():
            return None
        from openadapt_flow.console.attention import attention_item

        item = attention_item(self.runs_dir, path)
        return item.model_dump(mode="json") if item is not None else None

    def act(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = ATTENDED_TOOLS.get(tool_name)
        if tool is None:
            raise AttendedBridgeError(f"unknown attended action tool: {tool_name}")
        if tool_name not in self.enabled_action_tools():
            if tool.action in {"continue", "skip"} and self.allow_actions:
                raise AttendedBridgeError(
                    "Continue/Skip require a deployment-bound live executor "
                    "configured when this server starts"
                )
            raise AttendedBridgeError(
                "attended actions are disabled: restart with --allow-attended-actions"
            )

        expected = {
            "attention_id",
            "capability_digest",
            "idempotency_key",
            tool.confirmation,
        }
        unknown = set(arguments) - expected
        missing = expected - set(arguments)
        if unknown or missing:
            raise AttendedBridgeError(
                "attended action arguments do not match the exact tool schema"
            )
        if arguments[tool.confirmation] is not True:
            raise AttendedBridgeError(f"{tool.confirmation} must be explicitly true")
        capability_digest = arguments["capability_digest"]
        idempotency_key = arguments["idempotency_key"]
        if (
            not isinstance(capability_digest, str)
            or not _CAPABILITY_DIGEST_RE.fullmatch(capability_digest)
            or not isinstance(idempotency_key, str)
            or not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key)
        ):
            raise AttendedBridgeError("capability_digest or idempotency_key is malformed")

        path, item = self._resolve(arguments["attention_id"])
        capability = item.capability or {}
        if capability.get("digest") != capability_digest:
            raise AttendedBridgeError("the attention item changed; reload its exact capability")
        if tool.action not in (capability.get("allowed_actions") or []):
            raise AttendedBridgeError(f"the exact pause capability does not allow {tool.action}")

        from openadapt_flow.runtime.durable import (
            ApprovalRequired,
            AttendedActionRefused,
            AttendedActionRequest,
            ResumeRefused,
            execute_attended_action,
        )

        request = AttendedActionRequest(
            capability_digest=capability_digest,
            idempotency_key=idempotency_key,
            action=tool.action,
            disposition=tool.disposition,
        )
        try:
            if self.service is not None:
                decision = self.service.execute(
                    path,
                    request,
                    operator=self.operator,
                )
            else:
                decision = execute_attended_action(
                    path,
                    request,
                    operator=self.operator,
                )
        except (ApprovalRequired, AttendedActionRefused, ResumeRefused) as exc:
            _LOG.info(
                "Flow refused attended action %s for attention item %s: %s",
                tool.action,
                item.id,
                exc,
            )
            return {
                "attention_id": item.id,
                "action": tool.action,
                "status": "refused",
                "success": False,
                "message": _SAFE_DECISION_MESSAGES["refused"],
            }
        except Exception as exc:
            _LOG.exception("attended action failed without a safe terminal decision")
            raise AttendedBridgeError(
                "attended execution did not return a safe terminal decision; "
                "inspect the durable audit and reconcile live state before retrying"
            ) from exc
        return {
            "attention_id": item.id,
            "decision_id": decision.decision_id,
            "action": decision.action,
            "status": decision.status,
            "success": decision.status == "completed" and decision.report_success is True,
            "message": _SAFE_DECISION_MESSAGES.get(
                decision.status,
                "Flow recorded the attended decision. Review the protected "
                "local run evidence for details.",
            ),
            "report_success": decision.report_success,
            "next_transition": decision.next_transition,
            "transition_receipt_digest": decision.transition_receipt_digest,
        }
