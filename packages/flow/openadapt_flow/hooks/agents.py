"""What each agent sends a hook, and what it accepts back.

Verified against the agents' own documentation on 2026-11-21:

* Claude Code (code.claude.com/docs/en/hooks). Input on stdin: ``session_id``,
  ``transcript_path``, ``cwd``, ``permission_mode``, ``hook_event_name``;
  ``PreToolUse`` adds ``tool_name``, ``tool_input``, ``tool_use_id``;
  ``PostToolUse`` adds ``tool_response``; ``Stop`` adds
  ``stop_hook_active``. ``PreToolUse`` answers with
  ``hookSpecificOutput.permissionDecision`` ``allow``/``deny``/``ask``
  (``allow`` skips the user's permission prompt; deny and ask rules still
  apply) and ``permissionDecisionReason``. Exit 2 blocks with stderr as the
  reason; any other non-zero exit is a non-blocking error, so the tool runs.
* Codex (learn.chatgpt.com/docs/hooks). The same input fields plus ``model``
  and ``turn_id``. ``PreToolUse`` supports ``deny`` and ``allow``; ``ask``
  is parsed but unsupported (the hook run fails and the tool call
  continues), so OpenAdapt never sends it to Codex: a write that needs a
  person is denied with the approval command instead. Errors, timeouts and
  malformed output let the tool run.

Both agents treat a hook that can't start as a non-blocking error. The
launcher ``install`` writes turns every failure of the hook itself into
exit 2 before a tool call (see :mod:`openadapt_flow.hooks.install`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

Agent = Literal["claude-code", "codex"]
AGENTS: tuple[str, ...] = ("claude-code", "codex")
Decision = Literal["pass", "allow", "ask", "deny"]


@dataclass
class HookInput:
    agent: Agent
    event: str
    session_id: str
    cwd: str
    tool_name: str = ""
    tool_input: Any = None
    tool_use_id: str = ""
    tool_response: Any = None
    stop_hook_active: bool = False

    @classmethod
    def parse(cls, agent: Agent, event: str, data: Mapping[str, Any]) -> "HookInput":
        if not isinstance(data, Mapping):
            raise ValueError("hook input is not a JSON object")
        name = data.get("hook_event_name") or event
        if name != event:
            raise ValueError(f"hook input is for {name!r}, not {event!r}")
        tool_input = data.get("tool_input")
        return cls(
            agent=agent,
            event=event,
            session_id=str(data.get("session_id") or ""),
            cwd=str(data.get("cwd") or ""),
            tool_name=str(data.get("tool_name") or ""),
            tool_input=tool_input if isinstance(tool_input, Mapping) else {},
            tool_use_id=str(data.get("tool_use_id") or ""),
            tool_response=data.get("tool_response"),
            stop_hook_active=bool(data.get("stop_hook_active")),
        )


@dataclass
class HookOutput:
    """One answer, rendered per agent by :func:`render`."""

    decision: Decision = "pass"
    reason: str = ""
    #: Told to the agent after the call (PostToolUse) or with a decision.
    context: str = ""
    #: PostToolUse: flag the result to the agent (the write isn't verified).
    flag: bool = False
    #: Shown to the person (Stop).
    message: str = ""


def render(agent: Agent, event: str, out: HookOutput) -> tuple[Optional[dict], int]:
    """The JSON to print (or None) and the exit code."""
    if event == "PreToolUse":
        decision = out.decision
        if agent == "codex" and decision == "ask":
            decision = "deny"  # Codex runs the tool on "ask"; never send it.
        if decision == "pass":
            return None, 0
        body: dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": out.reason,
        }
        if out.context:
            body["additionalContext"] = out.context
        return {"hookSpecificOutput": body}, 0
    if event in ("PostToolUse", "PostToolUseFailure"):
        if not out.context:
            return None, 0
        doc: dict[str, Any] = {
            "hookSpecificOutput": {"hookEventName": event, "additionalContext": out.context}
        }
        if out.flag and event == "PostToolUse":
            doc["decision"] = "block"
            doc["reason"] = out.context
        return doc, 0
    if event == "Stop":
        return ({"systemMessage": out.message} if out.message else None), 0
    return None, 0
