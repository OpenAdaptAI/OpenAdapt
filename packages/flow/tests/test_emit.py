"""Tests for Skill/MCP emission (emit/**) and the CLI entry point."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow.emit import emit_mcp_server, emit_skill
from openadapt_flow.ir import Anchor, Step, Workflow

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _cli_env() -> dict[str, str]:
    """Env for CLI subprocesses: repo root importable regardless of cwd."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing}" if existing else str(_REPO_ROOT)
    )
    return env


def _make_bundle(tmp_path: Path, *, params: dict[str, str] | None = None) -> Path:
    """Write a minimal synthetic workflow bundle."""
    bundle = tmp_path / "bundle"
    workflow = Workflow(
        name="Triage Note",
        params=(
            params
            if params is not None
            else {"note": "Follow-up in 2 weeks; BP recheck."}
        ),
        steps=[
            Step(
                id="step_0",
                intent="click 'Sign In'",
                action="click",
                anchor=Anchor(
                    template="templates/step_0.png",
                    region=(10, 20, 160, 64),
                    click_point=(90, 52),
                    ocr_text="Sign In",
                ),
            ),
            Step(id="step_1", intent="type note", action="type", param="note"),
        ],
    )
    workflow.save(bundle)
    Image.new("RGB", (8, 8), (120, 120, 120)).save(bundle / "templates" / "step_0.png")
    return bundle


def _frontmatter(md: str) -> dict[str, str]:
    """Parse simple single-line YAML frontmatter into a dict."""
    lines = md.splitlines()
    assert lines[0] == "---"
    end = lines[1:].index("---") + 1
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


# -- emit_skill ---------------------------------------------------------------


def test_emit_skill(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    skill_dir = emit_skill(bundle, tmp_path / "skills")

    assert skill_dir == tmp_path / "skills" / "triage-note"
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.exists()
    md = skill_md.read_text(encoding="utf-8")

    fields = _frontmatter(md)
    assert fields["name"] == "triage-note"
    assert "Triage Note" in fields["description"]
    assert fields["description"]  # non-empty one-liner

    # Body: when-to-use, param docs, exact CLI invocation.
    assert "## When to use" in md
    assert "## Parameters" in md
    assert "| `note` |" in md
    assert "## What it does" in md
    assert "click 'Sign In'" in md
    assert "type <note>" in md
    invocation_lines = [
        line for line in md.splitlines() if line.startswith("openadapt-flow run ")
    ]
    assert len(invocation_lines) == 1
    invocation = invocation_lines[0]
    # Portable: the invocation references the bundle COPY inside the skill
    # folder, never an absolute path on the emitting machine.
    assert invocation.startswith("openadapt-flow run bundle ")
    assert str(bundle.resolve()) not in invocation
    assert "--url <APP_URL>" in invocation
    # A placeholder, never the recorded example value.
    assert "--param note=<note>" in invocation
    assert "Follow-up in 2 weeks" not in md

    # The bundle was copied into the skill folder (self-contained artifact).
    assert (skill_dir / "bundle" / "workflow.json").is_file()
    assert (skill_dir / "bundle" / "templates" / "step_0.png").is_file()


def test_emit_skill_invocation_is_valid_cli(tmp_path: Path) -> None:
    """The documented invocation must parse against the real CLI parser
    (guards SKILL.md / argparse drift, e.g. a newly required flag)."""
    import shlex

    from openadapt_flow.__main__ import build_parser

    bundle = _make_bundle(tmp_path)
    skill_dir = emit_skill(bundle, tmp_path / "skills")
    md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    invocation = next(
        line for line in md.splitlines() if line.startswith("openadapt-flow run ")
    )
    argv = shlex.split(invocation)[1:]  # drop the program name
    argv = ["http://localhost:1" if a == "<APP_URL>" else a for a in argv]
    args = build_parser().parse_args(argv)  # must not SystemExit
    assert args.command == "run"
    assert args.bundle == "bundle"


def _make_leaky_bundle(tmp_path: Path) -> Path:
    """A bundle whose recorded literals must never reach SKILL.md or server.py.

    It holds a constant (non-parameterized) typed password, a parameter whose
    recorded example stands in for patient data, a typed example in
    ``param_specs``, and a click whose on-screen label equals that example.
    """
    from openadapt_flow.ir import ParamSpec

    bundle = tmp_path / "leaky"
    workflow = Workflow(
        name="Triage Note",
        params={"note": "PHI-NOTE-XYZ"},
        param_specs={
            "mrn": ParamSpec(name="mrn", example="MRN-EXAMPLE-777"),
        },
        secret_params=["password"],
        steps=[
            Step(
                id="step_0",
                intent="type 'SECRET-PW-123'",
                action="type",
                text="SECRET-PW-123",
            ),
            Step(
                id="step_1",
                intent="type <password> (secret)",
                action="type",
                param="password",
                secret=True,
            ),
            Step(
                id="step_2",
                intent="click 'PHI-NOTE-XYZ'",
                action="click",
                anchor=Anchor(
                    template="templates/step_2.png",
                    region=(10, 20, 160, 64),
                    click_point=(90, 52),
                    ocr_text="PHI-NOTE-XYZ",
                ),
            ),
            Step(
                id="step_3",
                intent="type 'PHI-NOTE-XYZ'",
                action="type",
                param="note",
            ),
            Step(
                id="step_4",
                intent="type 'MRN-EXAMPLE-777'",
                action="type",
                param="mrn",
            ),
        ],
    )
    workflow.save(bundle)
    Image.new("RGB", (8, 8), (120, 120, 120)).save(bundle / "templates" / "step_2.png")
    return bundle


_RECORDED_LITERALS = ("SECRET-PW-123", "PHI-NOTE-XYZ", "MRN-EXAMPLE-777")


def test_emit_skill_contains_no_recorded_values(tmp_path: Path) -> None:
    """SKILL.md is loaded into a model's context: it must carry no typed
    text, no recorded parameter examples, and must route the agent to the
    governed ``run`` path with every parameter supplied explicitly."""
    bundle = _make_leaky_bundle(tmp_path)
    skill_dir = emit_skill(bundle, tmp_path / "skills")
    md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    for literal in _RECORDED_LITERALS:
        assert literal not in md, literal

    invocation = next(
        line for line in md.splitlines() if line.startswith("openadapt-flow ")
    )
    assert invocation.startswith("openadapt-flow run ")
    assert "--param note=<note>" in invocation
    assert "--param mrn=<mrn>" in invocation
    # A secret is read from the environment, never passed on the command line.
    assert "password" not in invocation
    assert "OPENADAPT_FLOW_SECRET_PASSWORD" in md
    assert "fall back to the recorded" not in md
    assert "Example value" not in md


def test_emit_mcp_server_has_no_recorded_defaults(tmp_path: Path) -> None:
    """The generated MCP tool must require every parameter: a recorded example
    as a default would both leak it and write it to the wrong record."""
    bundle = _make_leaky_bundle(tmp_path)
    out = emit_mcp_server(bundle, tmp_path / "mcp" / "server.py")
    source = out.read_text(encoding="utf-8")

    for literal in _RECORDED_LITERALS:
        assert literal not in source, literal
    tree = ast.parse(source)
    tool = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "run_triage_note"
    )
    assert [a.arg for a in tool.args.args] == ["url", "note", "mrn"]
    assert tool.args.defaults == []


def test_emit_skill_no_params(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path, params={})
    skill_dir = emit_skill(bundle, tmp_path / "skills")
    md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "--param" not in md
    assert "no parameters" in md.lower()


# -- emit_mcp_server ----------------------------------------------------------


def test_emit_mcp_server(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    out = emit_mcp_server(bundle, tmp_path / "mcp" / "server.py")

    assert out == tmp_path / "mcp" / "server.py"
    source = out.read_text(encoding="utf-8")

    # Generated source must be valid Python.
    tree = ast.parse(source)

    # One FastMCP tool with url + typed workflow params.
    assert "FastMCP" in source
    assert "from mcp.server.fastmcp import FastMCP" in source
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    tool_funcs = [f for f in funcs if f.name == "run_triage_note"]
    assert len(tool_funcs) == 1
    tool = tool_funcs[0]
    arg_names = [a.arg for a in tool.args.args]
    assert arg_names == ["url", "note"]
    assert all(
        isinstance(a.annotation, ast.Name) and a.annotation.id == "str"
        for a in tool.args.args
    )
    # Every workflow parameter is required: no recorded example as default.
    assert tool.args.defaults == []
    assert "Follow-up in 2 weeks" not in source
    assert ast.get_docstring(tool)

    # Server wiring: the bundle is copied next to server.py and referenced
    # relative to __file__ — never by an emitting-machine absolute path.
    assert str(bundle.resolve()) not in source
    assert "Path(__file__).resolve().parent / 'bundle'" in source
    assert (out.parent / "bundle" / "workflow.json").is_file()
    assert (out.parent / "bundle" / "templates" / "step_0.png").is_file()
    assert "mcp.run()" in source
    assert "'note': note" in source

    # Emission itself must not import mcp.
    assert "mcp" not in sys.modules
    assert "mcp.server.fastmcp" not in sys.modules


def test_emit_mcp_server_odd_name(tmp_path: Path) -> None:
    bundle = tmp_path / "odd"
    Workflow(name="2-Fast 2-Furious!", params={}).save(bundle)
    out = emit_mcp_server(bundle, tmp_path / "server.py")
    source = out.read_text(encoding="utf-8")
    tree = ast.parse(source)
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert any(f.startswith("run_") and f.isidentifier() for f in funcs)


def _emitted_bundle_dir(server: Path) -> Path:
    """Resolve the BUNDLE_DIR an emitted server.py would load at run time."""
    tree = ast.parse(server.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "BUNDLE_DIR"
        ):
            subdir = node.value.right  # Path(__file__).resolve().parent / <name>
            assert isinstance(subdir, ast.Constant)
            return server.resolve().parent / subdir.value
    raise AssertionError("emitted server defines no BUNDLE_DIR")


def _named_bundle(root: Path, name: str, n_steps: int) -> Path:
    bundle = root / name.lower().replace(" ", "-")
    Workflow(
        name=name,
        params={},
        steps=[
            Step(id=f"s{i}", intent=f"press key {i}", action="key", text="a")
            for i in range(n_steps)
        ],
    ).save(bundle)
    return bundle


def test_emit_mcp_two_servers_same_dir_keep_their_own_workflow(
    tmp_path: Path,
) -> None:
    run_demo = _named_bundle(tmp_path / "src", "Run Demo", 2)
    quickstart = _named_bundle(tmp_path / "src", "Local Quickstart", 5)
    servers = tmp_path / "servers"

    demo_server = emit_mcp_server(run_demo, servers / "run_demo.py")
    quick_server = emit_mcp_server(quickstart, servers / "quickstart.py")

    demo_dir = _emitted_bundle_dir(demo_server)
    quick_dir = _emitted_bundle_dir(quick_server)
    assert demo_dir != quick_dir
    assert Workflow.load(demo_dir).name == "Run Demo"
    assert len(Workflow.load(demo_dir).steps) == 2
    assert Workflow.load(quick_dir).name == "Local Quickstart"
    assert len(Workflow.load(quick_dir).steps) == 5


def test_emit_mcp_reemit_removes_stale_templates(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    Image.new("RGB", (4, 4)).save(bundle / "templates" / "old.png")
    out = emit_mcp_server(bundle, tmp_path / "mcp" / "server.py")
    emitted = _emitted_bundle_dir(out)
    assert (emitted / "templates" / "old.png").is_file()

    (bundle / "templates" / "old.png").unlink()
    emit_mcp_server(bundle, out)
    assert not (emitted / "templates" / "old.png").exists()
    assert (emitted / "templates" / "step_0.png").is_file()


def test_emit_mcp_refuses_to_replace_a_different_workflow(tmp_path: Path) -> None:
    mine = _named_bundle(tmp_path / "src", "Run Demo", 2)
    servers = tmp_path / "servers"
    # Someone else's bundle already sits where server.py keeps its copy.
    foreign = _named_bundle(tmp_path / "src", "Local Quickstart", 5)
    import shutil

    shutil.copytree(foreign, servers / "bundle")
    (servers / "bundle" / "templates").mkdir(exist_ok=True)
    (servers / "bundle" / "templates" / "keep.png").write_bytes(b"x")

    with pytest.raises(FileExistsError, match="Local Quickstart"):
        emit_mcp_server(mine, servers / "server.py")
    assert Workflow.load(servers / "bundle").name == "Local Quickstart"
    assert (servers / "bundle" / "templates" / "keep.png").is_file()
    assert not (servers / "server.py").exists()


def _load_emitted_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report_fields: dict[str, object]
):
    """Import an emitted server.py with the browser and replay stubbed out.

    Returns the generated tool function. ``Replayer.run`` returns a RunReport
    built from ``report_fields``, so the test checks only what the tool
    reports for a given runtime outcome.
    """
    import importlib.util
    import types
    from contextlib import contextmanager

    from openadapt_flow.ir import RunReport
    from openadapt_flow.runtime.replayer import Replayer

    fastmcp = types.ModuleType("mcp.server.fastmcp")

    class _FakeFastMCP:
        def __init__(self, name: str) -> None:
            self.name = name

        def tool(self):  # noqa: ANN202 - mirrors FastMCP's decorator factory
            return lambda func: func

        def run(self) -> None:  # pragma: no cover - never called
            raise AssertionError("not used")

    fastmcp.FastMCP = _FakeFastMCP  # type: ignore[attr-defined]
    for name in ("mcp", "mcp.server"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp)

    class _Page:
        def goto(self, url: str) -> None:
            pass

    class _Browser:
        def new_page(self, **kwargs: object) -> _Page:
            return _Page()

        def close(self) -> None:
            pass

    @contextmanager
    def _fake_sync_playwright():
        yield types.SimpleNamespace(
            chromium=types.SimpleNamespace(launch=lambda **kwargs: _Browser())
        )

    sync_api = pytest.importorskip("playwright.sync_api")
    import openadapt_flow._browser_setup as browser_setup
    import openadapt_flow.backends.playwright_backend as playwright_backend

    monkeypatch.setattr(browser_setup, "ensure_chromium_installed", lambda: None)
    monkeypatch.setattr(sync_api, "sync_playwright", _fake_sync_playwright)
    monkeypatch.setattr(playwright_backend, "PlaywrightBackend", lambda page: page)

    def _fake_run(self, workflow, **kwargs):  # noqa: ANN001, ANN202
        return RunReport(
            workflow_name=workflow.name,
            started_at="2026-10-10T00:00:00Z",
            **report_fields,
        )

    monkeypatch.setattr(Replayer, "run", _fake_run)

    out = emit_mcp_server(_make_bundle(tmp_path), tmp_path / "mcp" / "server.py")
    spec = importlib.util.spec_from_file_location("emitted_server", out)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run_triage_note


@pytest.mark.parametrize(
    ("fields", "expected_success"),
    [
        (
            {
                "success": True,
                "execution_profile": "demo",
                "execution_outcome": "COMPLETED_UNVERIFIED",
                "transaction_outcome": "COMPLETED_UNVERIFIED",
            },
            False,
        ),
        (
            {
                "success": False,
                "execution_profile": "demo",
                "execution_outcome": "HALTED",
                "transaction_outcome": "RECONCILIATION_REQUIRED",
            },
            False,
        ),
        (
            {
                "success": True,
                "execution_profile": "standard",
                "execution_outcome": "VERIFIED",
                "transaction_outcome": "VERIFIED",
                "production_eligible": True,
            },
            True,
        ),
        (
            # A coarse VERIFIED whose transaction still needs reconciliation
            # (for example a lost managed result) is not a success.
            {
                "success": True,
                "execution_profile": "standard",
                "execution_outcome": "VERIFIED",
                "transaction_outcome": "RECONCILIATION_REQUIRED",
            },
            False,
        ),
    ],
)
def test_emitted_mcp_tool_never_reports_success_for_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fields: dict[str, object],
    expected_success: bool,
) -> None:
    """A screen-only completion (no record check) must not come back to the
    calling agent as success: only a VERIFIED outcome does."""
    tool = _load_emitted_tool(tmp_path, monkeypatch, fields)
    out = tool(url="http://127.0.0.1:1/", note="synthetic note")
    assert out["success"] is expected_success
    assert out["outcome"] == fields["execution_outcome"]
    assert out["transaction_outcome"] == fields["transaction_outcome"]
    assert out["production_eligible"] is bool(fields.get("production_eligible"))
    assert out["run_dir"]


# -- CLI ----------------------------------------------------------------------


def test_cli_help_works_without_sibling_modules() -> None:
    """--help must work even if other agents' modules aren't built yet."""
    proc = subprocess.run(
        [sys.executable, "-m", "openadapt_flow", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env=_cli_env(),
    )
    assert proc.returncode == 0
    for cmd in ("demo-record", "compile", "replay", "bench", "emit-skill", "emit-mcp"):
        assert cmd in proc.stdout


@pytest.mark.parametrize(
    "subcommand",
    ["demo-record", "compile", "replay", "bench", "emit-skill", "emit-mcp"],
)
def test_cli_subcommand_help(subcommand: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "openadapt_flow", subcommand, "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env=_cli_env(),
    )
    assert proc.returncode == 0


def test_cli_emit_skill_end_to_end(tmp_path: Path) -> None:
    """emit-skill and emit-mcp run through the real CLI process."""
    bundle = _make_bundle(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_flow",
            "emit-skill",
            str(bundle),
            "--out",
            str(tmp_path / "skills"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=_cli_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "skills" / "triage-note" / "SKILL.md").exists()

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_flow",
            "emit-mcp",
            str(bundle),
            "--out",
            str(tmp_path / "server.py"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=_cli_env(),
    )
    assert proc.returncode == 0, proc.stderr
    ast.parse((tmp_path / "server.py").read_text(encoding="utf-8"))


def test_parse_params() -> None:
    from openadapt_flow.__main__ import _parse_params

    assert _parse_params(None) == {}
    assert _parse_params(["a=1", "b=x=y"]) == {"a": "1", "b": "x=y"}
    with pytest.raises(SystemExit):
        _parse_params(["missing_equals"])


def test_replay_params_file_keeps_values_out_of_argv(tmp_path: Path) -> None:
    from openadapt_flow.__main__ import _replay_params, build_parser

    params_file = tmp_path / "params.json"
    params_file.write_text('{"patient_id":"synthetic-001","count":2}')
    assert _replay_params(["count=3"], str(params_file)) == {
        "patient_id": "synthetic-001",
        "count": "3",
    }
    args = build_parser().parse_args(
        ["replay", "bundle", "--params-file", str(params_file)]
    )
    assert args.params_file == str(params_file)
