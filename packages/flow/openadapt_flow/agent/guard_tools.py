"""MCP tools for a guarded save: ``guard_begin``, ``guard_commit``, ``guard_confirm``.

Any agent that writes to an app on this computer can wrap one save in these
three calls (see :mod:`openadapt_flow.guard` and ``docs/GUARDED_SAVE.md``).
The operator, not the agent, writes each task file into ``--guard-tasks``;
the agent names a ``task_id`` and can't change which record or which values
it is checked against. No model calls; nothing leaves this computer.
"""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.agent.bridge import BridgeError, ToolSpec
from openadapt_flow.guard import Guard, GuardedSave, GuardRefused, load_task

GUARD_TOOLS = ("guard_begin", "guard_commit", "guard_confirm")

_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_GUARD_ID = re.compile(r"^guard-[0-9a-f]{32}$")
#: About 6 MB of PNG after base64.
_MAX_SCREENSHOT_B64 = 8 * 1024 * 1024
_MAX_SCREEN_TEXT = 256 * 1024

_WRITES = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
_READS = {**_WRITES, "readOnlyHint": True, "idempotentHint": True}

GUARD_INSTRUCTIONS = (
    "Before you save a record, call guard_begin with the task_id you were "
    "given and the text of the record that is open on the screen, as your "
    "harness reads it. If it is refused, don't save. Then call guard_commit "
    "with a screenshot of the filled form; if it says waiting_for_approval, a "
    "person must approve first, so call guard_commit again later. Save only "
    "after it says go, and save once. Then call guard_confirm. Only VERIFIED "
    "means saved and checked. REFUTED and RECONCILIATION_REQUIRED both mean "
    "don't retry: a person must check the record."
)


class GuardBridge:
    """Dispatches the guard tools. Guards live in this process's memory."""

    #: The task ledger ``serve`` attaches; it reads each guard result.
    ledger: Any = None

    def __init__(self, guarded: GuardedSave, tasks_dir: Path | str) -> None:
        self.guarded = guarded
        self.tasks_dir = Path(tasks_dir)
        self._guards: dict[str, Guard] = {}

    def handles(self, name: str) -> bool:
        return name in GUARD_TOOLS

    def list_tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="guard_begin",
                description=(
                    "Start a guarded save. Checks that the record open on the "
                    "screen is the one the task names, and reads the system of "
                    "record before you write. Returns a guard_id, or refused "
                    "with a reason; when refused, don't save."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "pattern": _ID},
                        "screen_text": {
                            "type": "string",
                            "maxLength": _MAX_SCREEN_TEXT,
                            "description": (
                                "The open record's text from the screen "
                                "(accessibility tree, DOM text or OCR)"
                            ),
                        },
                        "agent_id": {"type": "string", "pattern": _ID},
                    },
                    "required": ["task_id", "screen_text"],
                    "additionalProperties": False,
                },
                annotations=_WRITES,
            ),
            ToolSpec(
                name="guard_commit",
                description=(
                    "Ask to save now. Returns go (save once, then call "
                    "guard_confirm), waiting_for_approval (a person must "
                    "approve; call again later), or refused (don't save)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "guard_id": {"type": "string", "pattern": _GUARD_ID.pattern},
                        "screenshot_png_base64": {
                            "type": "string",
                            "maxLength": _MAX_SCREENSHOT_B64,
                        },
                        "screen_text": {
                            "type": "string",
                            "maxLength": _MAX_SCREEN_TEXT,
                        },
                        "reason": {
                            "type": "string",
                            "maxLength": 512,
                            "description": (
                                "Why you are saving, in one sentence. The "
                                "person approving sees it as your words."
                            ),
                        },
                    },
                    "required": ["guard_id"],
                    "additionalProperties": False,
                },
                annotations=_WRITES,
            ),
            ToolSpec(
                name="guard_confirm",
                description=(
                    "After you saved, read the system of record and get the "
                    "verdict: VERIFIED, REFUTED or RECONCILIATION_REQUIRED. "
                    "Only VERIFIED is a success. Never retry the others."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "guard_id": {"type": "string", "pattern": _GUARD_ID.pattern}
                    },
                    "required": ["guard_id"],
                    "additionalProperties": False,
                },
                annotations=_READS,
            ),
        ]

    def _guard(self, guard_id: str) -> Guard:
        guard = self._guards.get(guard_id) if _GUARD_ID.match(guard_id) else None
        if guard is None:
            raise BridgeError("unknown guard_id; call guard_begin first")
        return guard

    def dispatch(self, name: str, arguments: Optional[dict[str, Any]] = None) -> dict:
        args = dict(arguments or {})
        try:
            if name == "guard_begin":
                task_id = str(args.get("task_id", ""))
                if not re.match(_ID, task_id):
                    raise BridgeError("task_id must be an opaque id")
                task = load_task(self.tasks_dir / f"{task_id}.json")
                if task.task_id != task_id:
                    raise BridgeError("the task file names a different task_id")
                guard = self.guarded.begin(
                    task,
                    screen_text=args.get("screen_text"),
                    agent_id=str(args.get("agent_id") or "unidentified-agent"),
                )
                self._guards[guard.guard_id] = guard
                return {"ok": True, "guard_id": guard.guard_id, "identity": "verified"}
            if name == "guard_commit":
                guard = self._guard(str(args.get("guard_id", "")))
                png = None
                if args.get("screenshot_png_base64"):
                    try:
                        png = base64.b64decode(
                            args["screenshot_png_base64"], validate=True
                        )
                    except (binascii.Error, ValueError):
                        raise BridgeError(
                            "screenshot_png_base64 is not base64"
                        ) from None
                result = guard.commit(
                    screenshot_png=png,
                    screen_text=args.get("screen_text"),
                    reason=str(args["reason"])[:512] if args.get("reason") else None,
                )
                return {"ok": True, **result.to_dict()}
            if name == "guard_confirm":
                guard = self._guard(str(args.get("guard_id", "")))
                return {"ok": True, **guard.confirm().to_dict()}
        except GuardRefused as exc:
            return {
                "ok": False,
                "refused": exc.code,
                "message": exc.message,
                "write_allowed": False,
            }
        raise BridgeError("unknown tool name")


__all__ = ["GUARD_INSTRUCTIONS", "GUARD_TOOLS", "GuardBridge"]
