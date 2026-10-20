"""MCP (stdio) transport for the local OpenAdapt bridges.

Run directly::

    python -m openadapt_flow.agent.mcp --bundles ./bundles [--allow-run] ...
    python -m openadapt_flow.agent.mcp --authoring

or via the CLI entry point ``openadapt-agent serve``. The server speaks
MCP over stdio (what Claude Code / Claude Desktop consume). Tool logic
lives in :mod:`openadapt_flow.agent.bridge` and :mod:`openadapt_flow.agent.authoring`;
this module only adapts them to the official ``mcp`` SDK's low-level
server. This package does not open an HTTP listener.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from contextlib import contextmanager
from typing import Any, Iterator

import anyio
import mcp.server.stdio as _mcp_stdio
import mcp.types as types
from jsonschema import ValidationError, validate
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from openadapt_flow.agent import __version__
from openadapt_flow.agent.attended import ATTENDED_TOOLS
from openadapt_flow.agent.authoring import AuthoringBridge
from openadapt_flow.agent.bridge import AgentBridge, BridgeError

__all__ = ["HUMAN_CONFIRMED_TOOLS", "build_server", "serve"]

SERVER_NAME = "openadapt-agent"
_LOG = logging.getLogger(__name__)

_CONFIRMATION_COPY = {
    "continue_attention": (
        "A person must already have completed the paused task in the visible "
        "application. Confirm that OpenAdapt may verify that outcome and resume "
        "the governed workflow after it. The completed task will not be performed again."
    ),
    "skip_attention": (
        "Confirm that this paused task is not applicable. OpenAdapt will skip "
        "only if the compiled workflow and exact signed capability declare that "
        "skip safe; otherwise it will refuse."
    ),
    "teach_attention": (
        "Confirm that OpenAdapt should record an audited request for a corrective "
        "demonstration. This does not directly rewrite or promote the workflow."
    ),
    "escalate_attention": (
        "Confirm that OpenAdapt should record an audited escalation and preserve "
        "the exact durable pause for qualified assistance."
    ),
    "reject_attention": (
        "Confirm that OpenAdapt must end this run without resuming it. This rejection "
        "dispatches no new action, but earlier run actions may have effects. Review the "
        "protected local report and transaction outcome. The durable pause remains only "
        "as the audit record. Use escalation instead when a qualified colleague must "
        "inspect and possibly continue the run."
    ),
}


#: Tools that change state only after a person confirms in the client's own
#: form. A model-supplied argument never stands in for that answer.
HUMAN_CONFIRMED_TOOLS = frozenset({*ATTENDED_TOOLS, "admit"})


def client_supports_form_elicitation(session: Any) -> bool:
    """Whether the connected client said it can show an elicitation form.

    MCP 2025-11-25 treats an empty ``elicitation`` object as form support, and
    clients written for 2025-06-18 (which had no ``form``/``url`` keys) always
    send it that way. Only a client that declares ``url`` alone, or no
    elicitation at all, can't show a form.
    """
    params = getattr(session, "client_params", None)
    capabilities = getattr(params, "capabilities", None)
    elicitation = getattr(capabilities, "elicitation", None)
    if elicitation is None or not getattr(session, "can_send_request", True):
        return False
    form = getattr(elicitation, "form", None)
    url = getattr(elicitation, "url", None)
    return form is not None or url is None


async def _confirm_with_person(
    context: Any,
    message: str,
    *,
    title: str,
    unsupported: str,
    declined: str,
) -> None:
    """Ask the person through protocol-native form elicitation, or refuse."""
    session = context.session
    if not client_supports_form_elicitation(session):
        raise BridgeError(unsupported)
    result = await session.elicit_form(
        message,
        {
            "type": "object",
            "properties": {
                "confirmed": {
                    "type": "boolean",
                    "title": title,
                }
            },
            "required": ["confirmed"],
        },
        related_request_id=context.request_id,
    )
    content = result.content or {}
    if result.action != "accept" or content.get("confirmed") is not True:
        raise BridgeError(declined)


async def _ask_person_name(context: Any, message: str) -> str:
    """Ask the person for their name and a confirmation in the client's form."""
    session = context.session
    if not client_supports_form_elicitation(session):
        raise BridgeError(
            "confirming the record check needs a person in an MCP client with "
            "form elicitation; nothing was confirmed. A person can also run "
            "`openadapt-flow compile-agent admit TASK_DIR` at a terminal"
        )
    result = await session.elicit_form(
        message,
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "title": "Your name"},
                "confirmed": {
                    "type": "boolean",
                    "title": "I am a person and I confirm this record check",
                },
            },
            "required": ["name", "confirmed"],
        },
        related_request_id=context.request_id,
    )
    content = result.content or {}
    name = content.get("name")
    if (
        result.action != "accept"
        or content.get("confirmed") is not True
        or not isinstance(name, str)
        or not name.strip()
    ):
        raise BridgeError("the person declined or cancelled; nothing was confirmed")
    return name.strip()


async def _notify_tools_changed(context: Any) -> None:
    """Tell a client that supports it that the tool list changed."""
    try:
        await context.session.send_tool_list_changed()
    except Exception:  # noqa: BLE001 - the next tools/list shows it anyway
        _LOG.debug("could not send tools/list_changed", exc_info=True)


async def _confirm_attended_action(context: Any, name: str) -> None:
    """Require a second, protocol-native human confirmation before mutation."""
    await _confirm_with_person(
        context,
        _CONFIRMATION_COPY[name],
        title="I confirm this attended action",
        unsupported=(
            "attended actions require an MCP client with form elicitation so "
            "the local operator can confirm this exact decision; use Flow's "
            "attended console/CLI when the client does not support elicitation"
        ),
        declined="the local operator declined or cancelled; no attended action was submitted",
    )


async def _confirm_admission_decision(
    context: Any, bridge: AgentBridge, arguments: dict[str, Any]
) -> None:
    """A person decides an admission in the client's form (no delegate)."""
    from openadapt_flow.agent.admission import AdmissionToolError

    try:
        message = bridge.admission_desk.confirmation_message(arguments)
    except AdmissionToolError as exc:
        raise BridgeError(str(exc)) from exc
    await _confirm_with_person(
        context,
        message,
        title="I am a person and I make this decision",
        unsupported=(
            "deciding an admission needs a person in an MCP client with form "
            "elicitation, or a server started with --delegate-approvals; "
            "nothing was decided. A person can also run `openadapt-flow admit "
            "decide` at a terminal"
        ),
        declined="the person declined or cancelled; nothing was decided",
    )


async def _confirm_admit(context: Any, authoring: AuthoringBridge) -> None:
    """A person, not the calling agent, accepts a compiled draft."""
    await _confirm_with_person(
        context,
        authoring.admit_confirmation_message(),
        title="I reviewed this draft and accept it",
        unsupported=(
            "admit requires an MCP client with form elicitation so a person "
            "can accept the draft; no draft was accepted. Use a client that "
            "supports elicitation, or Flow's own authoring tools"
        ),
        declined="the person declined or cancelled; no draft was accepted",
    )


def _server_instructions(
    authoring: AuthoringBridge | None,
    bridge: AgentBridge | None = None,
    guard: Any = None,
    ledger: Any = None,
    teach: Any = None,
) -> str:
    text = ""
    if bridge is not None:
        text = (
            "OpenAdapt enters information into apps on this computer and checks "
            "that it saved. Call list_workflows to see what it can do. Then call "
            "run_workflow with a workflow name, its inputs, and your own "
            "request_id for this piece of work. Act on the outcome: done means "
            "saved and checked; needs_review means it stopped before saving and "
            "a person decides, so don't start the same work again (reason "
            "waiting_for_approval means a person must approve the next save "
            "first); "
            "not_sure_if_saved means a person must check the record, so never "
            "retry it; did_not_run means this call wrote nothing, so fix the "
            "problem and retry only when safe_to_retry is true, and after "
            "request_id_conflict an earlier request with the same request_id may "
            "have saved. Reuse the same request_id when you retry: the same id "
            "never writes twice. If the outcome is running, call get_run with "
            "the run_id. Tell the user what_happened in your own words and "
            "never call anything but done a success."
        )
        if bridge.mode == "sandbox":
            text += (
                " This server is a sandbox: it runs against a synthetic app, so "
                "no real record changes. Use the sandbox_case input to see each "
                "outcome."
            )
        if bridge.legacy_tools:
            text += (
                " Older tools stay available for existing clients: get_workflow, "
                "get_run_report, list_needs_attention, get_attention_item, and "
                "per-workflow run_<id> tools (deprecated; they take no "
                "request_id). Attended Continue/Skip require a human action plus "
                "protocol-native operator elicitation, an exact signed "
                "capability, live revalidation, and a stable idempotency key; "
                "they never re-actuate the human-completed step. Reject ends the "
                "run and dispatches no new action, but earlier run effects still "
                "require review of the protected local outcome."
            )
    if authoring is not None:
        text += (
            " --authoring adds first-demo tools observe, start_record, click, "
            "and halt over this same local stdio process. Local stdio may also "
            "type through the recorder; hosted MCP has no type tool. Human type "
            "during pause_for_input is record_observed, never type_text. "
            "compile returns needs_human_admit; admit then asks the person "
            "to accept the draft in the client's own form, and an agent can't "
            "accept it for them. An agent click never paints VERIFIED. "
            "--authoring does not "
            "enable run tools. This process must not be port-forwarded or "
            "served over HTTP."
        )
    if guard is not None:
        from openadapt_flow.agent.guard_tools import GUARD_INSTRUCTIONS

        text += " " + GUARD_INSTRUCTIONS
    if teach is not None:
        text += (
            " To add a workflow, teach it: call teach_start with the task, then "
            "teach_observe and teach_act until it is done, then teach_finish. "
            "Run 1 makes a real entry in the app; use test records. A person "
            "confirms the record check (teach_confirm_checks shows them a form) "
            "and teach_admit admits it only under the operator's signed policy. "
            "Once admitted, it is in list_workflows and has a taught_<name> tool "
            "that runs with no model calls."
        )
    if ledger is not None:
        from openadapt_flow.agent.ledger import LEDGER_INSTRUCTIONS

        text += " " + LEDGER_INSTRUCTIONS
    return text.strip()


def build_server(
    bridge: AgentBridge | None = None,
    authoring: AuthoringBridge | None = None,
    guard: Any = None,
    ledger: Any = None,
    teach: Any = None,
) -> Server:
    """Wrap workflow, authoring, guard and/or teach bridges in an MCP Server
    (no I/O started).

    ``guard`` is a :class:`~openadapt_flow.agent.guard_tools.GuardBridge`;
    ``teach`` is a :class:`~openadapt_flow.agent.teach.TeachBridge`.
    ``ledger`` is a :class:`~openadapt_flow.agent.ledger.TaskLedger`; it adds
    the job tools and reads every run and guard result this server returns.
    """
    if bridge is None and authoring is None and guard is None and teach is None:
        raise ValueError(
            "MCP server requires a workflow, authoring or guarded-save bridge"
        )
    if ledger is None:
        # ``serve`` attaches the ledger to the bridge it serves.
        ledger = getattr(bridge, "ledger", None) or getattr(guard, "ledger", None)

    def _tool_specs():
        return (
            *(bridge.list_tool_specs() if bridge is not None else ()),
            *(authoring.list_tool_specs() if authoring is not None else ()),
            *(guard.list_tool_specs() if guard is not None else ()),
            *(teach.list_tool_specs() if teach is not None else ()),
            *(ledger.list_tool_specs() if ledger is not None else ()),
        )

    async def _list_tools(context: Any = None) -> list[types.Tool]:
        # A client that can't show a confirmation form would only ever get a
        # refusal from an attended decision tool, so it isn't offered one.
        # The call-time check in _call_tool still applies.
        session = getattr(context, "session", None)
        hide_attended = session is not None and not client_supports_form_elicitation(
            session
        )
        return [
            types.Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_schema,
                annotations=(
                    types.ToolAnnotations(**spec.annotations)
                    if spec.annotations is not None
                    else None
                ),
                **({"outputSchema": spec.output_schema} if spec.output_schema else {}),
                **({"_meta": spec.meta} if spec.meta is not None else {}),
            )
            for spec in _tool_specs()
            if not (hide_attended and spec.name in ATTENDED_TOOLS)
        ]

    async def _call_tool(
        context: Any, name: str, arguments: dict[str, Any] | None
    ) -> types.CallToolResult:
        try:
            spec = next((spec for spec in _tool_specs() if spec.name == name), None)
            if spec is None:
                raise BridgeError("unknown or unavailable tool name")
            # MCP 2 removed the low-level decorator's schema validation.
            # Keep it explicit on both SDKs, before confirmation or dispatch.
            # ValidationError text contains input values, so never return it.
            try:
                validate(arguments or {}, spec.input_schema)
            except ValidationError:
                raise BridgeError(
                    getattr(spec, "usage", None)
                    or "tool arguments do not match the input schema"
                ) from None
            human_confirmed = False
            teaching = teach is not None and teach.handles(name)
            held_by_person: str | None = None
            person: str | None = None
            admission_by_person = (
                name == "decide_admission"
                and bridge is not None
                and bridge.admission_desk is not None
                and bridge.admission_desk.needs_person()
            )
            if name in ATTENDED_TOOLS:
                await _confirm_attended_action(context, name)
            elif admission_by_person:
                assert bridge is not None
                await _confirm_admission_decision(context, bridge, arguments or {})
            elif name == "admit" and authoring is not None and authoring.handles(name):
                await _confirm_admit(context, authoring)
                human_confirmed = True
            elif teaching and name == "teach_act":
                hold = await anyio.to_thread.run_sync(
                    teach.hold_needed, arguments or {}
                )
                if hold:
                    await _confirm_with_person(
                        context,
                        teach.hold_message(hold),
                        title="I am a person and I approve this save",
                        unsupported=(
                            "this save needs a person's approval in an MCP client "
                            "with form elicitation; nothing was done"
                        ),
                        declined="the person did not approve the save; nothing was done",
                    )
                    held_by_person = hold["binding"]
            elif teaching and name == "teach_confirm_checks":
                message = await anyio.to_thread.run_sync(
                    teach.confirm_checks_message, arguments or {}
                )
                person = await _ask_person_name(context, message)

            def call() -> dict[str, Any]:
                payload = dict(arguments or {})
                if ledger is None:
                    return dispatch(payload)
                if ledger.handles(name):
                    return ledger.dispatch(name, payload)
                # A taught workflow's own tool is a run_workflow call.
                taught_run = teaching and name in teach.taught
                if name == "run_workflow" or taught_run:
                    ledger.check_run(payload)
                result = dispatch(payload)
                # Item status comes only from this server's own results. A
                # ledger failure never hides a run's result from the caller.
                try:
                    ledger.observe(
                        "run_workflow" if taught_run else name, payload, result
                    )
                except Exception:
                    _LOG.exception("could not record a result in the task ledger")
                return result

            def dispatch(payload: dict[str, Any]) -> dict[str, Any]:
                if guard is not None and guard.handles(name):
                    return guard.dispatch(name, payload)
                if teaching:
                    return teach.dispatch(
                        name, payload, held_by_person=held_by_person, person=person
                    )
                if authoring is not None and authoring.handles(name):
                    if human_confirmed:
                        return authoring.dispatch(name, payload, human_confirmed=True)
                    return authoring.dispatch(name, payload)
                if bridge is None:
                    raise BridgeError("unknown tool name")
                if admission_by_person:
                    return bridge.dispatch_admission(
                        name, payload, human_confirmed=True
                    )
                return bridge.dispatch(name, payload)

            # CLI runs and filesystem projections are blocking. Live attended
            # actions synchronously submit to their own non-async backend-owner
            # thread, so every MCP call can leave the event loop responsive.
            result = await anyio.to_thread.run_sync(call)
            if (
                teaching
                and name == "teach_admit"
                and result.get("status") == "admitted"
            ):
                await _notify_tools_changed(context)
        except BridgeError as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps({"error": str(exc)}),
                    )
                ],
                isError=True,
            )
        except Exception:
            _LOG.exception("MCP tool dispatch failed inside the protected boundary")
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": (
                                    "Local tool execution failed safely. Inspect "
                                    "the protected local logs before retrying."
                                )
                            }
                        ),
                    )
                ],
                isError=True,
            )
        # Both SDKs accept wire aliases in constructors. Always return the
        # explicit result type: MCP 2 no longer wraps a bare content list.
        text = [types.TextContent(type="text", text=json.dumps(result, indent=2))]
        if getattr(spec, "output_schema", None):
            return types.CallToolResult(content=text, structuredContent=result)
        return types.CallToolResult(content=text)

    if hasattr(Server, "list_tools"):
        # Without version=, MCP 1 reports its own SDK version as serverInfo.
        server = Server(
            SERVER_NAME,
            version=__version__,
            instructions=_server_instructions(
                authoring, bridge, guard, ledger=ledger, teach=teach
            ),
        )

        async def _list_v1() -> list[types.Tool]:
            return await _list_tools(server.request_context)

        server.list_tools()(_list_v1)

        async def _call_v1(name: str, arguments: dict[str, Any] | None):
            return await _call_tool(server.request_context, name, arguments)

        # The common handler enforces the same schema without reflecting inputs.
        server.call_tool(validate_input=False)(_call_v1)
    else:

        async def _list_v2(context: Any, params: Any) -> types.ListToolsResult:
            return types.ListToolsResult(tools=await _list_tools(context))

        async def _call_v2(context: Any, params: types.CallToolRequestParams):
            return await _call_tool(context, params.name, params.arguments)

        # Without version=, MCP 2 reports an empty serverInfo.version.
        server = Server(
            SERVER_NAME,
            version=__version__,
            instructions=_server_instructions(
                authoring, bridge, guard, ledger=ledger, teach=teach
            ),
            on_list_tools=_list_v2,
            on_call_tool=_call_v2,
        )
    if teach is not None:
        # A taught workflow adds a tool while the server runs: say so in the
        # capabilities, so clients listen for tools/list_changed.
        from mcp.server.lowlevel.server import NotificationOptions

        initialization_options = server.create_initialization_options

        def _with_tools_changed(
            notification_options: Any = None, *args: Any, **kwargs: Any
        ):
            return initialization_options(
                notification_options or NotificationOptions(tools_changed=True),
                *args,
                **kwargs,
            )

        server.create_initialization_options = _with_tools_changed  # type: ignore[method-assign]
    return server


@contextmanager
def _protected_stdout() -> Iterator[Any]:
    """Keep stray writes off the MCP wire on SDKs that don't divert fd 1.

    Background runs call Flow and browser tooling, which may print. MCP 2's
    stdio transport already points fd 1 at stderr while it serves; MCP 1's
    does not, so this does it here and hands the transport a private copy.
    """
    if hasattr(_mcp_stdio, "_claim_fd"):
        yield None
        return
    wire_fd = os.dup(1)
    saved_fd = os.dup(1)
    os.dup2(2, 1)
    wire = os.fdopen(wire_fd, "wb")
    try:
        yield anyio.wrap_file(io.TextIOWrapper(wire, encoding="utf-8"))
    finally:
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        os.dup2(saved_fd, 1)
        os.close(saved_fd)


async def _run_stdio(
    bridge: AgentBridge | None,
    authoring: AuthoringBridge | None = None,
    guard: Any = None,
    teach: Any = None,
) -> None:
    server = build_server(bridge, authoring=authoring, guard=guard, teach=teach)
    with _protected_stdout() as wire:
        kwargs = {"stdout": wire} if wire is not None else {}
        async with stdio_server(**kwargs) as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )


def serve(
    bridge: AgentBridge | None = None,
    authoring: AuthoringBridge | None = None,
    guard: Any = None,
    teach: Any = None,
) -> None:
    """Serve the bridge over stdio until the client disconnects."""
    if teach is not None:
        anyio.run(_run_stdio, bridge, authoring, guard, teach)
        return
    if guard is None:
        anyio.run(_run_stdio, bridge, authoring)
        return
    anyio.run(_run_stdio, bridge, authoring, guard)


if __name__ == "__main__":  # pragma: no cover - exercised by smoke test
    import sys

    from openadapt_flow.agent.cli import main

    raise SystemExit(main(["serve", *sys.argv[1:]]))
