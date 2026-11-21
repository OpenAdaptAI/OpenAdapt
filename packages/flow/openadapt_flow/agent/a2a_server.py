"""The A2A protocol adapter: ``openadapt agent serve --a2a``.

Serves the same workflows as the MCP server to any agent that speaks the
Agent2Agent (A2A) protocol, using the official ``a2a-sdk`` (Apache-2.0,
``pip install 'openadapt-flow[a2a]'``). This module is a thin transport:

* Every call goes through :func:`openadapt_flow.agent.contract_call.call_remote_tool`,
  the gate MCP uses: schema validation, the task ledger, the bridge's run mode,
  the request-id ledger and Flow's fail-closed admission. Only
  ``list_workflows``, ``run_workflow``, ``get_run``, ``get_handoff`` and
  ``verify_handoff`` exist here. Attended decisions, admission decisions and
  approval answers need a person in a confirmation form, so they are not
  served, and no A2A message ever counts as a person's approval.
* A run is an A2A task. Only outcome ``done`` is ``TASK_STATE_COMPLETED``.
  ``needs_review`` waits for a person at this computer
  (``TASK_STATE_INPUT_REQUIRED``); a message on that task only re-checks the
  run. Everything else is ``TASK_STATE_FAILED``. The run's own result, with
  ``outcome`` and ``safe_to_retry`` unchanged, is always an artifact.
* A finished run's receipt travels as an ``openadapt.handoff/v1`` artifact
  part (exact bytes, so its digests hold), marked with the handoff-receipt
  extension URI.
* It listens on 127.0.0.1 by default. Any other address needs a bearer token.
  Every request with a browser ``Origin`` header, or a ``Host`` it doesn't
  serve, is refused, so a web page can't reach the port (DNS rebinding).
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.agent.bridge import AgentBridge, BridgeError
from openadapt_flow.agent.contract_call import call_remote_tool
from openadapt_flow.handoff import EXTENSION_URI, MEDIA_TYPE, SCHEMA, VERDICT_SCHEMA

_LOG = logging.getLogger(__name__)

#: The A2A listener binds loopback unless the operator names another address.
DEFAULT_HOST = "127.0.0.1"
PROTOCOL_VERSION = "1.0"
JSONRPC_PATH = "/a2a/jsonrpc"
REST_PATH = "/a2a/rest"
VERIFY_SKILL = "verify_handoff"
#: Longest one task waits for a running run before it reports where it got.
MAX_TASK_SECONDS = 1800.0
_POLL_SECONDS = 10

A2A_INSTALL_HINT = (
    "the A2A adapter needs the a2a extra: pip install 'openadapt-flow[a2a]' "
    "(or 'openadapt[a2a]')"
)


def a2a_available() -> bool:
    try:
        import a2a.server.routes  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return False
    return True


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class A2AConfig:
    host: str = DEFAULT_HOST
    port: int = 8770
    #: The URL other agents reach this server at (the Agent Card's interfaces).
    public_url: Optional[str] = None
    #: Bearer token; required unless ``host`` is loopback.
    token: Optional[str] = None
    extra_hosts: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not is_loopback(self.host) and not self.token:
            raise ValueError(
                "--a2a-host other than 127.0.0.1 needs --a2a-token-file: "
                "anything off this computer must authenticate"
            )

    @property
    def base_url(self) -> str:
        if self.public_url:
            return self.public_url.rstrip("/")
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def allowed_hosts(self) -> set[str]:
        from urllib.parse import urlparse

        hosts = {f"{self.host}:{self.port}", f"127.0.0.1:{self.port}"}
        hosts.add(f"localhost:{self.port}")
        parsed = urlparse(self.base_url)
        if parsed.netloc:
            hosts.add(parsed.netloc)
        hosts.update(self.extra_hosts)
        return hosts


# -- the Agent Card ---------------------------------------------------------------


def _skills(bridge: AgentBridge, *, include_workflows: bool) -> list[Any]:
    from a2a.types import AgentSkill

    skills = []
    if include_workflows and bridge.allow_run:
        listing = bridge.dispatch("list_workflows", {})
        for item in listing.get("workflows") or []:
            if not item.get("available", True):
                continue
            name = str(item.get("name"))
            purpose = str(item.get("purpose") or item.get("description") or "")
            done = item.get("done_means") or item.get("what_done_means")
            description = purpose + (f" Done means: {done}" if done else "")
            skills.append(
                AgentSkill(
                    id=name,
                    name=name,
                    description=(
                        description.strip()
                        or "A compiled OpenAdapt workflow on this computer."
                    )
                    + " Send a data part {\"skill\": \""
                    + name
                    + "\", \"inputs\": {...}, \"request_id\": \"...\"}. A "
                    "finished run's result includes its signed receipt when Flow "
                    "wrote one.",
                    tags=["openadapt", "workflow", "receipt"],
                    input_modes=["application/json"],
                    output_modes=["application/json", MEDIA_TYPE],
                )
            )
    skills.append(
        AgentSkill(
            id=VERIFY_SKILL,
            name="Verify a handoff",
            description=(
                "Send another agent's result with its receipt (an "
                f"{SCHEMA} document, as a data part or a {MEDIA_TYPE} file "
                "part). Returns who signed it, whether it is intact, which "
                "checks passed and which were not checked, and any confirmation "
                "read from the other party's system. A valid signature proves "
                "who signed, not that the claim is true."
            ),
            tags=["verification", "receipt", "handoff", "in-toto", "dsse"],
            input_modes=["application/json", MEDIA_TYPE],
            output_modes=["application/json"],
        )
    )
    return skills


def build_agent_card(
    bridge: AgentBridge, config: A2AConfig, *, extended: bool = False
) -> Any:
    """The Agent Card. Built only from the public workflow projection.

    On loopback the card lists every workflow. Off loopback the public card
    lists only the verifier skill; the workflows are in the authenticated
    extended card.
    """
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentExtension,
        AgentInterface,
        AgentProvider,
        HTTPAuthSecurityScheme,
        SecurityRequirement,
        SecurityScheme,
        StringList,
    )
    from google.protobuf import struct_pb2
    from google.protobuf.json_format import ParseDict

    from openadapt_flow.agent import __version__

    remote = not is_loopback(config.host)
    params = ParseDict(
        {
            "handoff_schema": SCHEMA,
            "verdict_schema": VERDICT_SCHEMA,
            "media_type": MEDIA_TYPE,
            "receipt_predicate": "https://openadapt.ai/record/run/v1",
            "envelope": "dsse",
        },
        struct_pb2.Struct(),
    )
    card = AgentCard(
        name="OpenAdapt",
        description=(
            "Runs compiled OpenAdapt workflows on this computer and checks that "
            "they saved. A finished run's result carries its signed receipt when "
            "Flow wrote one, and any agent can send a handoff to be verified."
        ),
        provider=AgentProvider(organization="OpenAdapt", url="https://openadapt.ai"),
        version=__version__,
        documentation_url=(
            "https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/flow/docs/A2A.md"
        ),
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
            extended_agent_card=remote,
            extensions=[
                AgentExtension(
                    uri=EXTENSION_URI,
                    description=(
                        "Task results carry the run's record/run/v1 receipt "
                        "(in-toto Statement in a DSSE envelope) as an "
                        f"{SCHEMA} artifact part; {VERIFY_SKILL} returns an "
                        f"{VERDICT_SCHEMA} verdict."
                    ),
                    required=False,
                    params=params,
                )
            ],
        ),
        default_input_modes=["application/json"],
        default_output_modes=["application/json", MEDIA_TYPE],
        skills=_skills(bridge, include_workflows=extended or not remote),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION,
                url=config.base_url + JSONRPC_PATH,
            ),
            AgentInterface(
                protocol_binding="HTTP+JSON",
                protocol_version=PROTOCOL_VERSION,
                url=config.base_url + REST_PATH,
            ),
        ],
    )
    if config.token:
        card.security_schemes["bearer"].CopyFrom(
            SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer")
            )
        )
        requirement = SecurityRequirement()
        requirement.schemes["bearer"].CopyFrom(StringList())
        card.security_requirements.append(requirement)
    return card


# -- requests ---------------------------------------------------------------------


def _integral(value: Any) -> Any:
    """A data part stores numbers as doubles; restore whole numbers."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _integral(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_integral(v) for v in value]
    return value


def parse_request(message: Any) -> tuple[str, dict[str, Any]]:
    """Map one A2A message to (tool, arguments). Raises BridgeError."""
    from google.protobuf.json_format import MessageToDict

    for part in message.parts:
        if part.HasField("raw") and part.media_type == MEDIA_TYPE:
            try:
                document = json.loads(part.raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise BridgeError("the handoff file part is not JSON") from None
            return VERIFY_SKILL, {"handoff": document}
    data = [
        _integral(MessageToDict(part.data)) for part in message.parts if part.HasField("data")
    ]
    if len(data) != 1 or not isinstance(data[0], dict):
        raise BridgeError(
            'send one data part: {"skill": NAME, "inputs": {...}, "request_id": ID}, '
            '{"skill": "verify_handoff", "handoff": {...}}, or '
            '{"skill": "get_run", "run_id": ID}'
        )
    body = dict(data[0])
    skill = body.pop("skill", None)
    if not isinstance(skill, str) or not skill:
        if body.get("schema") == SCHEMA:
            return VERIFY_SKILL, {"handoff": body}
        raise BridgeError('the data part needs "skill"')
    if skill == VERIFY_SKILL:
        return VERIFY_SKILL, {"handoff": body.get("handoff")}
    if skill in ("get_run", "get_handoff", "list_workflows"):
        return skill, body
    if skill == "run_workflow":
        return "run_workflow", body
    # A workflow skill from the Agent Card is a run_workflow call.
    return "run_workflow", {"workflow": skill, **body}


def task_state_for(outcome: Optional[str]) -> str:
    """Only done is COMPLETED. needs_review waits for a person here."""
    if outcome == "done":
        return "TASK_STATE_COMPLETED"
    if outcome == "needs_review":
        return "TASK_STATE_INPUT_REQUIRED"
    if outcome == "running":
        return "TASK_STATE_WORKING"
    return "TASK_STATE_FAILED"


def _status_text(result: dict[str, Any]) -> str:
    outcome = result.get("outcome")
    text = str(result.get("what_happened") or outcome or "")
    if outcome == "needs_review":
        text += (
            " A person at the OpenAdapt computer decides. A message on this "
            "task only re-checks the run; it never approves it."
        )
    return text.strip()


class OpenAdaptExecutor:
    """An ``a2a`` AgentExecutor over one bridge (built lazily, see ``executor_class``)."""

    def __init__(self, bridge: AgentBridge) -> None:
        self.bridge = bridge
        #: A2A task id -> OpenAdapt run id, for re-checks on the same task.
        self.runs: dict[str, str] = {}

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(call_remote_tool, self.bridge, name, arguments)

    async def execute(self, context: Any, event_queue: Any) -> None:
        from a2a.helpers.proto_helpers import new_data_part, new_raw_part, new_text_part
        from a2a.server.tasks.task_updater import TaskUpdater
        from a2a.types import Task, TaskState, TaskStatus

        message = context.message
        task_id, context_id = context.task_id, context.context_id
        if message is None or not task_id or not context_id:
            return
        updater = TaskUpdater(event_queue, task_id, context_id)

        def say(text: str) -> Any:
            return updater.new_agent_message(parts=[new_text_part(text)])

        if context.current_task is None:
            await event_queue.enqueue_event(
                Task(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                    history=[message],
                )
            )
        try:
            if task_id in self.runs:
                # A follow-up on a run's task re-checks it. The message's
                # content is never read: it can't approve or change a run.
                tool, arguments = "get_run", {"run_id": self.runs[task_id]}
            else:
                tool, arguments = parse_request(message)
            if tool == VERIFY_SKILL:
                verdict = await self._call(tool, arguments)
                await updater.add_artifact(
                    [new_data_part(verdict, media_type="application/json")],
                    name="verdict",
                    extensions=[EXTENSION_URI],
                    last_chunk=True,
                )
                await updater.complete(
                    say(" ".join(verdict.get("signer_lines") or []) or "Verified.")
                )
                return
            await updater.start_work(say("Working."))
            if tool in ("list_workflows", "get_handoff"):
                result = await self._call(tool, arguments)
                await updater.add_artifact(
                    [new_data_part(result, media_type="application/json")],
                    name=tool,
                    last_chunk=True,
                )
                await updater.complete()
                return
            result = await self._call(tool, arguments)
            run_id = result.get("run_id")
            if isinstance(run_id, str):
                self.runs[task_id] = run_id
            waited = 0.0
            while result.get("outcome") == "running" and waited < MAX_TASK_SECONDS:
                await updater.update_status(
                    TaskState.TASK_STATE_WORKING, message=say(_status_text(result))
                )
                result = await self._call(
                    "get_run", {"run_id": run_id, "wait_seconds": _POLL_SECONDS}
                )
                waited += _POLL_SECONDS
            parts = [new_data_part(result, media_type="application/json")]
            if result.get("outcome") not in (None, "running") and isinstance(run_id, str):
                try:
                    handoff = await self._call("get_handoff", {"run_id": run_id})
                except BridgeError as exc:
                    parts.append(new_text_part(f"No receipt attached: {exc}"))
                else:
                    parts.append(
                        new_raw_part(
                            json.dumps(handoff, sort_keys=True).encode("utf-8"),
                            media_type=MEDIA_TYPE,
                            filename=f"{run_id}.handoff.json",
                        )
                    )
            await updater.add_artifact(
                parts, name="run_result", extensions=[EXTENSION_URI], last_chunk=True
            )
            state = task_state_for(result.get("outcome"))
            text = say(_status_text(result))
            if state == "TASK_STATE_COMPLETED":
                await updater.complete(text)
            elif state == "TASK_STATE_INPUT_REQUIRED":
                await updater.requires_input(text)
            elif state == "TASK_STATE_WORKING":
                await updater.update_status(TaskState.TASK_STATE_WORKING, message=text)
            else:
                await updater.failed(text)
        except BridgeError as exc:
            await updater.failed(say(str(exc)))
        except Exception:
            _LOG.exception("A2A task failed inside the protected boundary")
            await updater.failed(
                say(
                    "Local tool execution failed safely. Inspect the protected "
                    "local logs before retrying."
                )
            )

    async def cancel(self, context: Any, event_queue: Any) -> None:
        from a2a.utils.errors import TaskNotCancelableError

        # A run in progress is never cancelled from here: stopping a write
        # half way is itself uncertain. The run stays as it is.
        raise TaskNotCancelableError(
            "OpenAdapt doesn't cancel a run over A2A; check it with get_run"
        )


def executor_class() -> type:
    from a2a.server.agent_execution.agent_executor import AgentExecutor

    return type("OpenAdaptAgentExecutor", (OpenAdaptExecutor, AgentExecutor), {})


# -- the HTTP app -------------------------------------------------------------------


class GuardMiddleware:
    """Host check, no browser Origin, bearer token, and the extensions header."""

    def __init__(self, app: Any, config: A2AConfig) -> None:
        self.app = app
        self.config = config
        self.hosts = config.allowed_hosts()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        problem = None
        if headers.get("host") not in self.hosts:
            problem = (421, "this server does not answer for that host")
        elif "origin" in headers:
            problem = (403, "browser requests are refused")
        elif self.config.token and scope["path"] != "/.well-known/agent-card.json":
            supplied = headers.get("authorization", "")
            expected = f"Bearer {self.config.token}"
            if not hmac.compare_digest(supplied.encode(), expected.encode()):
                problem = (401, "a bearer token is required")
        if problem is not None:
            body = json.dumps({"error": problem[1]}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": problem[0],
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ]
                    + ([(b"www-authenticate", b"Bearer")] if problem[0] == 401 else []),
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        requested = {
            item.strip()
            for item in headers.get("a2a-extensions", "").split(",")
            if item.strip()
        }

        async def send_with_extensions(message: Any) -> None:
            if message["type"] == "http.response.start" and EXTENSION_URI in requested:
                message = dict(message)
                message["headers"] = list(message.get("headers") or []) + [
                    (b"a2a-extensions", EXTENSION_URI.encode())
                ]
            await send(message)

        await self.app(scope, receive, send_with_extensions)


def build_app(bridge: AgentBridge, config: A2AConfig) -> Any:
    """The ASGI app: Agent Card, JSON-RPC and HTTP+JSON bindings."""
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import (
        create_agent_card_routes,
        create_jsonrpc_routes,
        create_rest_routes,
    )
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from starlette.applications import Starlette

    card = build_agent_card(bridge, config)
    extended = (
        build_agent_card(bridge, config, extended=True)
        if card.capabilities.extended_agent_card
        else None
    )
    handler = DefaultRequestHandler(
        agent_executor=executor_class()(bridge),
        task_store=InMemoryTaskStore(),
        agent_card=card,
        extended_agent_card=extended,
        # Refuse a part whose stated media type no skill takes
        # (ContentTypeNotSupportedError), as the spec requires.
        validate_input_modes=True,
    )
    routes = [
        # The card changes only when the server restarts with other bundles.
        *create_agent_card_routes(agent_card=card, cache_control="private, max-age=300"),
        *create_jsonrpc_routes(request_handler=handler, rpc_url=JSONRPC_PATH),
        # Clients that join "/" to the interface URL post here.
        *create_jsonrpc_routes(request_handler=handler, rpc_url=JSONRPC_PATH + "/"),
        *create_rest_routes(request_handler=handler, path_prefix=REST_PATH),
    ]
    app = Starlette(routes=routes)
    return GuardMiddleware(app, config)


def read_token(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    token = Path(path).expanduser().read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("the --a2a-token-file token must be at least 32 characters")
    return token


def serve(bridge: AgentBridge, config: A2AConfig) -> None:
    """Serve until interrupted."""
    import sys

    import uvicorn

    print(
        f"openadapt-agent: serving over A2A at {config.base_url} (Agent Card "
        f"{config.base_url}/.well-known/agent-card.json); "
        + ("bearer token required" if config.token else "loopback only"),
        file=sys.stderr,
    )
    uvicorn.run(
        build_app(bridge, config),
        host=config.host,
        port=config.port,
        log_level="warning",
        proxy_headers=False,
        server_header=False,
    )
