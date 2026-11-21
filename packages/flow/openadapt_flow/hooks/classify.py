"""Which declared system a tool call reaches, and whether it writes.

Fail closed: a call that names a declared system and is not provably a read
is a write. A recognized in-scope write gets its values from the call; any
other write to a declared system is an ``unknown_write``. Recognized:

* **Shell** (Claude Code ``Bash``, Codex ``Bash``/``exec_command``): a
  single ``sqlite3``, ``psql`` or ``mysql`` command with one SQL statement,
  or a single ``curl`` command (a ``| jq``-style pipe after a read is
  allowed). Anything else that names the system (a script, a redirect, a
  compound command) is an unknown write.
* **File tools** (``Write``, ``Edit``, ``MultiEdit``, ``NotebookEdit``,
  Codex ``apply_patch``) on a declared SQLite file: an unknown write.
* **WebFetch** on a declared HTTP base: a read (it only sends GET).
* **MCP tools** (``mcp__<server>__<tool>``) matched by a system's patterns.

A call that reaches OpenAdapt's own files (the systems file, the contracts,
``.openadapt/hooks``, the agents' hook settings) or runs ``openadapt hooks
approve|uninstall`` is ``protected``: the agent can't widen its own scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Optional
from urllib.parse import urlsplit

from openadapt_flow.hooks import HOOKS_DIR, SYSTEMS_FILE
from openadapt_flow.hooks.systems import System, SystemsFile, Write

Kind = Literal["none", "read", "write", "unknown_write", "protected"]

SHELL_TOOLS = ("Bash", "exec_command", "shell", "local_shell")
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch")
DB_COMMANDS = ("sqlite3", "psql", "mysql", "mariadb")
_OPERATORS = set(";&|<>()")
_READ_SQL = re.compile(r"^\s*(select|explain\s+(query\s+plan\s+)?select|values)\b", re.I)
_WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|replace|upsert|merge|create|drop|alter|truncate|"
    r"attach|detach|pragma|vacuum|reindex|grant|revoke|copy|call|do|lock|"
    r"set|begin|commit|into)\b",
    re.I,
)
_READ_DOT = re.compile(r"^\s*\.(tables|schema|indexes|indices|headers|mode)\b[^;]*$")
_READ_SHELL = ("cat", "ls", "head", "tail", "grep", "rg", "wc", "less", "stat", "file")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
_CURL_VALUE = {
    "-X", "--request", "-d", "--data", "--data-raw", "--data-binary",
    "--data-ascii", "--data-urlencode", "--json", "-H", "--header", "-o",
    "--output", "-u", "--user", "-A", "--user-agent", "-e", "--referer", "-b",
    "--cookie", "-c", "--cookie-jar", "-w", "--write-out", "-m", "--max-time",
    "--connect-timeout", "--retry", "--url", "-F", "--form", "-T",
    "--upload-file", "--cacert", "--cert", "--key", "-x", "--proxy",
}
_CURL_BODY = {
    "-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--json",
    "--data-urlencode",
}
_CURL_UPLOAD = {"-F", "--form", "-T", "--upload-file"}


@dataclass
class Intent:
    kind: Kind
    system: Optional[System] = None
    write: Optional[Write] = None
    params: dict[str, Any] = field(default_factory=dict)
    #: One plain sentence, without values from the call.
    detail: str = ""

    @property
    def touches_system(self) -> bool:
        return self.kind in ("write", "unknown_write")


def call_digest(tool_name: str, tool_input: Any) -> str:
    """The same call, byte for byte, gets the same digest (approvals, holds)."""
    text = json.dumps(
        {"tool": tool_name, "input": tool_input},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _command_text(tool_input: Mapping[str, Any]) -> str:
    command = tool_input.get("command", tool_input.get("cmd", ""))
    if isinstance(command, list):
        return shlex.join(str(part) for part in command)
    return str(command or "")


def _split(text: str) -> Optional[list[list[str]]]:
    """Shell words, as segments between ``|``; None for anything else."""
    if "`" in text or "$(" in text or "\n" in text.strip():
        return None
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = [[]]
    for word in words:
        if word and set(word) <= _OPERATORS:
            if word != "|":
                return None
            segments.append([])
            continue
        segments[-1].append(word)
    if any(not seg for seg in segments):
        return None
    return segments


def _protected_paths(systems: Optional[SystemsFile], root: Path) -> list[Path]:
    paths = [
        root / SYSTEMS_FILE,
        root / HOOKS_DIR,
        root / ".claude" / "settings.json",
        root / ".claude" / "settings.local.json",
        root / ".codex" / "hooks.json",
        root / ".codex" / "config.toml",
    ]
    if systems is not None:
        paths.append(systems.source or root / SYSTEMS_FILE)
        for system in systems.systems:
            paths += [systems.contract_path(w) for w in system.writes]
    return paths


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _protected_marker(text: str, root: Path, systems: Optional[SystemsFile]) -> bool:
    if re.search(r"\bhooks\s+(approve|uninstall|install)\b", text):
        return True
    for path in _protected_paths(systems, root):
        for marker in {str(path), _relative(path, root)}:
            if marker and marker != "." and marker in text:
                return True
    return False


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except (ValueError, OSError):
        return ""


def _sql_of(command: list[str]) -> Optional[str]:
    """The one SQL text a database CLI command runs, or None if unclear."""
    name = os.path.basename(command[0])
    args = command[1:]
    if name == "sqlite3":
        positional = []
        skip = False
        for arg in args:
            if skip:
                skip = False
                continue
            if arg in ("-cmd", "-init"):
                return None
            if arg in ("-separator", "-nullvalue", "-newline"):
                skip = True
                continue
            if arg.startswith("-"):
                continue
            positional.append(arg)
        return positional[1] if len(positional) == 2 else None
    flag = {"psql": ("-c", "--command"), "mysql": ("-e", "--execute")}.get(
        "mysql" if name == "mariadb" else name
    )
    if flag is None:
        return None
    found: list[str] = []
    for i, arg in enumerate(args):
        if arg in flag and i + 1 < len(args):
            found.append(args[i + 1])
        elif any(arg.startswith(f + "=") for f in flag if f.startswith("--")):
            found.append(arg.split("=", 1)[1])
        elif arg in ("-f", "--file", "--source"):
            return None
    return found[0] if len(found) == 1 else None


def _one_statement(sql: str) -> Optional[str]:
    text = sql.strip().rstrip(";").strip()
    # A ``;`` left inside, outside quotes, is a second statement.
    quote = ""
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == ";":
            return None
    return text or None


def _is_read_sql(sql: str) -> bool:
    if _READ_DOT.match(sql):
        return True
    # Keywords inside string literals don't count; strip them first.
    bare = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return bool(_READ_SQL.match(bare)) and not _WRITE_WORDS.search(bare)


def _database(system: System, segments: list[list[str]], text: str) -> Intent:
    command = segments[0]
    allowed = system.commands or list(DB_COMMANDS)
    name = os.path.basename(command[0])
    if len(segments) != 1 or name not in allowed:
        return Intent(
            "unknown_write",
            system,
            detail=f"a command that reaches {system.name} and isn't a single "
            f"{'/'.join(allowed)} statement",
        )
    sql = _sql_of(command)
    statement = _one_statement(sql) if sql is not None else None
    if statement is None:
        return Intent(
            "unknown_write",
            system,
            detail=f"a {name} command whose SQL isn't one statement on the command line",
        )
    if _is_read_sql(statement):
        return Intent("read", system, detail="a read-only SQL statement")
    for write in system.writes:
        found = re.fullmatch(write.sql or "", statement, re.S)
        if found:
            return Intent(
                "write", system, write, dict(found.groupdict()), f"write {write.name}"
            )
    return Intent(
        "unknown_write", system, detail=f"a SQL write to {system.name} no entry declares"
    )


def _curl(system: System, segments: list[list[str]]) -> Intent:
    command = segments[0]
    if os.path.basename(command[0]) != "curl":
        return Intent(
            "unknown_write",
            system,
            detail=f"a command that reaches {system.name} and isn't a single curl call",
        )
    method: Optional[str] = None
    body: Optional[str] = None
    urls: list[str] = []
    get = False
    args = command[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        value = args[i + 1] if i + 1 < len(args) else None
        long_value = arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else None
        flag = arg.split("=", 1)[0] if long_value is not None else arg
        if flag in _CURL_UPLOAD:
            return Intent("unknown_write", system, detail="a curl upload")
        if flag in ("-G", "--get"):
            get = True
        elif flag in ("-I", "--head"):
            method = "HEAD"
        if flag in _CURL_VALUE:
            got = long_value if long_value is not None else value
            if got is None:
                return Intent("unknown_write", system, detail="a curl flag without a value")
            if flag in ("-X", "--request"):
                method = got.upper()
            elif flag in _CURL_BODY:
                if body is not None or got.startswith("@"):
                    return Intent(
                        "unknown_write", system, detail="a curl body from a file or in parts"
                    )
                body = got
            elif flag == "--url":
                urls.append(got)
            i += 1 if long_value is not None else 2
            continue
        if not arg.startswith("-"):
            urls.append(arg)
        elif re.fullmatch(r"-[a-zA-Z]{2,}", arg) and set(arg[1:]) & set("XdHouAebcwmFTx"):
            return Intent("unknown_write", system, detail="combined curl flags")
        i += 1
    base = urlsplit(system.base_url or "")
    mine = [u for u in urls if urlsplit(u if "://" in u else "http://" + u).netloc in _netlocs(base)]
    if len(urls) != 1 or len(mine) != 1:
        return Intent(
            "unknown_write", system, detail=f"a curl call that reaches {system.name} unclearly"
        )
    if method is None:
        method = "GET" if (body is None or get) else "POST"
    if method in ("GET", "HEAD", "OPTIONS") and (body is None or get):
        return Intent("read", system, detail=f"an HTTP {method}")
    if len(segments) > 1:
        return Intent("unknown_write", system, detail="an HTTP write in a pipeline")
    url = mine[0] if "://" in mine[0] else "http://" + mine[0]
    path = urlsplit(url).path or "/"
    prefix = base.path.rstrip("/")
    if prefix and path.startswith(prefix):
        path = path[len(prefix) :] or "/"
    params = _json_scalars(body)
    for write in system.writes:
        if write.method != method:
            continue
        found = re.fullmatch(write.path or "", path)
        if found:
            return Intent(
                "write",
                system,
                write,
                {**params, **found.groupdict()},
                f"write {write.name}",
            )
    return Intent(
        "unknown_write",
        system,
        detail=f"an HTTP {method} to {system.name} no entry declares",
    )


def _netlocs(base: Any) -> set[str]:
    found = {base.netloc}
    if base.hostname in ("127.0.0.1", "localhost") and base.port:
        found |= {f"{h}:{base.port}" for h in ("127.0.0.1", "localhost", "[::1]")}
    return found


def _json_scalars(body: Optional[str]) -> dict[str, Any]:
    if not body:
        return {}
    try:
        value = json.loads(body)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(k): v
        for k, v in value.items()
        if isinstance(v, (str, int, float, bool)) or v is None
    }


def _shell(text: str, systems: SystemsFile, root: Path) -> Intent:
    hits = [
        s
        for s in systems.systems
        if s.kind in ("database", "http") and any(m in text for m in s.markers(root))
    ]
    if not hits:
        return Intent("none")
    if len(hits) > 1:
        return Intent(
            "unknown_write", hits[0], detail="a command that reaches more than one system"
        )
    system = hits[0]
    segments = _split(text)
    if segments is None:
        return Intent(
            "unknown_write",
            system,
            detail=f"a compound command that reaches {system.name}",
        )
    if any(m in " ".join(seg) for seg in segments[1:] for m in system.markers(root)):
        return Intent("unknown_write", system, detail="a pipeline that names the system twice")
    if system.kind == "database":
        return _database(system, segments, text)
    return _curl(system, segments)


def _file_paths(tool_name: str, tool_input: Mapping[str, Any], root: Path) -> list[Path]:
    paths: list[str] = []
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            paths.append(value)
    if tool_name == "apply_patch" or "*** Begin Patch" in _command_text(tool_input):
        paths += [m.strip() for m in _PATCH_FILE.findall(_command_text(tool_input))]
    out = []
    for raw in paths:
        path = Path(raw)
        out.append(path if path.is_absolute() else root / path)
    return out


def _mcp(tool_name: str, tool_input: Mapping[str, Any], systems: SystemsFile) -> Intent:
    for system in systems.systems:
        if system.kind != "mcp":
            continue
        if not any(re.fullmatch(p, tool_name) for p in system.tools):
            continue
        if any(re.fullmatch(p, tool_name) for p in system.read_tools):
            return Intent("read", system, detail="a read-only MCP tool")
        for write in system.writes:
            if re.fullmatch(write.tool or "", tool_name):
                params = {
                    str(k): v
                    for k, v in tool_input.items()
                    if isinstance(v, (str, int, float, bool)) or v is None
                }
                return Intent("write", system, write, params, f"write {write.name}")
        return Intent(
            "unknown_write",
            system,
            detail=f"an MCP tool of {system.name} no entry declares",
        )
    return Intent("none")


def classify(
    tool_name: str,
    tool_input: Mapping[str, Any],
    systems: SystemsFile,
    root: Optional[Path] = None,
) -> Intent:
    """What this one tool call does to the declared systems."""
    root = root or systems.root
    if not isinstance(tool_input, Mapping):
        tool_input = {}
    if tool_name.startswith("mcp__"):
        return _mcp(tool_name, tool_input, systems)
    if tool_name in SHELL_TOOLS:
        text = _command_text(tool_input)
        if _protected_marker(text, root, systems):
            words = _split(text)
            if (
                words is not None
                and len(words) == 1
                and os.path.basename(words[0][0]) in _READ_SHELL
            ):
                return Intent("read", detail="reads OpenAdapt's own files")
            return Intent("protected", detail="changes OpenAdapt's own hook files")
        return _shell(text, systems, root)
    if tool_name in FILE_TOOLS:
        paths = _file_paths(tool_name, tool_input, root)
        protected = _protected_paths(systems, root)
        for path in paths:
            if any(path.resolve() == p.resolve() or _under(path, p) for p in protected):
                return Intent("protected", detail="edits OpenAdapt's own hook files")
        for system in systems.systems:
            sqlite = system.sqlite_path(root)
            if sqlite is None:
                continue
            for path in paths:
                if path.resolve().parent == sqlite.resolve().parent and path.name.startswith(
                    sqlite.name
                ):
                    return Intent(
                        "unknown_write", system, detail=f"a file edit of {system.name}"
                    )
        return Intent("none")
    if tool_name == "WebFetch":
        url = str(tool_input.get("url") or "")
        for system in systems.systems:
            if system.kind == "http" and any(m in url for m in system.markers(root)):
                return Intent("read", system, detail="WebFetch sends GET only")
        return Intent("none")
    return Intent("none")
