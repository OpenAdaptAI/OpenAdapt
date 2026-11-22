"""``openadapt wrap --task TASK -- COMMAND``: wrap any agent process.

OpenAdapt opens the task's app in its own headless browser and starts
``COMMAND`` with three environment variables:

``OPENADAPT_MCP_URL``
    A Model Context Protocol endpoint (streamable HTTP) on ``127.0.0.1``.
``OPENADAPT_MCP_TOKEN``
    A random token for this run only. Every request must send it as
    ``Authorization: Bearer <token>``; it is compared in constant time.
``OPENADAPT_TASK``
    The task, with its inputs, as the agent should read it.

The command gets the caller's environment without any ``OPENADAPT_*``
variable and without any variable the task's ``verify.yaml`` reads (a
``${NAME}`` reference or a ``*_env`` value), so OpenAdapt's own keys and
settings and the system of record's credentials never reach the agent being
admitted. When the privacy extra scrubs listings, the task's own values
(its record's identity and its inputs) appear as placeholders such as
``⟦task.identity_1⟧`` in ``OPENADAPT_TASK`` and in every listing; typing a
placeholder types its value.

The endpoint's tools are the skill cache's browser tools (``observe``,
``click``, ``type_text``, ``press_key``, ``scroll``, ``finish``). Each one goes
through compile-agent's harness, exactly as OpenAdapt's built-in agent does:
the save gate checks that the task's record is on screen before a save, every
save is read back from the system of record, an action outside the task's
origins ends the run, and the actions are recorded. The command never gets
the browser itself (no DevTools endpoint), so it cannot act around those
checks. When it exits, the endpoint closes and the run goes through the same
record check, proposed checks, canary and admission as ``openadapt wrap
--task``. Later calls (``openadapt wrap run``) run the admitted program with
no model calls.

The command itself runs as an ordinary process with the user's permissions;
only the browser it drives is OpenAdapt's.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.integrations.agents.skills import BrowserTools, FrameworkAgent

__all__ = [
    "ENV_TASK",
    "ENV_TOKEN",
    "ENV_URL",
    "CommandFailed",
    "ProcessAgent",
    "ToolEndpoint",
    "child_environment",
    "request_id_for",
    "stop_running_commands",
    "verify_env_names",
]

ENV_URL = "OPENADAPT_MCP_URL"
ENV_TOKEN = "OPENADAPT_MCP_TOKEN"
ENV_TASK = "OPENADAPT_TASK"
_PATH = "/mcp"
#: How long a killed command's process group gets to exit after SIGTERM.
_TERM_GRACE_S = 5.0


class CommandFailed(RuntimeError):
    """The wrapped command exited with a nonzero status or ran out of time."""

    def __init__(self, exit_code: int) -> None:
        super().__init__(f"the wrapped command exited with status {exit_code}")
        self.exit_code = exit_code


def child_environment(
    environ: Mapping[str, str], withheld: Iterable[str] = ()
) -> dict[str, str]:
    """The command's environment: the caller's, without OpenAdapt's own settings.

    The command is the agent whose run is being admitted, so it gets none of
    OpenAdapt's configuration or credentials (``OPENADAPT_*``: the admission
    key passphrase, ``OPENADAPT_FLOW_SECRET_<NAME>`` sign-in values, Cloud
    keys), and none of the ``withheld`` names (the variables the task's
    verifier reads, such as its database password). Its own variables, such
    as its model key, pass through.
    """

    drop = {name.upper() for name in withheld}
    return {
        k: v
        for k, v in environ.items()
        if not k.upper().startswith("OPENADAPT_") and k.upper() not in drop
    }


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


def verify_env_names(verify: Any) -> frozenset[str]:
    """Every environment variable a ``verify.yaml`` reads.

    That is each ``${NAME}`` (or ``${NAME:-default}``) reference anywhere in
    the file, and each value of a key ending in ``_env`` (for example
    ``sql_password_env``, ``basic_env``, or every value of a
    ``secret_env: {slot: VAR}`` map). An unreadable file raises, so a
    wrapped command never starts with credentials that could not be checked.
    """

    import yaml

    text = Path(verify).read_text(encoding="utf-8")
    names = set(_ENV_REF.findall(text))

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if isinstance(key, str) and key.endswith("_env"):
                    # One name, a list of names, or a map such as
                    # ``secret_env: {slot: VAR}``: every value is a variable.
                    if isinstance(value, Mapping):
                        values = list(value.values())
                    elif isinstance(value, (list, tuple)):
                        values = list(value)
                    else:
                        values = [value]
                    names.update(v for v in values if isinstance(v, str) and v)
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(yaml.safe_load(text))
    return frozenset(names)


def request_id_for(task: str, command: Sequence[str], values: Mapping[str, str]) -> str:
    """A request ID derived from the task, the command and its inputs.

    The same wrapped run asked again gets the same ID, so a retry dedupes
    instead of saving twice. A different input is a different request.
    """

    material = json.dumps(
        {
            "task": str(Path(task).expanduser().resolve()),
            "command": list(command),
            "inputs": dict(sorted(values.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "wrap-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


#: Wrapped commands still running, so a signal to OpenAdapt ends them too.
_RUNNING: set["subprocess.Popen[Any]"] = set()
# Re-entrant: the CLI's signal handlers call stop_running_commands() on the
# main thread, which may already hold the lock when the signal arrives.
_RUNNING_LOCK = threading.RLock()


def stop_running_commands() -> None:
    """End every wrapped command still running, with its whole process group."""

    with _RUNNING_LOCK:
        running = list(_RUNNING)
    for process in running:
        with contextlib.suppress(Exception):
            _stop_group(process)


def _run_group(command: Sequence[str], env: Mapping[str, str], timeout_s: float) -> int:
    """Run ``command`` in its own process group and return its exit status.

    The group is ended on a timeout (``CommandFailed``), on any exception in
    this thread, and by :func:`stop_running_commands` (which the CLI calls on
    Ctrl-C and SIGTERM, since the group's own session gets neither).
    """

    process = subprocess.Popen(
        list(command), env=dict(env), start_new_session=os.name == "posix"
    )
    with _RUNNING_LOCK:
        _RUNNING.add(process)
    try:
        try:
            return process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _stop_group(process)
            raise CommandFailed(
                process.returncode if process.returncode else -9
            ) from None
        except BaseException:
            _stop_group(process)
            raise
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(process)


def _tool_specs() -> list[dict[str, Any]]:
    def obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        }

    text = {"type": "string"}
    return [
        {
            "name": "observe",
            "description": "Read the current page: one line per heading, text, row or "
            "control. Controls carry refs such as e3.",
            "schema": obj({}, []),
        },
        {
            "name": "click",
            "description": "Click the control with this ref from the latest listing.",
            "schema": obj({"ref": text}, ["ref"]),
        },
        {
            "name": "type_text",
            "description": "Type text. With a ref, click that field first; without "
            "one, type into the focused field. Typing appends to the field.",
            "schema": obj({"text": text, "ref": text}, ["text"]),
        },
        {
            "name": "press_key",
            "description": "Press one key, such as Enter, Tab or Escape.",
            "schema": obj({"key": text}, ["key"]),
        },
        {
            "name": "scroll",
            "description": "Scroll the page up or down by most of a screen.",
            "schema": obj(
                {"direction": {"type": "string", "enum": ["up", "down"]}}, []
            ),
        },
        {
            "name": "finish",
            "description": "End the task, once, when it is done or cannot be done.",
            "schema": obj({"summary": text}, ["summary"]),
        },
    ]


def _call(tools: BrowserTools, name: str, args: Mapping[str, Any]) -> str:
    if name == "observe":
        return tools.observe()
    if name == "click":
        return tools.click(str(args["ref"]))
    if name == "type_text":
        return tools.type_text(str(args["text"]), str(args.get("ref") or ""))
    if name == "press_key":
        return tools.press_key(str(args["key"]))
    if name == "scroll":
        return tools.scroll(str(args.get("direction") or "down"))
    if name == "finish":
        return tools.finish(str(args["summary"]))
    raise LookupError(name)


def _mcp_server(tools: BrowserTools) -> Any:
    """A low-level MCP server over ``tools``.

    It follows ``openadapt_flow.wrap.mcp``'s adapter for both SDK generations;
    the streamable-HTTP endpoint is tested with the locked MCP 1 SDK.
    """

    from importlib import import_module

    import anyio
    from jsonschema import ValidationError, validate
    from mcp.server.lowlevel import Server

    try:
        types: Any = import_module("mcp.types")
    except ModuleNotFoundError as error:
        if error.name != "mcp.types":
            raise
        types = import_module("mcp_types")
    modern = types.__name__ == "mcp_types"
    input_schema_field = "input_schema" if modern else "inputSchema"
    error_field = "is_error" if modern else "isError"
    specs = {spec["name"]: spec for spec in _tool_specs()}

    async def list_tools() -> list[Any]:
        return [
            types.Tool(
                name=spec["name"],
                description=spec["description"],
                **{input_schema_field: spec["schema"]},
            )
            for spec in specs.values()
        ]

    async def call_tool(name: str, arguments: Optional[dict[str, Any]]) -> Any:
        spec = specs.get(name)
        try:
            if spec is None:
                raise LookupError(name)
            args = arguments or {}
            validate(args, spec["schema"])
            # The browser belongs to the harness thread; BrowserTools hands
            # the action to it and waits.
            text = await anyio.to_thread.run_sync(
                lambda: _call(tools, name, args), abandon_on_cancel=False
            )
        except (ValidationError, LookupError):
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="Not done: invalid call")],
                **{error_field: True},
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    from openadapt_flow.privacy import PLACEHOLDER_HELP

    instructions = (
        "You operate a web application for the user. observe returns the page as "
        "a listing; controls carry refs such as e3. Act with click, type_text, "
        "press_key and scroll; each returns the new listing. Before a save the "
        "right record must be on screen; every save is read back. Do exactly "
        "the task in OPENADAPT_TASK and call finish once. " + PLACEHOLDER_HELP
    )
    if hasattr(Server, "list_tools"):
        server = Server(
            "openadapt-wrap-process", version="1", instructions=instructions
        )
        server.list_tools()(list_tools)
        server.call_tool(validate_input=False)(call_tool)
        return server

    async def list_v2(context: Any, params: Any) -> Any:
        return types.ListToolsResult(tools=await list_tools())

    async def call_v2(context: Any, params: Any) -> Any:
        return await call_tool(params.name, params.arguments)

    callback_server: Any = Server
    return callback_server(
        "openadapt-wrap-process",
        version="1",
        instructions=instructions,
        on_list_tools=list_v2,
        on_call_tool=call_v2,
    )


class ToolEndpoint:
    """The browser tools as a token-guarded MCP endpoint on 127.0.0.1."""

    def __init__(self, tools: BrowserTools) -> None:
        self.token = secrets.token_urlsafe(32)
        self._tools = tools
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}{_PATH}"
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None

    def _app(self) -> Any:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        manager = StreamableHTTPSessionManager(app=_mcp_server(self._tools))
        expected = ("Bearer " + self.token).encode("ascii")

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] == "lifespan":
                async with contextlib.AsyncExitStack() as stack:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await stack.enter_async_context(manager.run())
                        await send({"type": "lifespan.startup.complete"})
                    message = await receive()
                    await send({"type": "lifespan.shutdown.complete"})
                return
            if scope["type"] != "http":
                return
            headers = dict(scope.get("headers") or [])
            given = headers.get(b"authorization", b"")
            if scope.get("path") != _PATH or not hmac.compare_digest(given, expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401 if scope.get("path") == _PATH else 404,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send({"type": "http.response.body", "body": b"refused"})
                return
            await manager.handle_request(scope, receive, send)

        return app

    def start(self, timeout_s: float = 10.0) -> None:
        import uvicorn

        config = uvicorn.Config(
            self._app(),
            log_level="error",
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._thread = threading.Thread(
            target=self._server.run,
            kwargs={"sockets": [self._sock]},
            name="openadapt-wrap-mcp",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + timeout_s
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("the local tool endpoint did not start")
            time.sleep(0.02)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._sock.close()


class ProcessAgent(FrameworkAgent):
    """Run 1 by an outside command that drives OpenAdapt's browser over MCP.

    The command's model usage is its own and is not visible here, so run 1's
    cost is reported as unknown, never as zero.
    """

    dialect = "generic"
    requires_model_key = False

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_s: float = 600.0,
        principal_id: str = "agent:wrapped-process",
        withheld_env: Iterable[str] = (),
    ) -> None:
        if not command or any(not isinstance(part, str) for part in command):
            raise ValueError("the wrapped command is empty")
        if not (timeout_s > 0):
            raise ValueError("the command timeout must be positive")
        super().__init__(self._run_command, principal_id=principal_id)
        self.command = list(command)
        self.timeout_s = timeout_s
        #: Variables the task's verifier reads; the command never gets them.
        self.withheld_env: frozenset[str] = frozenset(withheld_env)
        #: The command's exit status, once it has exited (negative: a signal).
        self.exit_code: Optional[int] = None

    def _run_command(self, task: str, tools: BrowserTools) -> None:
        endpoint = ToolEndpoint(tools)
        endpoint.start()
        try:
            env = child_environment(os.environ, self.withheld_env)
            env.update(
                {ENV_URL: endpoint.url, ENV_TOKEN: endpoint.token, ENV_TASK: task}
            )
            try:
                self.exit_code = _run_group(self.command, env, self.timeout_s)
            except CommandFailed as failed:
                self.exit_code = failed.exit_code
                raise
        finally:
            endpoint.stop()
        if self.exit_code != 0:
            raise CommandFailed(self.exit_code)
        if tools.summary is None and tools.stopped is None:
            # A command that exits 0 without calling finish has finished.
            tools.summary = "the wrapped command exited with status 0"
        return None


def _stop_group(process: "subprocess.Popen[Any]") -> None:
    """End the command and every process it started, by its own group only."""

    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=_TERM_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    else:
        process.kill()
    process.wait(timeout=_TERM_GRACE_S)
