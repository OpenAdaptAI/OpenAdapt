"""``openadapt setup``: add OpenAdapt to the coding agents on this computer.

For each of Claude Code, Cursor, Codex and Gemini CLI that is installed, it
registers the OpenAdapt MCP server (``openadapt-agent serve
--teach-browser``) and installs the ``openadapt-workflow-tools`` skill, which
tells the agent when and how to teach a browser task and call it again.

Every change is listed as it is made. Running it again changes nothing that
is already right. ``--dry-run`` lists the changes without making them.
``--undo`` reverses what setup changed, using the record it keeps in
``~/.openadapt/setup.json``; an entry someone changed since is left alone.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

__all__ = ["CLIENTS", "SERVER_NAME", "SKILL_NAME", "Setup", "server_command"]

SERVER_NAME = "openadapt"
SKILL_NAME = "openadapt-workflow-tools"
MANIFEST = Path(".openadapt") / "setup.json"
CODEX_BEGIN = "# BEGIN openadapt setup (openadapt setup --undo removes this block)"
CODEX_END = "# END openadapt setup"
_CODEX_TABLE = re.compile(r'^\s*\[mcp_servers\.(?:openadapt|"openadapt")\]\s*$', re.M)


@dataclass(frozen=True)
class Client:
    key: str
    name: str
    executable: str
    #: The client's folder in the home directory.
    folder: str
    #: How the MCP server is registered: ``claude-cli``, ``json`` or ``toml``.
    kind: str
    config: str = ""

    def skill_path(self, home: Path) -> Path:
        return home / self.folder / "skills" / SKILL_NAME / "SKILL.md"


CLIENTS = (
    Client("claude", "Claude Code", "claude", ".claude", "claude-cli", ".claude.json"),
    Client("cursor", "Cursor", "cursor", ".cursor", "json", ".cursor/mcp.json"),
    Client("codex", "Codex", "codex", ".codex", "toml", ".codex/config.toml"),
    Client(
        "gemini", "Gemini CLI", "gemini", ".gemini", "json", ".gemini/settings.json"
    ),
)


def server_command(
    executable: Optional[str] = None, prefix: Optional[str] = None
) -> list[str]:
    """The command a client runs to start OpenAdapt's MCP server: the
    ``openadapt-agent`` installed next to this Python, so the client starts
    the same install that ran setup."""

    executable = executable or sys.executable
    prefix = prefix if prefix is not None else sys.prefix
    if "/uv/archive-v" in prefix.replace("\\", "/"):
        # A uvx throwaway environment: name the package, not a cache path.
        major, minor = sys.version_info[:2]
        return [
            "uvx",
            "--python",
            f"{major}.{minor}",
            "--from",
            "openadapt",
            "openadapt",
            "agent",
            "serve",
            "--teach-browser",
        ]
    folder = Path(executable).parent
    for name in ("openadapt-agent", "openadapt-agent.exe"):
        if (folder / name).exists():
            return [str(folder / name), "serve", "--teach-browser"]
    return [executable, "-m", "openadapt_flow.agent", "serve", "--teach-browser"]


def skill_text() -> str:
    return (
        resources.files("openadapt")
        .joinpath("skills", SKILL_NAME, "SKILL.md")
        .read_text(encoding="utf-8")
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("it is not a JSON object")
    return data


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".openadapt-tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def _toml_str(value: str) -> str:
    return json.dumps(value)


def _codex_block(command: Sequence[str]) -> str:
    args = ", ".join(_toml_str(a) for a in command[1:])
    return (
        f"{CODEX_BEGIN}\n"
        f"[mcp_servers.{SERVER_NAME}]\n"
        f"command = {_toml_str(command[0])}\n"
        f"args = [{args}]\n"
        "startup_timeout_sec = 60\n"
        f"{CODEX_END}\n"
    )


def _strip_codex_block(text: str) -> tuple[str, Optional[str]]:
    start = text.find(CODEX_BEGIN)
    if start < 0:
        return text, None
    end = text.find(CODEX_END, start)
    if end < 0:
        return text, None
    end += len(CODEX_END)
    if text[end : end + 1] == "\n":
        end += 1
    block = text[start:end]
    before = text[:start]
    if before.endswith("\n\n"):
        before = before[:-1]
    return before + text[end:], block


@dataclass
class Setup:
    """One run of ``openadapt setup``. ``say`` gets one line per change."""

    home: Path
    command: list[str]
    say: Callable[[str], None]
    dry_run: bool = False
    which: Callable[[str], Optional[str]] = field(default_factory=lambda: shutil.which)
    run: Callable[..., Any] = field(default_factory=lambda: subprocess.run)
    changes: list[dict[str, Any]] = field(default_factory=list)
    failures: int = 0

    # -- the record of what setup changed ----------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.home / MANIFEST

    def _manifest(self) -> dict[str, Any]:
        try:
            data = _read_json(self.manifest_path)
        except (OSError, ValueError):
            data = {}
        data.setdefault("changes", [])
        return data

    def _remember(self, change: dict[str, Any]) -> None:
        """Keep the first ``previous`` value for a place: undo restores what
        was there before setup ever ran."""

        if self.dry_run:
            return
        data = self._manifest()
        for old in data["changes"]:
            if old["client"] == change["client"] and old["what"] == change["what"]:
                change = {**change, "previous": old.get("previous")}
                data["changes"].remove(old)
                break
        data["changes"].append(change)
        _write_atomic(self.manifest_path, json.dumps(data, indent=2) + "\n")

    def _say(self, client: Client, text: str) -> None:
        self.say(f"{client.name}: {'would ' if self.dry_run else ''}{text}")

    # -- detection ----------------------------------------------------------------------------

    def detected(self, client: Client) -> bool:
        return (
            bool(self.which(client.executable)) or (self.home / client.folder).is_dir()
        )

    def _desired_entry(self) -> dict[str, Any]:
        return {"command": self.command[0], "args": list(self.command[1:])}

    # -- install ------------------------------------------------------------------------------

    def install(self, clients: Sequence[Client] = CLIENTS) -> int:
        found = [c for c in clients if self.detected(c)]
        for client in clients:
            if client not in found:
                self.say(f"{client.name}: not found, skipped")
        for client in found:
            try:
                if client.kind == "claude-cli":
                    self._install_claude(client)
                elif client.kind == "json":
                    self._install_json(client)
                else:
                    self._install_codex(client)
                self._install_skill(client)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.failures += 1
                self.say(f"{client.name}: not changed: {exc}")
        return len(found)

    def _install_json(self, client: Client) -> None:
        path = self.home / client.config
        try:
            data = _read_json(path)
        except ValueError as exc:
            raise ValueError(
                f"{path} is not valid JSON ({exc}); fix it and run setup again"
            )
        servers = data.get("mcpServers")
        if servers is not None and not isinstance(servers, dict):
            raise ValueError(f"mcpServers in {path} is not an object")
        servers = dict(servers or {})
        current = servers.get(SERVER_NAME)
        desired = self._desired_entry()
        if current == desired:
            self._say(client, f"already set up in {path} (unchanged)")
            return
        servers[SERVER_NAME] = desired
        data["mcpServers"] = servers
        verb = "replace" if current is not None else "add"
        self._say(client, f"{verb} MCP server {SERVER_NAME!r} in {path}")
        if not self.dry_run:
            _write_atomic(path, json.dumps(data, indent=2) + "\n")
            self._remember(
                {
                    "client": client.key,
                    "what": "mcp",
                    "path": str(path),
                    "written": desired,
                    "previous": current,
                }
            )

    def _install_codex(self, client: Client) -> None:
        path = self.home / client.config
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        rest, ours = _strip_codex_block(text)
        block = _codex_block(self.command)
        if ours == block:
            self._say(client, f"already set up in {path} (unchanged)")
            return
        if _CODEX_TABLE.search(rest):
            raise ValueError(
                f"{path} already has an [mcp_servers.{SERVER_NAME}] table that "
                "setup did not write; remove it, or point it at: "
                + " ".join(self.command)
            )
        self._say(
            client,
            f"{'replace' if ours else 'add'} MCP server {SERVER_NAME!r} in {path}",
        )
        if not self.dry_run:
            sep = (
                ""
                if not rest or rest.endswith("\n\n")
                else ("\n" if rest.endswith("\n") else "\n\n")
            )
            _write_atomic(path, rest + sep + block)
            self._remember(
                {
                    "client": client.key,
                    "what": "mcp",
                    "path": str(path),
                    "previous": None,
                }
            )

    def _claude_current(self) -> Optional[dict[str, Any]]:
        try:
            data = _read_json(self.home / ".claude.json")
        except (OSError, ValueError):
            return None
        entry = (data.get("mcpServers") or {}).get(SERVER_NAME)
        return entry if isinstance(entry, dict) else None

    def _claude(self, *args: str) -> None:
        exe = self.which("claude")
        result = self.run(
            [exe, "mcp", *args], capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            raise subprocess.SubprocessError(
                f"claude mcp {args[0]} failed: {(result.stderr or result.stdout).strip()[:300]}"
            )

    def _install_claude(self, client: Client) -> None:
        current = self._claude_current()
        desired = self._desired_entry()
        if (
            current is not None
            and {
                "command": current.get("command"),
                "args": list(current.get("args") or []),
            }
            == desired
        ):
            self._say(
                client, f"MCP server {SERVER_NAME!r} already registered (unchanged)"
            )
            return
        add = ["add", "--scope", "user", SERVER_NAME, "--", *self.command]
        if self.which("claude") is None:
            raise ValueError(
                "the claude command is not on PATH; register the server with: "
                "claude mcp " + " ".join(_quote(a) for a in add)
            )
        self._say(client, "run: claude mcp " + " ".join(_quote(a) for a in add))
        if self.dry_run:
            return
        if current is not None:
            self._claude("remove", "--scope", "user", SERVER_NAME)
        self._claude(*add)
        self._remember(
            {
                "client": client.key,
                "what": "mcp",
                "path": str(self.home / ".claude.json"),
                "written": desired,
                "previous": current,
            }
        )

    def _install_skill(self, client: Client) -> None:
        path = client.skill_path(self.home)
        text = skill_text()
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            self._say(client, f"skill already at {path} (unchanged)")
            return
        self._say(client, f"{'update' if current else 'write'} skill {path}")
        if not self.dry_run:
            _write_atomic(path, text)
            self._remember(
                {
                    "client": client.key,
                    "what": "skill",
                    "path": str(path),
                    "written": text,
                    "previous": current,
                }
            )

    # -- undo ---------------------------------------------------------------------------------

    def undo(self) -> int:
        data = self._manifest()
        if not data["changes"]:
            self.say("Nothing to undo: setup has not changed anything here.")
            return 0
        clients = {c.key: c for c in CLIENTS}
        kept = []
        for change in data["changes"]:
            client = clients[change["client"]]
            try:
                done = (
                    self._undo_skill(client, change)
                    if change["what"] == "skill"
                    else self._undo_mcp(client, change)
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.failures += 1
                self._say(client, f"not undone: {exc}")
                done = False
            if not done:
                kept.append(change)
        if not self.dry_run:
            if kept:
                data["changes"] = kept
                _write_atomic(self.manifest_path, json.dumps(data, indent=2) + "\n")
            else:
                self.manifest_path.unlink(missing_ok=True)
        return len(data["changes"]) - len(kept)

    def _undo_skill(self, client: Client, change: dict[str, Any]) -> bool:
        path = Path(change["path"])
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current is None:
            self._say(client, f"skill {path} is already gone")
            return True
        if current != change["written"]:
            self._say(client, f"left {path}: it changed after setup wrote it")
            return False
        if change.get("previous") is not None:
            self._say(client, f"restore the earlier {path}")
            if not self.dry_run:
                _write_atomic(path, change["previous"])
            return True
        self._say(client, f"remove skill {path}")
        if not self.dry_run:
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                pass
        return True

    def _undo_mcp(self, client: Client, change: dict[str, Any]) -> bool:
        path = Path(change["path"])
        if client.kind == "toml":
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            rest, ours = _strip_codex_block(text)
            if ours is None:
                self._say(client, f"no setup block in {path}")
                return True
            self._say(client, f"remove MCP server {SERVER_NAME!r} from {path}")
            if not self.dry_run:
                _write_atomic(path, rest)
            return True
        if client.kind == "claude-cli":
            current = self._claude_current()
            if current is None:
                self._say(client, f"MCP server {SERVER_NAME!r} is already gone")
                return True
            if {
                "command": current.get("command"),
                "args": list(current.get("args") or []),
            } != change["written"]:
                self._say(
                    client, f"left MCP server {SERVER_NAME!r}: it changed after setup"
                )
                return False
            self._say(client, f"run: claude mcp remove --scope user {SERVER_NAME}")
            if not self.dry_run:
                self._claude("remove", "--scope", "user", SERVER_NAME)
                previous = change.get("previous")
                if previous is not None:
                    self._claude(
                        "add-json", "--scope", "user", SERVER_NAME, json.dumps(previous)
                    )
            return True
        data = _read_json(path)
        servers = dict(data.get("mcpServers") or {})
        if SERVER_NAME not in servers:
            self._say(client, f"MCP server {SERVER_NAME!r} is already gone from {path}")
            return True
        if servers[SERVER_NAME] != change["written"]:
            self._say(client, f"left {SERVER_NAME!r} in {path}: it changed after setup")
            return False
        previous = change.get("previous")
        if previous is not None:
            servers[SERVER_NAME] = previous
            self._say(client, f"restore the earlier {SERVER_NAME!r} entry in {path}")
        else:
            del servers[SERVER_NAME]
            self._say(client, f"remove MCP server {SERVER_NAME!r} from {path}")
        if servers:
            data["mcpServers"] = servers
        else:
            data.pop("mcpServers", None)
        if not self.dry_run:
            _write_atomic(path, json.dumps(data, indent=2) + "\n")
        return True


def _quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
