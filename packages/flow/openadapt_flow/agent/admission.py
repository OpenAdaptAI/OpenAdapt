"""MCP tools for ``openadapt-flow admit``: prepare, list, and decide.

The operator turns these on with ``openadapt-agent serve --admissions DIR``.
Recordings are named by folder under ``DIR/recordings`` and shadow
candidates by id under ``DIR/shadow-candidates``; an agent never passes a
path. Admissions are written under ``DIR/admissions/<admission>``.

Who decides:

* With ``--delegate-approvals AGENT_ID --approver-grant FILE`` the calling
  agent decides as that delegated approver, under the person-signed grant in
  FILE. Flow verifies the grant on every decision, and the record says
  ``agent:AGENT_ID via mcp under grant <digest> signed by <person>``. Without
  a grant that verifies, names AGENT_ID, covers the workflow and lets an
  agent approve saves, the agent's decision is refused.
* Without it, ``decide_admission`` asks a person in the client's own form
  (MCP elicitation) and records ``person:mcp-form via mcp``. A model-supplied
  argument never stands in for that answer.

What an agent can't do: decide an admission it prepared (the record names
the preparer), decide under the ``regulated`` policy pack, or decide without
the exact ``pending_sha256`` it was shown, or decide without a grant a person
signed. Accepting runs the same gates as ``qualify accept`` and admits the
workflow to the ``problems_only`` approval mode; nothing here signs an
admission.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.admit_pipeline import (
    PENDING_FILE,
    AdmissionError,
    Decider,
    decide_admission,
    load_pending,
    prepare_admission,
    render_summary,
)

TOOL_NAMES = ("prepare_admission", "list_admissions", "decide_admission")
_FOLDER = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_ADMISSION = r"^[0-9a-f]{32}$"
_SHA = r"^[0-9a-f]{64}$"
REJECT_REASONS = ("wrong_checks", "wrong_steps", "not_this_task", "other")
PERSON_VIA_FORM = "mcp-form"

PREPARE_DESCRIPTION = (
    "Turn recordings of one task into a program with proposed checks, like "
    "`openadapt-flow admit`: compile each recording, induce one program over "
    "two or more, and propose the checks. It stops there and returns a short "
    "summary and pending_sha256. Nothing is applied until someone else "
    "decides with decide_admission; you can't decide an admission you prepared."
)
LIST_DESCRIPTION = (
    "List admissions on this server with their status, summary and pending_sha256."
)
DECIDE_DESCRIPTION = (
    "Accept or reject one admission that stopped for a decision. Send the "
    "admission and pending_sha256 from list_admissions. If the operator "
    "started this server with --delegate-approvals you decide as that "
    "delegated approver; otherwise a person decides in your client's form. "
    "Refused for an admission this agent prepared and under the regulated "
    "policy pack. The record names who decided."
)


class AdmissionToolError(ValueError):
    pass


class AdmissionDesk:
    """The admission tools over one operator-chosen folder."""

    def __init__(
        self,
        root: Path | str,
        *,
        delegate_id: Optional[str] = None,
        approver_grant: Optional[Path | str] = None,
        key_dir: Optional[Path | str] = None,
    ):
        self.root = Path(root).expanduser().resolve()
        self.delegate_id = delegate_id
        #: The person-signed grant the delegate decides under. Read on every
        #: decision so a replaced or expired file stops working at once.
        self.approver_grant = (
            Path(approver_grant).expanduser() if approver_grant else None
        )
        self.key_dir = key_dir

    def _grant(self) -> Any:
        if self.approver_grant is None:
            return None
        import json

        try:
            return json.loads(self.approver_grant.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # -- paths -------------------------------------------------------------

    def _child(self, area: str, name: Any, pattern: str = _FOLDER) -> Path:
        if not isinstance(name, str) or not re.match(pattern, name):
            raise AdmissionToolError(f"{area} names one folder")
        base = self.root / area
        path = base / name
        if base.is_symlink() or path.is_symlink():
            raise AdmissionToolError(f"{area} folder is unavailable")
        resolved = path.resolve()
        if base.resolve() not in resolved.parents or not resolved.is_dir():
            raise AdmissionToolError(f"no such folder in {area}")
        return resolved

    def _preparer(self) -> Decider:
        return Decider(kind="agent", id=self.delegate_id or "mcp-client", route="mcp")

    # -- specs -------------------------------------------------------------

    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "prepare_admission",
                "description": PREPARE_DESCRIPTION,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "recordings": {
                            "type": "array",
                            "items": {"type": "string", "pattern": _FOLDER},
                            "maxItems": 20,
                        },
                        "candidate_id": {"type": "string", "pattern": _FOLDER},
                        "name": {"type": "string", "pattern": _FOLDER},
                        "policy_pack": {
                            "type": "string",
                            "enum": ["community", "cloud", "regulated"],
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
            },
            {
                "name": "list_admissions",
                "description": LIST_DESCRIPTION,
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            },
            {
                "name": "decide_admission",
                "description": DECIDE_DESCRIPTION,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "admission": {"type": "string", "pattern": _ADMISSION},
                        "pending_sha256": {"type": "string", "pattern": _SHA},
                        "decision": {"type": "string", "enum": ["accept", "reject"]},
                        "reject_reason": {
                            "type": "string",
                            "enum": list(REJECT_REASONS),
                        },
                    },
                    "required": ["admission", "pending_sha256", "decision"],
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": True,
                    "idempotentHint": False,
                    "openWorldHint": False,
                },
            },
        ]

    def needs_person(self) -> bool:
        """Whether decide_admission must ask a person in the client's form."""

        return self.delegate_id is None

    # -- tools -------------------------------------------------------------

    def _view(self, folder: Path) -> dict[str, Any]:
        pending = load_pending(folder)
        return {
            "admission": folder.name,
            "status": pending.status,
            "halt_reason": pending.halt_reason,
            "workflow_name": pending.workflow_name,
            "policy_pack": pending.policy_pack,
            "prepared_by": pending.prepared_by.model_dump(mode="json"),
            "checks": pending.checks,
            "shadow_suggestions": [
                {"kind": item.get("kind"), "note": item.get("note")}
                for item in pending.shadow_suggestions
            ],
            "record_next": [item.get("code") for item in pending.record_next],
            "decided": (folder / "admission-decision.json").exists(),
            "summary": render_summary(pending),
            "pending_sha256": pending.sha256(),
        }

    def prepare(self, arguments: dict[str, Any]) -> dict[str, Any]:
        recordings = [
            self._child("recordings", item)
            for item in arguments.get("recordings") or []
        ]
        candidate = None
        if arguments.get("candidate_id") is not None:
            candidate = self._child("shadow-candidates", arguments["candidate_id"])
        if not recordings and candidate is None:
            raise AdmissionToolError("give recordings or a candidate_id")
        admission_id = uuid.uuid4().hex
        out = self.root / "admissions" / admission_id
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            prepare_admission(
                recordings,
                out=out,
                name=arguments["name"],
                prepared_by=self._preparer(),
                candidate=candidate,
                policy_pack=arguments.get("policy_pack") or "community",
                admission_id=admission_id,
            )
        except AdmissionError as exc:
            raise AdmissionToolError(str(exc)) from exc
        return self._view(out)

    def list(self) -> dict[str, Any]:
        base = self.root / "admissions"
        rows = []
        if base.is_dir() and not base.is_symlink():
            for folder in sorted(base.iterdir()):
                if (
                    re.match(_ADMISSION, folder.name)
                    and not folder.is_symlink()
                    and (folder / PENDING_FILE).is_file()
                ):
                    rows.append(self._view(folder))
        return {"admissions": rows}

    def confirmation_message(self, arguments: dict[str, Any]) -> str:
        folder = self._child("admissions", arguments.get("admission"), _ADMISSION)
        verb = "accept" if arguments.get("decision") == "accept" else "reject"
        return (
            render_summary(load_pending(folder))
            + f"\nConfirm that you, a person, {verb} this admission."
        )

    def decide(
        self, arguments: dict[str, Any], *, human_confirmed: bool = False
    ) -> dict[str, Any]:
        folder = self._child("admissions", arguments.get("admission"), _ADMISSION)
        decision = arguments.get("decision")
        reason = arguments.get("reject_reason")
        if (decision == "reject") != (reason is not None):
            raise AdmissionToolError("a reject needs exactly one reject_reason")
        if human_confirmed:
            decider = Decider(kind="person", id=PERSON_VIA_FORM, route="mcp")
        elif self.delegate_id is not None:
            decider = Decider(kind="agent", id=self.delegate_id, route="mcp")
        else:
            raise AdmissionToolError(
                "a person must decide this admission in the client's form"
            )
        try:
            result = decide_admission(
                folder,
                pending_sha256=str(arguments.get("pending_sha256")),
                decision=decision,
                decided_by=decider,
                reason=reason,
                approver_grant=self._grant() if decider.kind == "agent" else None,
                key_dir=self.key_dir,
            )
        except AdmissionError as exc:
            raise AdmissionToolError(str(exc)) from exc
        return {
            "admission": folder.name,
            "decision": result.decision,
            "decided_by": result.decided_by.model_dump(mode="json"),
            "prepared_by": result.prepared_by.model_dump(mode="json"),
            "oracle_gate_passed": result.oracle_gate_passed,
            "approval_mode": result.approval_mode,
            "next": result.next,
        }


__all__ = ["AdmissionDesk", "AdmissionToolError", "TOOL_NAMES"]
