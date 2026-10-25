"""`openadapt setup` registers the MCP server and the skill, and undoes it.

Every test works in a temporary home folder. Cursor, Codex and Gemini CLI are
set up by editing their config files, so those run for real. Claude Code is
set up through its own `claude mcp` command; here a stand-in for that command
edits ~/.claude.json the way the real one does.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from openadapt import setup_clients as sc
from openadapt.cli import main

COMMAND = ["/opt/oa/bin/openadapt-agent", "serve", "--teach-browser"]
ENTRY = {"command": COMMAND[0], "args": COMMAND[1:]}


class FakeClaude:
    """`claude mcp add/remove/add-json --scope user` on ~/.claude.json."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: Any) -> SimpleNamespace:
        self.calls.append(argv[1:])
        path = self.home / ".claude.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        servers = data.setdefault("mcpServers", {})
        verb, name = argv[2], argv[5]
        if verb == "add":
            if name in servers:
                return SimpleNamespace(returncode=1, stdout="", stderr="already exists")
            command = argv[argv.index("--") + 1 :]
            servers[name] = {
                "type": "stdio",
                "command": command[0],
                "args": command[1:],
                "env": {},
            }
        elif verb == "add-json":
            servers[name] = json.loads(argv[6])
        elif verb == "remove":
            servers.pop(name)
        path.write_text(json.dumps(data))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _setup(home: Path, *, which=lambda name: None, run=None, dry_run=False):
    lines: list[str] = []
    setup = sc.Setup(
        home=home,
        command=list(COMMAND),
        say=lines.append,
        dry_run=dry_run,
        which=which,
        run=run or (lambda *a, **k: pytest.fail("no command should run")),
    )
    return setup, lines


def _files(home: Path) -> dict[str, str]:
    return {
        str(p.relative_to(home)): p.read_text()
        for p in sorted(home.rglob("*"))
        if p.is_file()
    }


def test_sets_up_each_client_found_and_says_what_changed(tmp_path: Path) -> None:
    for folder in (".cursor", ".codex", ".gemini"):
        (tmp_path / folder).mkdir()
    setup, lines = _setup(tmp_path)

    assert setup.install() == 3
    assert setup.failures == 0
    assert "Claude Code: not found, skipped" in lines
    assert (
        f"Cursor: add MCP server 'openadapt' in {tmp_path / '.cursor/mcp.json'}"
        in lines
    )
    assert any(line.startswith("Codex: write skill ") for line in lines)

    for config in (".cursor/mcp.json", ".gemini/settings.json"):
        data = json.loads((tmp_path / config).read_text())
        assert data["mcpServers"]["openadapt"] == ENTRY
    toml = (tmp_path / ".codex/config.toml").read_text()
    assert '[mcp_servers.openadapt]\ncommand = "/opt/oa/bin/openadapt-agent"\n' in toml
    assert 'args = ["serve", "--teach-browser"]' in toml
    for folder in (".cursor", ".codex", ".gemini"):
        skill = tmp_path / folder / "skills" / sc.SKILL_NAME / "SKILL.md"
        assert skill.read_text() == sc.skill_text()

    # Again: nothing changes, and it says so.
    before = _files(tmp_path)
    again, lines = _setup(tmp_path)
    again.install()
    assert _files(tmp_path) == before
    assert all("unchanged" in line or "skipped" in line for line in lines), lines


def test_keeps_other_servers_and_undo_restores_the_earlier_entry(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".cursor" / "mcp.json"
    config.parent.mkdir()
    earlier = {"command": "old-openadapt", "args": []}
    config.write_text(
        json.dumps(
            {"mcpServers": {"github": {"command": "gh-mcp"}, "openadapt": earlier}}
        )
    )
    setup, lines = _setup(tmp_path)
    setup.install([sc.CLIENTS[1]])
    assert json.loads(config.read_text())["mcpServers"] == {
        "github": {"command": "gh-mcp"},
        "openadapt": ENTRY,
    }
    assert f"Cursor: replace MCP server 'openadapt' in {config}" in lines
    # A second run doesn't forget what was there before setup.
    _setup(tmp_path)[0].install([sc.CLIENTS[1]])

    undo, lines = _setup(tmp_path)
    assert undo.undo() == 2
    assert json.loads(config.read_text())["mcpServers"] == {
        "github": {"command": "gh-mcp"},
        "openadapt": earlier,
    }
    assert not (tmp_path / ".cursor" / "skills" / sc.SKILL_NAME).exists()
    assert not (tmp_path / sc.MANIFEST).exists()


def test_undo_leaves_what_someone_changed_since(tmp_path: Path) -> None:
    (tmp_path / ".gemini").mkdir()
    _setup(tmp_path)[0].install([sc.CLIENTS[3]])
    config = tmp_path / ".gemini" / "settings.json"
    data = json.loads(config.read_text())
    data["mcpServers"]["openadapt"]["args"].append("--headed")
    config.write_text(json.dumps(data))

    undo, lines = _setup(tmp_path)
    undo.undo()
    assert "--headed" in config.read_text()
    assert any("it changed after setup" in line for line in lines)
    # The skill was undone; the changed entry stays on record for later.
    remaining = json.loads((tmp_path / sc.MANIFEST).read_text())["changes"]
    assert [c["what"] for c in remaining] == ["mcp"]


def test_codex_table_setup_did_not_write_is_left_alone(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "o4"\n\n[mcp_servers.openadapt]\ncommand = "mine"\n')
    setup, lines = _setup(tmp_path)
    setup.install([sc.CLIENTS[2]])
    assert setup.failures == 1
    assert (
        config.read_text()
        == 'model = "o4"\n\n[mcp_servers.openadapt]\ncommand = "mine"\n'
    )
    assert any("did not write" in line for line in lines)


def test_codex_undo_removes_only_its_block(tmp_path: Path) -> None:
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    original = 'model = "o4"\n\n[mcp_servers.other]\ncommand = "x"\n'
    config.write_text(original)
    _setup(tmp_path)[0].install([sc.CLIENTS[2]])
    assert sc.CODEX_BEGIN in config.read_text()
    _setup(tmp_path)[0].undo()
    assert config.read_text() == original


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    for folder in (".cursor", ".codex"):
        (tmp_path / folder).mkdir()
    setup, lines = _setup(tmp_path, dry_run=True)
    setup.install()
    assert _files(tmp_path) == {}
    assert any(line.startswith("Cursor: would add MCP server") for line in lines)


def test_claude_code_registers_through_its_own_command(tmp_path: Path) -> None:
    claude = FakeClaude(tmp_path)
    which = lambda name: "/usr/bin/claude" if name == "claude" else None  # noqa: E731
    setup, lines = _setup(tmp_path, which=which, run=claude)
    assert setup.install([sc.CLIENTS[0]]) == 1
    assert claude.calls == [
        ["mcp", "add", "--scope", "user", "openadapt", "--", *COMMAND]
    ]
    assert (
        "Claude Code: run: claude mcp add --scope user openadapt -- "
        + " ".join(shlex.quote(a) for a in COMMAND)
        in lines
    )
    assert (tmp_path / ".claude/skills" / sc.SKILL_NAME / "SKILL.md").exists()

    again, lines = _setup(tmp_path, which=which, run=claude)
    again.install([sc.CLIENTS[0]])
    assert len(claude.calls) == 1
    assert "Claude Code: MCP server 'openadapt' already registered (unchanged)" in lines

    _setup(tmp_path, which=which, run=claude)[0].undo()
    assert claude.calls[-1] == ["mcp", "remove", "--scope", "user", "openadapt"]
    assert (
        "openadapt"
        not in json.loads((tmp_path / ".claude.json").read_text())["mcpServers"]
    )


def test_claude_code_without_its_command_prints_the_command(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    setup, lines = _setup(tmp_path)
    setup.install([sc.CLIENTS[0]])
    assert setup.failures == 1
    assert any("claude mcp add --scope user openadapt --" in line for line in lines)


def test_server_command_uses_the_agent_next_to_this_python(tmp_path: Path) -> None:
    (tmp_path / "openadapt-agent").write_text("")
    python = str(tmp_path / "python")
    assert sc.server_command(python, "/venv") == [
        str(tmp_path / "openadapt-agent"),
        "serve",
        "--teach-browser",
    ]
    assert sc.server_command(str(tmp_path / "x" / "python"), "/venv")[1:] == [
        "-m",
        "openadapt_flow.agent",
        "serve",
        "--teach-browser",
    ]
    uvx = sc.server_command(python, "/Users/a/.cache/uv/archive-v0/abc")
    assert uvx[0] == "uvx" and uvx[-4:] == [
        "openadapt",
        "agent",
        "serve",
        "--teach-browser",
    ]


def test_skill_names_the_teach_tools() -> None:
    text = sc.skill_text()
    assert text.startswith("---\nname: openadapt-workflow-tools\n")
    for tool in ("teach_start", "teach_act", "teach_finish", "taught_<name>"):
        assert tool in text


def test_cli_setup_dry_run_and_nothing_found(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sc.shutil, "which", lambda name: None)
    result = CliRunner().invoke(main, ["setup", "--dry-run"])
    assert result.exit_code == 1
    assert "No supported agent found" in result.output

    (tmp_path / ".cursor").mkdir()
    result = CliRunner().invoke(main, ["setup", "--client", "cursor"])
    assert result.exit_code == 0, result.output
    assert "Cursor: add MCP server 'openadapt'" in result.output
    assert "openadapt setup --undo" in result.output
    result = CliRunner().invoke(main, ["setup", "--undo"])
    assert result.exit_code == 0
    assert "remove MCP server 'openadapt'" in result.output
    assert not (tmp_path / ".cursor" / "mcp.json").read_text().count("openadapt")


def test_real_subprocess_failure_is_reported(tmp_path: Path) -> None:
    def failing(argv, **_):
        return subprocess.CompletedProcess(argv, 1, "", "boom")

    which = lambda name: "/usr/bin/claude" if name == "claude" else None  # noqa: E731
    setup, lines = _setup(tmp_path, which=which, run=failing)
    setup.install([sc.CLIENTS[0]])
    assert setup.failures == 1
    assert any("claude mcp add failed: boom" in line for line in lines)
