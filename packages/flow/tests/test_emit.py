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
    invocation_lines = [
        line for line in md.splitlines() if line.startswith("openadapt-flow replay ")
    ]
    assert len(invocation_lines) == 1
    invocation = invocation_lines[0]
    # Portable: the invocation references the bundle COPY inside the skill
    # folder, never an absolute path on the emitting machine.
    assert invocation.startswith("openadapt-flow replay bundle ")
    assert str(bundle.resolve()) not in invocation
    assert "--url <APP_URL>" in invocation
    assert '--param note="Follow-up in 2 weeks; BP recheck."' in invocation

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
        line for line in md.splitlines() if line.startswith("openadapt-flow replay ")
    )
    argv = shlex.split(invocation)[1:]  # drop the program name
    argv = ["http://localhost:1" if a == "<APP_URL>" else a for a in argv]
    args = build_parser().parse_args(argv)  # must not SystemExit
    assert args.command == "replay"
    assert args.bundle == "bundle"


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
    # note default is the recorded example value.
    assert tool.args.defaults[-1].value == "Follow-up in 2 weeks; BP recheck."
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
