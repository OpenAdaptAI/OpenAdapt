"""Write, show and remove the hook settings in one project folder.

Only project-local files are touched:

* ``.openadapt/hooks/run-hook``: the launcher every hook entry calls. It
  pins this Python and this project. If the hook can't start or crashes
  before a tool call, the launcher exits 2, which both agents treat as a
  block; otherwise a broken hook would let the call through.
* Claude Code: ``.claude/settings.local.json`` (project scope, not shared;
  the launcher path is machine-specific). Events ``PreToolUse``,
  ``PostToolUse``, ``PostToolUseFailure`` and ``Stop``.
* Codex: ``.codex/hooks.json``. Events ``PreToolUse``, ``PostToolUse`` and
  ``Stop``. Codex runs project hooks only after the project and the hook
  are trusted (``/hooks`` in the CLI); a changed hook needs trusting again.

Existing settings are kept: OpenAdapt's entries are the ones whose command
runs this project's launcher, and ``uninstall`` removes only those. A file
the install created and that is empty afterwards is removed.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.hooks import HOOKS_DIR, SYSTEMS_FILE
from openadapt_flow.hooks.agents import AGENTS
from openadapt_flow.hooks.systems import SystemsError, load_systems

MANIFEST_SCHEMA = "openadapt.hooks-install/v1"
LAUNCHER = "run-hook"
TIMEOUT_S = 120
CONFIG_FILES = {
    "claude-code": Path(".claude") / "settings.local.json",
    "codex": Path(".codex") / "hooks.json",
}
EVENTS = {
    "claude-code": ("PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"),
    "codex": ("PreToolUse", "PostToolUse", "Stop"),
}
#: The tools whose calls can reach a declared system. Claude Code treats a
#: matcher with regex characters as an unanchored regular expression.
MATCHERS = {
    "claude-code": "Bash|Write|Edit|MultiEdit|NotebookEdit|WebFetch|mcp__.*",
    "codex": "^(Bash|exec_command|apply_patch|Edit|Write|mcp__.*)$",
}
STRICT_REFUSAL = (
    "--strict needs the `openadapt wrap -- <command>` broker, which holds the "
    "credentials and gives the agent only scoped tools. That broker is not "
    "available for databases, HTTP APIs or MCP servers in this release, so "
    "--strict is refused rather than installed as advisory hooks under a strict "
    "name. Install without --strict for advisory hooks."
)


class InstallError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def launcher_path(project: Path) -> Path:
    return project / HOOKS_DIR / LAUNCHER


def _launcher_text(project: Path, python: str) -> str:
    py = shlex.quote(python)
    root = shlex.quote(str(project))
    return f"""#!/bin/sh
# Written by `openadapt hooks install`; `openadapt hooks uninstall` removes it.
# Runs one OpenAdapt hook event: $1 is the agent, $2 the event, stdin the
# agent's hook input. If the hook can't run before a tool call, the call is
# blocked (exit 2), never let through.
{py} -m openadapt_flow.hooks run --project {root} --agent "$1" --event "$2"
rc=$?
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
  echo "OpenAdapt's hook failed to run (exit $rc). Run: openadapt hooks status" >&2
  if [ "$2" = "PreToolUse" ]; then exit 2; fi
  exit 1
fi
exit "$rc"
"""


def _entry(agent: str, event: str, launcher: Path) -> dict[str, Any]:
    command = f"{shlex.quote(str(launcher))} {agent} {event}"
    handler = {
        "type": "command",
        "command": command,
        "timeout": TIMEOUT_S,
        "statusMessage": "OpenAdapt: checking writes to declared systems",
    }
    group: dict[str, Any] = {"hooks": [handler]}
    if event != "Stop":
        group = {"matcher": MATCHERS[agent], **group}
    return group


def _ours(handler: Any, launcher: Path) -> bool:
    return isinstance(handler, dict) and str(launcher) in str(
        handler.get("command", "")
    )


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as exc:
        raise InstallError(f"{path} is not valid JSON; fix it first ({exc})") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path} is not a JSON object")
    return data


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".openadapt.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _strip(data: dict[str, Any], launcher: Path) -> tuple[dict[str, Any], int]:
    """Remove OpenAdapt's handlers; return the rest and how many were removed."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return data, 0
    removed = 0
    kept_events: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            kept_events[event] = groups
            continue
        kept_groups = []
        for group in groups:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            rest = [h for h in handlers if not _ours(h, launcher)]
            removed += len(handlers) - len(rest)
            if rest:
                kept_groups.append({**group, "hooks": rest})
            elif len(handlers) == 0:
                kept_groups.append(group)
        if kept_groups:
            kept_events[event] = kept_groups
    out = dict(data)
    if kept_events:
        out["hooks"] = kept_events
    else:
        out.pop("hooks", None)
    return out, removed


def _check_project(project: Path) -> Path:
    project = Path(project).resolve()
    if not project.is_dir():
        raise InstallError(f"{project} is not a folder")
    if project == Path.home().resolve():
        raise InstallError(
            "the project folder is your home folder, where .claude and .codex are "
            "your global settings; install from the project's own folder"
        )
    return project


def _manifest_path(project: Path) -> Path:
    return project / HOOKS_DIR / "install.json"


def _manifest(project: Path) -> dict[str, Any]:
    path = _manifest_path(project)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": MANIFEST_SCHEMA, "agents": {}}
    return (
        data
        if isinstance(data, dict)
        else {"schema_version": MANIFEST_SCHEMA, "agents": {}}
    )


def install(
    project: Path,
    agent: str,
    *,
    strict: bool = False,
    python: Optional[str] = None,
) -> dict[str, Any]:
    if strict:
        raise InstallError(STRICT_REFUSAL)
    if agent not in AGENTS:
        raise InstallError(f"--agent is one of {', '.join(AGENTS)}")
    project = _check_project(project)
    try:
        systems = load_systems(project / SYSTEMS_FILE)
    except SystemsError as exc:
        raise InstallError(
            f"{exc}. Write {SYSTEMS_FILE} first (see docs/HOOKS.md); the hooks "
            "check only the systems it declares."
        ) from exc
    folder = project / HOOKS_DIR
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    (folder / ".gitignore").write_text(
        "# OpenAdapt hook state, private snapshots and receipts: local only.\n*\n",
        encoding="utf-8",
    )
    launcher = launcher_path(project)
    launcher.write_text(
        _launcher_text(project, python or sys.executable), encoding="utf-8"
    )
    launcher.chmod(0o755)
    path = project / CONFIG_FILES[agent]
    existed = path.exists()
    data, _ = _strip(_load(path), launcher)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(f"{path}: 'hooks' is not an object")
    for event in EVENTS[agent]:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise InstallError(f"{path}: hooks.{event} is not a list")
        groups.append(_entry(agent, event, launcher))
    _save(path, data)
    manifest = _manifest(project)
    previous = manifest.get("agents", {}).get(agent, {})
    manifest.setdefault("agents", {})[agent] = {
        "file": str(CONFIG_FILES[agent]),
        "created_file": previous.get("created_file", not existed),
        "events": list(EVENTS[agent]),
        "installed_at": _now(),
    }
    manifest["python"] = python or sys.executable
    manifest["schema_version"] = MANIFEST_SCHEMA
    _manifest_path(project).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "agent": agent,
        "file": str(path),
        "launcher": str(launcher),
        "systems": [s.name for s in systems.systems],
        "events": list(EVENTS[agent]),
    }


def uninstall(project: Path, agent: Optional[str] = None) -> list[str]:
    project = _check_project(project)
    launcher = launcher_path(project)
    manifest = _manifest(project)
    agents = [agent] if agent else list(AGENTS)
    done: list[str] = []
    for name in agents:
        path = project / CONFIG_FILES[name]
        if not path.exists():
            manifest.get("agents", {}).pop(name, None)
            continue
        data, removed = _strip(_load(path), launcher)
        created = manifest.get("agents", {}).get(name, {}).get("created_file", False)
        if not data and created:
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                pass
            done.append(f"{name}: removed {path} (OpenAdapt created it)")
        elif removed:
            _save(path, data)
            done.append(f"{name}: removed {removed} OpenAdapt entries from {path}")
        manifest.get("agents", {}).pop(name, None)
    if not manifest.get("agents"):
        launcher.unlink(missing_ok=True)
        _manifest_path(project).unlink(missing_ok=True)
        done.append(
            f"removed {launcher}; receipts and state stay in {project / HOOKS_DIR}"
        )
    else:
        _manifest_path(project).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    return done


def status(project: Path) -> dict[str, Any]:
    project = Path(project).resolve()
    launcher = launcher_path(project)
    manifest = _manifest(project)
    agents: dict[str, Any] = {}
    for name in AGENTS:
        path = project / CONFIG_FILES[name]
        found: list[str] = []
        if path.exists():
            try:
                hooks = _load(path).get("hooks") or {}
            except InstallError:
                hooks = {}
            for event, groups in hooks.items() if isinstance(hooks, dict) else []:
                for group in groups if isinstance(groups, list) else []:
                    handlers = group.get("hooks") if isinstance(group, dict) else None
                    if isinstance(handlers, list) and any(
                        _ours(h, launcher) for h in handlers
                    ):
                        found.append(event)
        missing = [e for e in EVENTS[name] if e not in found]
        agents[name] = {
            "installed": bool(found),
            "file": str(path),
            "events": sorted(set(found)),
            "missing_events": missing if found else [],
        }
        if name == "codex" and found:
            agents[name]["note"] = (
                "Codex runs project hooks only once you trust them with /hooks; "
                "this status can't see that."
            )
    python = manifest.get("python")
    try:
        systems = load_systems(project / SYSTEMS_FILE)
        systems_info: Any = [
            {"name": s.name, "kind": s.kind, "writes": [w.name for w in s.writes]}
            for s in systems.systems
        ]
        systems_error = None
    except SystemsError as exc:
        systems_info, systems_error = [], str(exc)
    return {
        "project": str(project),
        "agents": agents,
        "launcher": {
            "path": str(launcher),
            "present": launcher.is_file() and os.access(launcher, os.X_OK),
            "python": python,
            "python_present": bool(python) and Path(str(python)).exists(),
        },
        "systems": systems_info,
        "systems_error": systems_error,
        "advisory": True,
    }
