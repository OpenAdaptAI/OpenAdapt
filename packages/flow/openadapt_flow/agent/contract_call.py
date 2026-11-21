"""One gate for a tool call, whichever transport it came in on.

MCP (:mod:`openadapt_flow.agent.mcp`) and the A2A protocol adapter
(:mod:`openadapt_flow.agent.a2a_server`) both call these, so a request over
A2A passes exactly the checks the same request over MCP passes:

* the arguments must match the tool's input schema, and the error never
  echoes input values;
* a run goes through the task ledger's ``check_run`` first, and the ledger
  records every result this server returns;
* the tool is dispatched by the transport-agnostic bridge, which applies the
  run mode, the request-id ledger, intake grants and Flow's fail-closed
  admission (``openadapt-flow run``).

A remote transport also gets an allowlist. Tools that change state only after
a person confirms in the client's own form (attended decisions, admission
decisions, ``admit``) and the approval-answer tools are never on it: a
message from another agent is never a person's approval.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from jsonschema import ValidationError, validate

from openadapt_flow.agent.bridge import AgentBridge, BridgeError

_LOG = logging.getLogger(__name__)

#: What a remote agent may call. Read-only tools plus run_workflow, which
#: is itself gated by the server's mode (production needs --mode production).
REMOTE_TOOLS = frozenset(
    {"list_workflows", "run_workflow", "get_run", "get_handoff", "verify_handoff"}
)


def validate_arguments(spec: Any, arguments: Optional[Mapping[str, Any]]) -> None:
    """Refuse arguments that don't match the schema, without echoing them."""
    try:
        validate(dict(arguments or {}), spec.input_schema)
    except ValidationError:
        # ValidationError text contains input values, so never return it.
        raise BridgeError(
            getattr(spec, "usage", None) or "tool arguments do not match the input schema"
        ) from None


def call_with_ledger(
    ledger: Any,
    name: str,
    payload: dict[str, Any],
    dispatch: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    taught_run: bool = False,
) -> dict[str, Any]:
    """Dispatch through the task ledger: check a run first, record its result."""
    if ledger is None:
        return dispatch(payload)
    if ledger.handles(name):
        return ledger.dispatch(name, payload)
    if name == "run_workflow" or taught_run:
        ledger.check_run(payload)
    result = dispatch(payload)
    # Item status comes only from this server's own results. A ledger
    # failure never hides a run's result from the caller.
    try:
        ledger.observe("run_workflow" if taught_run else name, payload, result)
    except Exception:
        _LOG.exception("could not record a result in the task ledger")
    return result


def call_remote_tool(
    bridge: AgentBridge,
    name: str,
    arguments: Optional[Mapping[str, Any]],
    *,
    allowed: frozenset[str] = REMOTE_TOOLS,
) -> dict[str, Any]:
    """The whole gate for one call from a remote agent. Blocking."""
    if name not in allowed:
        raise BridgeError("that tool is not available to a remote agent")
    spec = next((s for s in bridge.list_tool_specs() if s.name == name), None)
    if spec is None:
        raise BridgeError("unknown or unavailable tool name")
    validate_arguments(spec, arguments)
    return call_with_ledger(
        bridge.ledger,
        name,
        dict(arguments or {}),
        lambda payload: bridge.dispatch(name, payload),
    )
