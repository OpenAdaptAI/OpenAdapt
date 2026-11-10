"""Generated entry points use the installed MCP SDK; no SDK stand-ins.

The controlled CLI/compiler results exercise argument dispatch without an app.
Web calls stop at the genuine pulled-skill admission seam before browser work.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib
import inspect
import json
import runpy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow.compile_agent.emit import emit_mcp
from openadapt_flow.emit.mcp_tool import emit_mcp_server
from openadapt_flow.ir import ActionKind, Step, Workflow

from tests._mcp_tools import call_generated_tool, registered_server, sdk_modules

ENTRY_POINTS = ("web", "linux", "citrix", "compiled")


def _emit(root: Path, entry: str, names: list[str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if entry == "compiled":
        compiler = SimpleNamespace(
            task=SimpleNamespace(
                task_id="sdk.matrix",
                instruction="Synthetic dispatch",
                params=dict.fromkeys(names, str),
            ),
            dir=root / "task",
        )
        return emit_mcp(compiler, root / "server.py")  # type: ignore[arg-type]
    bundle = root / "input"
    Workflow(
        name="SDK Matrix",
        surface=entry,
        params=dict.fromkeys(names, "RECORDED_VALUE_MUST_NOT_BE_EMITTED"),
        steps=[
            Step(id=f"s{i}", intent="type", action=ActionKind.TYPE, param=name)
            for i, name in enumerate(names)
        ],
    ).save(bundle)
    return emit_mcp_server(bundle, root / "output" / "server.py")


def _load(path: Path) -> tuple[dict[str, Any], Any]:
    namespace = runpy.run_path(str(path))
    try:
        module = importlib.import_module("mcp.server.mcpserver")
    except ModuleNotFoundError as error:
        if error.name != "mcp.server.mcpserver":
            raise
        module = importlib.import_module("mcp.server.fastmcp")
        server_type = module.FastMCP
    else:
        server_type = module.MCPServer
    servers = [value for value in namespace.values() if isinstance(value, server_type)]
    assert len(servers) == 1
    tools = servers[0]._tool_manager._tools
    assert len(tools) == 1
    tool = next(iter(tools.values()))
    assert type(tool).__module__.startswith("mcp.")
    return namespace, tool


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_genuine_sdk_registers_and_dispatches_generated_tool(
    tmp_path, monkeypatch, entry
):
    names = ["kind", "mcp_import_error", "workflow"]
    path = _emit(tmp_path / entry, entry, names)
    seen: list[dict[str, str]] = []

    def cli(argv, **kwargs):
        params = json.loads(Path(argv[argv.index("--params-file") + 1]).read_text())
        seen.append(params)
        run = Path(argv[argv.index("--run-dir") + 1])
        run.mkdir(parents=True)
        (run / "report.json").write_text(
            json.dumps(
                {
                    "execution_outcome": "VERIFIED",
                    "transaction_outcome": "VERIFIED",
                    "production_eligible": True,
                    "model_calls": 0,
                }
            )
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def compiled(task, params):
        seen.append(params)
        return SimpleNamespace(to_dict=lambda: {"verified": True})

    namespace, _ = _load(path)
    monkeypatch.setattr("subprocess.run", cli)
    monkeypatch.setattr("openadapt_flow.compile_agent.run_compiled_params", compiled)
    monkeypatch.setattr(
        "openadapt_flow.skills.pulled_skill_refusal",
        lambda *a, **k: "synthetic admission refusal",
    )
    function = next(
        value
        for name, value in namespace.items()
        if name.startswith(("run_sdk_matrix", "sdk_matrix"))
        and inspect.isfunction(value)
    )
    ordered_names = sorted(names) if entry == "compiled" else names
    arguments = list(inspect.signature(function).parameters)
    if entry == "web":
        assert arguments.pop(0) == "url"
    values = {name: f"VALUE_{i}" for i, name in enumerate(ordered_names)}
    supplied = (
        dict(values) if entry == "compiled" else dict(zip(arguments, values.values()))
    )
    if entry == "web":
        supplied["url"] = "http://127.0.0.1:1"
    namespace["DEPLOYMENT"] = str(tmp_path / "deploy.yaml")
    # runpy's returned mapping can differ from a function's globals mapping.
    function.__globals__["DEPLOYMENT"] = namespace["DEPLOYMENT"]
    shapes: list[str] = []
    result = asyncio.run(
        call_generated_tool(namespace, supplied, observed_shapes=shapes)
    )
    server_type, _, _ = sdk_modules()
    expected_shape = "CallToolResult"
    if server_type.__name__ != "MCPServer":
        public_tools = asyncio.run(registered_server(namespace).list_tools())
        assert len(public_tools) == 1
        # FastMCP returns structured content only when it advertises a schema.
        expected_shape = (
            "content_list"
            if public_tools[0].outputSchema is None
            else "content_and_structured_tuple"
        )
    assert shapes == [expected_shape]
    if entry == "web":
        assert result["success"] is False and result["outcome"] == "REFUSED"
        assert result["detail"] == "synthetic admission refusal"
        assert seen == []
    elif entry == "compiled":
        assert result == {"verified": True}
        assert seen == [values]
    else:
        assert result["success"] is True and result["outcome"] == "VERIFIED"
        assert seen == [values]
    assert "RECORDED_VALUE_MUST_NOT_BE_EMITTED" not in path.read_text()


@settings(max_examples=12, derandomize=True, deadline=None)
@given(
    st.lists(
        st.sampled_from(
            ["mcp", "FastMCP", "mcp_import_error", "workflow", "note", "Field"]
        ),
        min_size=1,
        max_size=6,
        unique=True,
    )
)
def test_all_entry_points_keep_sdk_symbols_separate_from_parameters(names):
    with TemporaryDirectory() as directory:
        for entry in ENTRY_POINTS:
            path = _emit(Path(directory) / entry, entry, names)
            _, tool = _load(path)
            properties = tool.parameters["properties"]
            assert len(properties) == len(names) + (entry == "web")
            assert set(tool.parameters["required"]) == set(properties)
            assert all(schema["type"] == "string" for schema in properties.values())
            if entry == "compiled":
                assert set(properties) == set(names)


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize(
    "failure",
    [
        ImportError("unsupported SDK server API"),
        ModuleNotFoundError("missing SDK dependency", name="sdk_dependency"),
        ModuleNotFoundError("SDK absent", name="mcp"),
    ],
)
def test_sdk_selection_does_not_hide_broken_or_unknown_api(
    tmp_path, monkeypatch, failure, entry
):
    path = _emit(tmp_path / entry, entry, ["note"])
    original = builtins.__import__
    imports = []

    def load(name, *args, **kwargs):
        if name.startswith("mcp.server."):
            imports.append(name)
        if name == "mcp.server.mcpserver":
            raise failure
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", load)
    with pytest.raises(type(failure)) as caught:
        runpy.run_path(str(path))
    assert caught.value is failure
    assert imports == ["mcp.server.mcpserver"]


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("invalid", ["missing", "wrong_type"])
def test_sdk_argument_validation_refuses_before_any_effect(
    tmp_path, monkeypatch, entry, invalid
):
    path = _emit(tmp_path / entry, entry, ["note"])
    namespace, _ = _load(path)
    effects = []

    def forbidden(*args, **kwargs):
        effects.append("dispatch")
        pytest.fail("SDK-invalid arguments reached a handler effect")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("openadapt_flow.compile_agent.run_compiled_params", forbidden)
    monkeypatch.setattr("openadapt_flow.skills.pulled_skill_refusal", forbidden)
    supplied = {} if invalid == "missing" else {"note": ["not a string"]}
    if entry == "web":
        supplied["url"] = "http://127.0.0.1:1"
    _, exceptions, _ = sdk_modules()
    with pytest.raises(exceptions.ToolError) as caught:
        asyncio.run(call_generated_tool(namespace, supplied))
    # Genuine SDK validation, rather than a handler exception or fake context.
    assert type(caught.value.__cause__).__name__ == "ValidationError"
    assert effects == []
