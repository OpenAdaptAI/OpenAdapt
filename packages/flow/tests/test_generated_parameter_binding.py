"""Generated data-name binding controls; genuine MCP SDK, no app execution.

The generated-tool probes spy only at compiled/governed dispatch. Separate
signature probes exercise real compiler/JIT refusals without admitting a task.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import inspect
import json
import keyword
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from openadapt_flow.compile_agent import pipeline
from openadapt_flow.compile_agent.emit import emit_mcp
from openadapt_flow.emit import emit_mcp_server
from openadapt_flow.ir import Step, Workflow
from openadapt_flow.jit.compile_agent import WebApp
from openadapt_flow.jit.compile_agent import compile_agent as make_jit

from tests._mcp_tools import call_generated_tool
from openadapt_flow.jit.signature import TaskSignature

COMPILED_PARAMETERS = [
    ("value",),
    ("TASK_DIR",),
    ("run_compiled",),
    ("run_compiled_params",),
    ("mcp",),
    ("FastMCP",),
    ("self", "params", "reset", "task_dir"),
    ("dict", "str", "__name__"),
    (
        "_",
        "___",
        "_value",
        "input_",
        "input___",
        "input_value",
        "Annotated",
        "Field",
        "model_config",
        "model_fields",
        "model_computed_fields",
        "schema",
    ),
    (
        "TASK_DIR",
        "TASK_DIR_2",
        "TASK_DIR_3",
        "mcp",
        "mcp_2",
        "FastMCP",
        "FastMCP_2",
        "run_compiled_params",
        "run_compiled_params_2",
    ),
]
PORTABLE_PARAMETERS = [
    ("value",),
    (
        "model_config",
        "model_fields",
        "model_computed_fields",
        "schema",
        "model_dump",
        "field_model_config",
    ),
    ("url", "URL", "url_2", "params", "BUNDLE_DIR", "mcp", "Path"),
    ("class", "def", "for", "match", "case", "str", "dict", "params_2"),
    (
        "a-b",
        "a_b",
        "a_b_2",
        "a b",
        "a/b",
        "123",
        "w_123",
        "___",
        "workflow",
        "workflow_2",
    ),
    (
        "_run_governed",
        "SURFACE",
        "DEPLOYMENT",
        "report",
        "workflow",
        "run_dir",
        "browser",
        "page",
        "tempfile",
        "json",
        "os",
        "sys",
        "proc",
        "fd",
        "fh",
    ),
]
# Independent template-boundary vocabulary from both generated server scopes.
TEMPLATE_BINDINGS = {
    "url",
    "params",
    "mcp",
    "FastMCP",
    "Path",
    "tempfile",
    "BUNDLE_DIR",
    "SURFACE",
    "DEPLOYMENT",
    "_run_governed",
    "_refused",
    "workflow",
    "page",
    "browser",
    "backend",
    "run_dir",
    "work",
    "params_file",
    "fd",
    "fh",
    "proc",
    "report",
    "report_path",
    "refusal",
    "pulled_skill_refusal",
    "sync_playwright",
    "launch_chromium",
    "PlaywrightBackend",
    "prepare_ws_guard",
    "Workflow",
    "Replayer",
    "json",
    "os",
    "subprocess",
    "sys",
    "parser",
    "detail",
    "lines",
    "line",
}


def _load_generated(path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("mcp")
    name = "generated_parameter_binding_model"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _registered_tool(module, public_name: str | None = None):
    try:
        from mcp.server.mcpserver import MCPServer as SDKServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP as SDKServer
    servers = [value for value in vars(module).values() if isinstance(value, SDKServer)]
    assert len(servers) == 1
    server = servers[0]
    assert type(server).__module__.startswith("mcp.")
    tools = server._tool_manager._tools
    assert len(tools) == 1
    if public_name is not None:
        assert set(tools) == {public_name}
    return next(iter(tools.values()))


def _tool(module, public_name: str | None = None):
    return _registered_tool(module, public_name).fn


def _reply():
    return pipeline.ReplayResult(
        status="not_admitted",
        verified=False,
        model_calls=None,
        seconds=0.0,
        run_dir=None,
        detail="dispatch component fixture; no task ran",
    )


def _assert_compiled_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_id: str,
    parameters: tuple[str, ...],
) -> None:
    import openadapt_flow.compile_agent as exported

    task = TaskSignature(
        task_id,
        " ".join("{" + name + "}" for name in parameters),
        {name: str for name in parameters},
    )
    fixed = tmp_path / "operator-task"
    # emit_mcp consumes task/directory metadata only; this is not an admitted
    # compiler or a fake runtime authorization.
    metadata = SimpleNamespace(task=task, dir=fixed)
    calls = []
    reply = _reply()

    def dispatch(task_directory, *mapping, **values):
        assert len(mapping) <= 1
        calls.append((task_directory, dict(mapping[0]) if mapping else dict(values)))
        return reply

    # Instrument both generations so literal-parent controls cannot start an
    # application when their old generated binding is wrong.
    monkeypatch.setattr(exported, "run_compiled", dispatch)
    monkeypatch.setattr(exported, "run_compiled_params", dispatch, raising=False)
    emitted = emit_mcp(metadata, tmp_path / "server.py")
    module = _load_generated(emitted, monkeypatch)
    tool = _registered_tool(module, task_id)
    function = tool.fn
    values = {name: "caller-data:" + name for name in parameters}
    if "TASK_DIR" in values:
        values["TASK_DIR"] = str(tmp_path / "different-admitted-task")
    returned = asyncio.run(call_generated_tool(module, values))
    assert returned == reply.to_dict() and returned["verified"] is False
    assert len(calls) == 1
    assert str(calls[0][0]) == str(fixed)
    assert calls[0][1] == values
    assert set(tool.parameters["properties"]) == set(parameters)
    assert set(tool.parameters["required"]) == set(parameters)
    assert len(inspect.signature(function).parameters) == len(parameters)
    assert all(
        not name.startswith("_") for name in inspect.signature(function).parameters
    )
    assert not fixed.exists()


@pytest.mark.parametrize("task_id", ["tool", "mcp", "str", "class"])
@pytest.mark.parametrize("parameters", COMPILED_PARAMETERS)
def test_actual_compiled_mcp_keeps_fixed_task_and_every_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_id: str,
    parameters: tuple[str, ...],
) -> None:
    _assert_compiled_binding(tmp_path, monkeypatch, task_id, parameters)


@pytest.mark.parametrize("parameter", ["class", "def", "lambda", "for"])
def test_compiled_keyword_source_refuses_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parameter: str,
) -> None:
    import openadapt_flow.compile_agent as exported

    calls = []
    monkeypatch.setattr(
        exported, "run_compiled", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setattr(
        exported,
        "run_compiled_params",
        lambda *args, **kwargs: calls.append((args, kwargs)),
        raising=False,
    )
    task = TaskSignature("tool", "{" + parameter + "}", {parameter: str})
    path = tmp_path / "server.py"
    with pytest.raises(ValueError, match="Python keywords"):
        emit_mcp(SimpleNamespace(task=task, dir=tmp_path / "task"), path)
    assert calls == [] and not path.exists()


@pytest.mark.parametrize("parameter", ["a-b", "9bad", "a b"])
def test_compiled_input_identifier_grammar_is_still_enforced(parameter: str) -> None:
    with pytest.raises(ValueError, match="parameter name"):
        TaskSignature("tool", "{" + parameter + "}", {parameter: str})


def _compiler(tmp_path: Path, parameters: tuple[str, ...]) -> pipeline.FirstRunCompiler:
    import json

    records = tmp_path / "export"
    records.mkdir(exist_ok=True)
    verify = tmp_path / "verify.yaml"
    verify.write_text(json.dumps({"verifier": {"kind": "file", "root": str(records)}}))
    return pipeline.FirstRunCompiler(
        " ".join("{" + name + "}" for name in parameters),
        params=parameters,
        url="https://synthetic.example.test/fixture",
        verify=verify,
        expect={"id": "synthetic-record"},
        on_screen=["Synthetic Record"],
        task_id="binding-probe",
        cache_dir=tmp_path / "compiler-cache",
    )


@pytest.mark.parametrize("parameter", ["reset", "task_dir"])
def test_existing_constructor_control_names_still_refuse(
    tmp_path: Path, parameter: str
) -> None:
    with pytest.raises(ValueError, match="reserved"):
        _compiler(tmp_path, (parameter,))


@pytest.mark.parametrize("entry", ["mapping", "legacy"])
@pytest.mark.parametrize(
    "parameters", [("value",), ("self", "params", "TASK_DIR", "run_compiled")]
)
def test_normal_compiler_and_jit_bind_data_then_keep_unadmitted_refusal(
    tmp_path: Path,
    entry: str,
    parameters: tuple[str, ...],
) -> None:
    compiler = _compiler(tmp_path, parameters)
    compiler.save_spec()
    values = {name: "input:" + name for name in parameters}
    loaded = pipeline.FirstRunCompiler.load(compiler.dir)
    assert (
        inspect.signature(type(loaded).run).bind(loaded, **values).arguments["params"]
        == values
    )
    if entry == "mapping":
        result = pipeline.run_compiled_params(compiler.dir, values)
    else:
        result = pipeline.run_compiled(compiler.dir, **values)
    assert result.status == "not_admitted" and not result.verified
    assert result.model_calls is None
    jit = loaded._jit()
    assert not jit.admitted
    jit_result = jit.run(**values)
    assert jit_result.status == "needs_recording" and not jit_result.agent_called
    assert not jit.program_dir.exists()


def test_mapping_separates_control_arguments_from_raw_task_signature_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parameters = ("task_dir", "reset", "self", "params", "TASK_DIR")
    task = TaskSignature(
        "direct-signature",
        " ".join("{" + name + "}" for name in parameters),
        {name: str for name in parameters},
    )
    values = {name: "data:" + name for name in parameters}
    fixed = tmp_path / "fixed"
    calls = []
    reply = _reply()

    class DispatchBoundary:
        def run(self, /, **params):
            calls.append(("run", task.validate(params)))
            return reply

    def load(cls, directory, *, reset):
        calls.append(("load", directory, reset))
        return DispatchBoundary()

    def reset_control():
        return None

    monkeypatch.setattr(pipeline.FirstRunCompiler, "load", classmethod(load))
    assert pipeline.run_compiled_params(fixed, values, reset=reset_control) is reply
    assert calls == [("load", fixed, reset_control), ("run", values)]
    # Below that modeled load boundary the genuine JIT accepts those names as
    # data and keeps its ordinary missing-admission/no-agent refusal.
    jit = make_jit(
        None,
        task=task,
        app=WebApp(url="https://synthetic.example.test/fixture"),
        verifier=None,
        policy="clinical-write",
        cache_dir=tmp_path / "jit-cache",
    )
    assert (
        inspect.signature(type(jit).run).bind(jit, **values).arguments["params"]
        == values
    )
    result = jit.run(**values)
    assert result.status == "needs_recording" and not result.agent_called
    assert not jit.program_dir.exists()


@pytest.mark.parametrize(
    "parameters", [("value",), ("self",), ("principal_id",), ("self", "principal_id")]
)
def test_recording_binds_data_without_entering_browser_or_admitting_task(
    tmp_path: Path,
    parameters: tuple[str, ...],
) -> None:
    task = TaskSignature(
        "recording-binding",
        " ".join("{" + name + "}" for name in parameters),
        {name: str for name in parameters},
    )
    values = {name: "data:" + name for name in parameters}
    jit = make_jit(
        None,
        task=task,
        app=WebApp(url="https://synthetic.example.test/fixture"),
        verifier=None,
        policy="clinical-write",
        cache_dir=tmp_path / "cache",
    )
    assert (
        inspect.signature(type(jit).recording).bind(jit, **values).arguments["params"]
        == values
    )
    recording = jit.recording(**values)
    assert recording._owner is jit
    assert recording._params == recording._values == values
    assert recording.computer is None and recording.request is None
    assert recording.result is None and recording._session is None
    result = jit.run(**values)
    assert result.status == "needs_recording" and not result.agent_called
    assert not jit.program_dir.exists()


@pytest.mark.parametrize(
    "parameters", [("value",), ("self",), ("principal_id",), ("self", "principal_id")]
)
def test_canary_mapping_keeps_data_separate_and_preserves_actual_refusals(
    tmp_path: Path,
    parameters: tuple[str, ...],
) -> None:
    from openadapt_flow.jit import lifecycle

    task = TaskSignature(
        "canary-binding",
        " ".join("{" + name + "}" for name in parameters),
        {name: str for name in parameters},
    )
    values = {name: "data:" + name for name in parameters}
    jit = make_jit(
        None,
        task=task,
        app=WebApp(url="https://synthetic.example.test/fixture"),
        verifier=None,
        policy="clinical-write",
        cache_dir=tmp_path / "cache",
        key_dir=tmp_path / "keys",
    )
    with pytest.raises(lifecycle.AdmissionError, match="a canary runs a DRAFT"):
        jit.canary_params(values, principal_id="Fixture reviewer")
    assert not jit.program_dir.exists()

    # An explicitly untrusted, unsigned candidate marker only reaches the
    # genuine principal refusal. It cannot establish a usable draft or admit
    # anything, and no lifecycle/run boundary is replaced.
    jit.program_dir.mkdir(parents=True)
    candidate = jit.program_dir / lifecycle.CANDIDATE_FILE
    candidate.write_text(json.dumps({"fixture": "untrusted refusal input"}))
    before = candidate.read_bytes()
    with pytest.raises(
        lifecycle.AdmissionError, match="a named human principal is required"
    ):
        jit.canary_params(values, principal_id=None)
    if "principal_id" not in values:
        with pytest.raises(
            lifecycle.AdmissionError, match="a named human principal is required"
        ):
            jit.canary(principal_id=None, **values)
    assert candidate.read_bytes() == before
    assert not jit.runs_dir.exists()
    assert not jit.admitted


@pytest.mark.parametrize("surface", ["web", "windows"])
@pytest.mark.parametrize("parameters", PORTABLE_PARAMETERS)
def test_portable_templates_keep_injective_aliases_and_original_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    parameters: tuple[str, ...],
) -> None:
    import ast

    bundle = tmp_path / "source-bundle"
    workflow = Workflow(
        name="binding workflow",
        surface=surface,
        params={name: "recorded-example" for name in parameters},
        steps=[Step(id="observe", intent="Observe fixture", action="wait")],
    )
    workflow.save(bundle, encrypt=False)
    path = emit_mcp_server(bundle, tmp_path / "emitted/server.py")
    module = _load_generated(path, monkeypatch)
    function = _tool(module)
    tree = ast.parse(path.read_text())
    definition = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function.__name__
    )
    mappings = [
        node
        for node in ast.walk(definition)
        if isinstance(node, ast.Dict)
        and len(node.keys) == len(parameters)
        and all(
            isinstance(key, ast.Constant) and type(key.value) is str
            for key in node.keys
        )
        and {key.value for key in node.keys} == set(parameters)
    ]
    assert len(mappings) == 1
    mapping = mappings[0]
    assert all(isinstance(value, ast.Name) for value in mapping.values)
    aliases = [value.id for value in mapping.values]
    assert len(aliases) == len(set(aliases)) == len(parameters)
    assert not set(aliases) & (
        TEMPLATE_BINDINGS | set(dir(builtins)) | set(dir(BaseModel))
    )
    assert not any(keyword.iskeyword(alias) for alias in aliases)
    values = {name: "actual:" + str(index) for index, name in enumerate(parameters)}
    arguments = {alias: values[name] for name, alias in zip(parameters, aliases)}
    if surface == "web":
        arguments["url"] = "https://synthetic.example.test/fixed-url"
    bound = inspect.signature(function).bind(**arguments)
    assert {
        key.value: bound.arguments[value.id]
        for key, value in zip(mapping.keys, mapping.values)
    } == values
    if surface == "web":
        # Web replay has an inline browser lifecycle; inspect its actual input
        # expression and SDK registration without executing that lifecycle.
        assert bound.arguments["url"] == "https://synthetic.example.test/fixed-url"
    else:
        calls = []
        monkeypatch.setattr(
            module,
            "_run_governed",
            lambda params: calls.append(params) or {"outcome": "component"},
        )
        assert function(**arguments) == {"outcome": "component"}
        assert calls == [values]


@settings(max_examples=64, derandomize=True, deadline=None)
@given(
    st.lists(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_ -/",
            min_size=0,
            max_size=20,
        ),
        min_size=1,
        max_size=12,
    )
)
def test_allocator_is_injective_and_cannot_overwrite_template_or_builtin_bindings(
    suggestions,
):
    from openadapt_flow.emit.python_names import PythonNames

    namespace = PythonNames.from_source(
        "import pathlib as Path\nTASK_DIR = None\ndef mcp(url, params):\n    return str(params)\n"
    )
    original = (
        {"Path", "TASK_DIR", "mcp", "url", "params", "str"}
        | set(dir(builtins))
        | set(dir(BaseModel))
    )
    allocated = [namespace.allocate(suggestion) for suggestion in suggestions]
    assert len(allocated) == len(set(allocated))
    assert all(
        name.isidentifier() and name[0].isalpha() and not keyword.iskeyword(name)
        for name in allocated
    )
    assert not set(allocated) & original


_PARAMETER = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,15}", fullmatch=True).filter(
    lambda name: not keyword.iskeyword(name)
)


@settings(max_examples=16, derandomize=True, deadline=None)
@given(st.lists(_PARAMETER, min_size=1, max_size=5, unique=True))
def test_compiled_emitter_preserves_supported_identifier_grammar(parameters):
    with (
        tempfile.TemporaryDirectory() as directory,
        pytest.MonkeyPatch.context() as patch,
    ):
        _assert_compiled_binding(Path(directory), patch, "tool", tuple(parameters))
