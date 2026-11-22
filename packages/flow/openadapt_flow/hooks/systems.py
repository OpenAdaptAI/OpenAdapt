"""``openadapt.systems.yaml``: the systems of record a coding agent may write.

Each system says how a tool call reaches it, which writes are in scope, how
each one is read back, and who approves it::

    schema_version: openadapt.systems/v1
    unknown_write: block            # or ask: a write no entry covers
    systems:
      - name: clinic-db
        kind: database
        url: sqlite:///data/clinic.sqlite
        commands: [sqlite3]
        writes:
          - name: add-note
            sql: "INSERT INTO notes \\(patient_id, note\\) VALUES \\('(?P<patient_id>[^']*)', '(?P<note>[^']*)'\\)"
            contract: contracts/note.yaml
            approval: allow
      - name: notes-api
        kind: http
        base_url: http://127.0.0.1:8080
        writes:
          - name: add-note
            method: POST
            path: "/api/notes"
            contract: contracts/note-api.yaml
            approval: ask
      - name: crm
        kind: mcp
        tools: ["mcp__crm__.*"]
        read_tools: ["mcp__crm__get_.*"]
        writes:
          - name: update-contact
            tool: mcp__crm__update_contact
            contract: contracts/contact.yaml

A write's values come from the call: the SQL pattern's named groups, the
path pattern's named groups and the JSON body's top-level values, or the MCP
tool's top-level arguments. Each contract is an ``openadapt.verify/v1`` file
(the one ``verify-run`` reads) and must be satisfiable from those values.
Patterns are Python regular expressions matched against the whole text.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SYSTEMS_SCHEMA = "openadapt.systems/v1"
Approval = Literal["allow", "ask", "block"]
_NAME = r"^[a-z][a-z0-9_-]{0,47}$"


class SystemsError(ValueError):
    """The systems file is missing, unreadable or invalid."""


def _compiles(pattern: str, what: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{what} is not a regular expression: {exc}") from exc
    return pattern


class Write(BaseModel):
    """One in-scope write: how a call is recognized, read back and approved."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME)
    #: database: the whole statement (trailing ``;`` and spaces ignored).
    sql: Optional[str] = None
    #: http: the method and the path (without the query string).
    method: Optional[str] = None
    path: Optional[str] = None
    #: mcp: the tool name.
    tool: Optional[str] = None
    #: The ``openadapt.verify/v1`` contract, relative to the systems file.
    contract: str
    #: allow: no prompt from OpenAdapt (the agent's own permission rules
    #: still apply); ask: a person approves this call; block: never.
    approval: Approval = "ask"
    #: With ``approval: allow``, also tell Claude Code to skip its own
    #: permission prompt for this call. Off unless set here.
    auto_approve: bool = False

    @model_validator(mode="after")
    def _check(self) -> "Write":
        for what in ("sql", "path", "tool"):
            value = getattr(self, what)
            if value is not None:
                _compiles(value, f"writes[{self.name}].{what}")
        if self.method is not None:
            self.method = self.method.upper()
        if self.auto_approve and self.approval != "allow":
            raise ValueError(f"writes[{self.name}]: auto_approve needs approval: allow")
        return self


class System(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME)
    kind: Literal["database", "http", "mcp"]
    #: database: ``sqlite:///path`` (relative to the project) or a server URL.
    url: Optional[str] = None
    #: database: the CLIs that reach it (``sqlite3``, ``psql``, ``mysql``).
    commands: list[str] = Field(default_factory=list)
    #: http: the API base, such as ``http://127.0.0.1:8080``.
    base_url: Optional[str] = None
    #: mcp: tool-name patterns of the server's tools.
    tools: list[str] = Field(default_factory=list)
    #: mcp: tool-name patterns that only read. Every other tool writes.
    read_tools: list[str] = Field(default_factory=list)
    #: Other strings that name this system in a command (a database name, a
    #: host alias, an environment variable such as ``$DATABASE_URL``).
    aliases: list[str] = Field(default_factory=list)
    writes: list[Write] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "System":
        if self.kind == "database" and not self.url:
            raise ValueError(f"system {self.name}: a database needs url")
        if self.kind == "http" and not self.base_url:
            raise ValueError(f"system {self.name}: an http system needs base_url")
        if self.kind == "mcp" and not self.tools:
            raise ValueError(f"system {self.name}: an mcp system needs tools")
        for pattern in [*self.tools, *self.read_tools]:
            _compiles(pattern, f"system {self.name} tool pattern")
        need = {"database": "sql", "http": "path", "mcp": "tool"}[self.kind]
        for write in self.writes:
            if getattr(write, need) is None:
                raise ValueError(
                    f"system {self.name}: writes[{write.name}] needs {need}"
                )
            if self.kind == "http" and write.method is None:
                raise ValueError(
                    f"system {self.name}: writes[{write.name}] needs method"
                )
        names = [w.name for w in self.writes]
        if len(set(names)) != len(names):
            raise ValueError(f"system {self.name}: write names repeat")
        return self

    def sqlite_path(self, root: Path) -> Optional[Path]:
        """The SQLite file a ``sqlite:///`` URL names, resolved from ``root``."""
        if not self.url or not self.url.startswith("sqlite:///"):
            return None
        raw = self.url[len("sqlite:///") :]
        path = Path(raw)
        return path if path.is_absolute() else (root / path)

    def markers(self, root: Path) -> list[str]:
        """Strings whose presence in a command means it reaches this system."""
        found: list[str] = list(self.aliases)
        if self.kind == "database" and self.url:
            found.append(self.url)
            sqlite = self.sqlite_path(root)
            if sqlite is not None:
                raw = self.url[len("sqlite:///") :]
                found += [raw, str(sqlite), str(sqlite.resolve()), sqlite.name]
            else:
                parts = urlsplit(self.url)
                if parts.hostname and parts.path.strip("/"):
                    found.append(parts.path.strip("/"))
        if self.kind == "http" and self.base_url:
            base = self.base_url.rstrip("/")
            parts = urlsplit(base)
            found.append(base)
            if parts.netloc:
                found.append(parts.netloc)
                if parts.hostname in ("127.0.0.1", "localhost") and parts.port:
                    for host in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]"):
                        found.append(f"{host}:{parts.port}")
        return sorted({m for m in found if m}, key=len, reverse=True)


class SystemsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.systems/v1"]
    #: A write to a declared system that no entry covers.
    unknown_write: Literal["block", "ask"] = "block"
    systems: list[System] = Field(min_length=1)
    #: Where the file was read from (set by :func:`load_systems`).
    source: Optional[Path] = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _check(self) -> "SystemsFile":
        names = [s.name for s in self.systems]
        if len(set(names)) != len(names):
            raise ValueError("system names repeat")
        return self

    @property
    def root(self) -> Path:
        return self.source.parent if self.source is not None else Path.cwd()

    def contract_path(self, write: Write) -> Path:
        path = Path(write.contract)
        return path if path.is_absolute() else self.root / path


def load_systems(path: Path) -> SystemsFile:
    import yaml

    if not path.is_file():
        raise SystemsError(f"{path} is not a file")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SystemsError(f"cannot read {path}: {exc}") from exc
    try:
        parsed = SystemsFile.model_validate(data)
    except ValidationError as exc:
        raise SystemsError(
            f"{path} is not a valid {SYSTEMS_SCHEMA} file: {exc}"
        ) from exc
    parsed.source = path.resolve()
    return parsed
