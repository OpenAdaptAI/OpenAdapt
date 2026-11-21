"""Unit tests for the coding-agent hooks: inputs, outputs, scope and install."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from openadapt_flow.hooks.agents import HookInput, HookOutput, render
from openadapt_flow.hooks.classify import call_digest, classify, write_key
from openadapt_flow.hooks.engine import Engine
from openadapt_flow.hooks.install import (
    InstallError,
    install,
    launcher_path,
    status,
    uninstall,
)
from openadapt_flow.hooks.systems import SystemsError, SystemsFile, load_systems

SQL = (
    r"INSERT INTO notes \(patient_id, note\) VALUES "
    r"\('(?P<patient_id>[^']*)', '(?P<note>[^']*)'\)"
)


def _systems(tmp_path: Path, **extra) -> SystemsFile:
    doc = {
        "schema_version": "openadapt.systems/v1",
        "systems": [
            {
                "name": "clinic-db",
                "kind": "database",
                "url": "sqlite:///data/clinic.sqlite",
                "commands": ["sqlite3", "psql"],
                "writes": [{"name": "add-note", "sql": SQL, "contract": "c/db.yaml"}],
            },
            {
                "name": "notes-api",
                "kind": "http",
                "base_url": "http://127.0.0.1:8080",
                "writes": [
                    {
                        "name": "add-note",
                        "method": "post",
                        "path": r"/api/patients/(?P<patient_id>[a-z0-9]+)/notes",
                        "contract": "c/api.yaml",
                    }
                ],
            },
            {
                "name": "crm",
                "kind": "mcp",
                "tools": [r"mcp__crm__.*"],
                "read_tools": [r"mcp__crm__get_.*"],
                "writes": [
                    {
                        "name": "update-contact",
                        "tool": "mcp__crm__update_contact",
                        "contract": "c/crm.yaml",
                        "approval": "allow",
                    }
                ],
            },
        ],
        **extra,
    }
    path = tmp_path / "openadapt.systems.yaml"
    path.write_text(yaml.safe_dump(doc))
    return load_systems(path)


def _bash(command: str) -> dict:
    return {"command": command}


# -- systems file -----------------------------------------------------------------


def test_the_systems_file_is_closed_and_checked(tmp_path: Path) -> None:
    systems = _systems(tmp_path)
    assert systems.unknown_write == "block"
    assert systems.systems[1].writes[0].method == "POST"
    assert systems.systems[0].writes[0].approval == "ask"  # the default
    bad = tmp_path / "bad.yaml"
    for doc, needle in [
        ({"schema_version": "openadapt.systems/v1", "systems": []}, "systems"),
        (
            {
                "schema_version": "openadapt.systems/v1",
                "systems": [{"name": "x", "kind": "http"}],
            },
            "base_url",
        ),
        (
            {
                "schema_version": "openadapt.systems/v1",
                "systems": [
                    {
                        "name": "x",
                        "kind": "mcp",
                        "tools": ["mcp__x__.*"],
                        "writes": [{"name": "w", "tool": "(", "contract": "c"}],
                    }
                ],
            },
            "regular expression",
        ),
        (
            {
                "schema_version": "openadapt.systems/v1",
                "systems": [
                    {
                        "name": "x",
                        "kind": "mcp",
                        "tools": ["a"],
                        "writes": [
                            {
                                "name": "w",
                                "tool": "a",
                                "contract": "c",
                                "auto_approve": True,
                            }
                        ],
                    }
                ],
            },
            "auto_approve",
        ),
        ({"schema_version": "openadapt.systems/v2", "systems": []}, "schema_version"),
    ]:
        bad.write_text(yaml.safe_dump(doc))
        with pytest.raises(SystemsError, match=needle):
            load_systems(bad)


# -- classification ---------------------------------------------------------------


@pytest.mark.parametrize(
    "command, kind, write, params",
    [
        (
            'sqlite3 data/clinic.sqlite "INSERT INTO notes (patient_id, note) '
            "VALUES ('p1', 'hi')\"",
            "write",
            "add-note",
            {"patient_id": "p1", "note": "hi"},
        ),
        ('sqlite3 data/clinic.sqlite "SELECT * FROM notes"', "read", None, {}),
        ('sqlite3 data/clinic.sqlite ".tables"', "read", None, {}),
        # A keyword inside a literal doesn't make a read a write ...
        (
            "sqlite3 data/clinic.sqlite \"SELECT * FROM notes WHERE note = 'delete'\"",
            "read",
            None,
            {},
        ),
        # ... but anything not provably a read is a write.
        ('sqlite3 data/clinic.sqlite "DELETE FROM notes"', "unknown_write", None, {}),
        (
            'sqlite3 data/clinic.sqlite "SELECT 1; DELETE FROM notes"',
            "unknown_write",
            None,
            {},
        ),
        ("sqlite3 data/clinic.sqlite < fix.sql", "unknown_write", None, {}),
        ("python fix.py data/clinic.sqlite", "unknown_write", None, {}),
        ("rm data/clinic.sqlite", "unknown_write", None, {}),
        ('sqlite3 data/clinic.sqlite "$(cat fix.sql)"', "unknown_write", None, {}),
        ("ls -la", "none", None, {}),
        ("curl -s http://127.0.0.1:8080/api/notes", "read", None, {}),
        ("curl -s http://127.0.0.1:8080/api/notes | jq .", "read", None, {}),
        ("curl -sI http://localhost:8080/api/notes", "read", None, {}),
        (
            "curl -X POST http://127.0.0.1:8080/api/patients/p9/notes "
            '-d \'{"note": "hi", "extra": {"x": 1}}\'',
            "write",
            "add-note",
            {"patient_id": "p9", "note": "hi"},
        ),
        # A body without -X is a POST.
        (
            'curl localhost:8080/api/patients/p9/notes --data \'{"note": "x"}\'',
            "write",
            "add-note",
            {"patient_id": "p9", "note": "x"},
        ),
        ("curl -X DELETE http://127.0.0.1:8080/api/notes/1", "unknown_write", None, {}),
        (
            "curl -d @body.json http://127.0.0.1:8080/api/notes",
            "unknown_write",
            None,
            {},
        ),
        ("curl -F f=@x http://127.0.0.1:8080/api/notes", "unknown_write", None, {}),
        (
            "wget --post-data=x http://127.0.0.1:8080/api/notes",
            "unknown_write",
            None,
            {},
        ),
        ("curl -s https://example.com", "none", None, {}),
        # Fail-open shapes a simple reading would miss: each is a write.
        (
            "sqlite3 data/clinic.sqlite \"SELECT 1 /* ' */; DELETE FROM notes /* ' */\"",
            "unknown_write",
            None,
            {},
        ),
        (
            'sqlite3 data/clinic.sqlite "SELECT 1 -- ; DELETE"',
            "unknown_write",
            None,
            {},
        ),
        (
            "sqlite3 -cmd 'DELETE FROM notes' data/clinic.sqlite 'SELECT 1'",
            "unknown_write",
            None,
            {},
        ),
        (
            "psql sqlite:///data/clinic.sqlite -c 'SELECT 1' --file=evil.sql",
            "unknown_write",
            None,
            {},
        ),
        (
            "mysql --init-command='DELETE FROM notes' -e 'SELECT 1' data/clinic.sqlite",
            "unknown_write",
            None,
            {},
        ),
        (
            'curl -d\'{"note":"x"}\' http://127.0.0.1:8080/api/notes',
            "unknown_write",
            None,
            {},
        ),
        ("curl -dnote=x http://127.0.0.1:8080/api/notes", "unknown_write", None, {}),
        ("curl -XPOST http://127.0.0.1:8080/api/notes", "unknown_write", None, {}),
        (
            "curl --config=evil.txt http://127.0.0.1:8080/api/notes",
            "unknown_write",
            None,
            {},
        ),
        ("curl -K evil.txt http://127.0.0.1:8080/api/notes", "unknown_write", None, {}),
        ("curl -sSL http://127.0.0.1:8080/api/notes", "read", None, {}),
        (
            'curl -XPOST http://127.0.0.1:8080/api/patients/p9/notes -d \'{"note": "x"}\'',
            "write",
            "add-note",
            {"patient_id": "p9", "note": "x"},
        ),
        ("cat openadapt.systems.yaml", "read", None, {}),
        ("sed -i s/block/allow/ openadapt.systems.yaml", "protected", None, {}),
        ("openadapt hooks approve ap-1", "protected", None, {}),
        ("python -m openadapt_flow.hooks uninstall", "protected", None, {}),
    ],
)
def test_shell_calls_are_classified_fail_closed(
    tmp_path: Path, command: str, kind: str, write, params
) -> None:
    intent = classify("Bash", _bash(command), _systems(tmp_path), tmp_path)
    assert intent.kind == kind, intent.detail
    assert (intent.write.name if intent.write else None) == write
    if params:
        assert intent.params == params


def test_codex_shell_input_may_be_a_list(tmp_path: Path) -> None:
    command = ["sqlite3", "data/clinic.sqlite", "DELETE FROM notes"]
    intent = classify("Bash", {"command": command}, _systems(tmp_path), tmp_path)
    assert intent.kind == "unknown_write"


def test_file_tools_on_the_database_or_hook_files(tmp_path: Path) -> None:
    systems = _systems(tmp_path)
    db = tmp_path / "data" / "clinic.sqlite"
    for name, tool_input, kind in [
        ("Write", {"file_path": str(db), "content": "x"}, "unknown_write"),
        ("Edit", {"file_path": str(db) + "-wal"}, "unknown_write"),
        ("Write", {"file_path": str(tmp_path / "src" / "app.py")}, "none"),
        ("Edit", {"file_path": str(tmp_path / "openadapt.systems.yaml")}, "protected"),
        ("Write", {"file_path": str(tmp_path / "c" / "db.yaml")}, "protected"),
        (
            "Write",
            {"file_path": str(tmp_path / ".claude" / "settings.json")},
            "protected",
        ),
        (
            "apply_patch",
            {"command": "*** Begin Patch\n*** Update File: .codex/hooks.json\n"},
            "protected",
        ),
        (
            "apply_patch",
            {"command": "*** Begin Patch\n*** Add File: data/clinic.sqlite\n"},
            "unknown_write",
        ),
    ]:
        assert classify(name, tool_input, systems, tmp_path).kind == kind, (
            name,
            tool_input,
        )


def test_mcp_and_webfetch(tmp_path: Path) -> None:
    systems = _systems(tmp_path)
    write = classify(
        "mcp__crm__update_contact", {"id": "c1", "phone": "555", "tags": ["x"]}, systems
    )
    assert write.kind == "write" and write.params == {"id": "c1", "phone": "555"}
    assert classify("mcp__crm__get_contact", {"id": "c1"}, systems).kind == "read"
    assert (
        classify("mcp__crm__delete_contact", {"id": "c1"}, systems).kind
        == "unknown_write"
    )
    assert classify("mcp__other__anything", {}, systems).kind == "none"
    fetched = classify("WebFetch", {"url": "http://127.0.0.1:8080/api/notes"}, systems)
    assert fetched.kind == "read"


def test_the_call_digest_is_stable() -> None:
    assert call_digest("Bash", {"a": 1, "b": 2}) == call_digest(
        "Bash", {"b": 2, "a": 1}
    )
    assert call_digest("Bash", {"a": 1}) != call_digest("Bash", {"a": 2})


# -- agent input and output ---------------------------------------------------------


def test_hook_input_is_parsed_and_checked() -> None:
    data = {
        "session_id": "s",
        "cwd": "/p",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_use_id": "toolu_1",
        "permission_mode": "default",
    }
    inp = HookInput.parse("claude-code", "PreToolUse", data)
    assert inp.tool_name == "Bash" and inp.tool_use_id == "toolu_1"
    with pytest.raises(ValueError, match="not 'PostToolUse'"):
        HookInput.parse("claude-code", "PostToolUse", data)
    with pytest.raises(ValueError):
        HookInput.parse("codex", "PreToolUse", ["not", "an", "object"])  # type: ignore[arg-type]


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_pre_tool_use_output(agent: str) -> None:
    assert render(agent, "PreToolUse", HookOutput()) == (None, 0)
    doc, code = render(agent, "PreToolUse", HookOutput("deny", "no"))
    assert code == 0
    assert doc == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "no",
        }
    }
    doc, _ = render(agent, "PreToolUse", HookOutput("ask", "a person decides"))
    # Codex runs the tool on "ask", so it never gets one: it gets a deny.
    expected = "ask" if agent == "claude-code" else "deny"
    assert doc["hookSpecificOutput"]["permissionDecision"] == expected


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_post_and_stop_output(agent: str) -> None:
    doc, _ = render(agent, "PostToolUse", HookOutput(context="VERIFIED"))
    assert doc == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "VERIFIED",
        }
    }
    doc, _ = render(agent, "PostToolUse", HookOutput(context="UNCERTAIN", flag=True))
    assert doc["decision"] == "block" and doc["reason"] == "UNCERTAIN"
    assert render(agent, "Stop", HookOutput()) == (None, 0)
    assert render(agent, "Stop", HookOutput(message="m")) == ({"systemMessage": "m"}, 0)


def _inp(agent: str, event: str, **kw) -> HookInput:
    return HookInput(agent=agent, event=event, session_id="s", cwd="/", **kw)  # type: ignore[arg-type]


def test_pre_check_without_a_systems_file_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "v"))
    out = Engine(tmp_path).handle(
        _inp(
            "claude-code", "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}
        )
    )
    assert out.decision == "deny" and "openadapt.systems.yaml" in out.reason


def test_unknown_write_policy_ask_and_block(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "v"))
    call = {"command": 'sqlite3 data/clinic.sqlite "DROP TABLE notes"'}
    _systems(tmp_path)
    blocked = Engine(tmp_path).handle(
        _inp(
            "claude-code",
            "PreToolUse",
            tool_name="Bash",
            tool_input=call,
            tool_use_id="1",
        )
    )
    assert blocked.decision == "deny" and "no such write" in blocked.reason
    _systems(tmp_path, unknown_write="ask")
    asked = Engine(tmp_path).handle(
        _inp(
            "claude-code",
            "PreToolUse",
            tool_name="Bash",
            tool_input=call,
            tool_use_id="2",
        )
    )
    assert asked.decision == "ask"
    # Allowed by a person but undeclared: no contract, so never verified.
    after = Engine(tmp_path).handle(
        _inp(
            "claude-code",
            "PostToolUse",
            tool_name="Bash",
            tool_input=call,
            tool_use_id="2",
        )
    )
    assert "UNCERTAIN" in after.context and after.flag
    codex = Engine(tmp_path).handle(
        _inp(
            "codex",
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": call["command"] + " "},
        )
    )
    assert codex.decision == "deny" and "openadapt hooks approve ap-" in codex.reason


def test_a_write_whose_contract_needs_more_values_is_blocked(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "v"))
    _systems(tmp_path)
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "crm.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": {
                    "kind": "rest",
                    "base_url": "http://127.0.0.1:9",
                    "records_path": "/r",
                },
                "effects": [
                    {
                        "kind": "record_written",
                        "match": {"id": {"param": "id"}, "email": {"param": "email"}},
                        "expected_count": 1,
                    }
                ],
            }
        )
    )
    out = Engine(tmp_path).handle(
        _inp(
            "claude-code",
            "PreToolUse",
            tool_name="mcp__crm__update_contact",
            tool_input={"id": "c1"},
        )
    )
    assert out.decision == "deny" and "email" in out.reason
    # And a store that can't be read before the write blocks it too.
    out = Engine(tmp_path).handle(
        _inp(
            "claude-code",
            "PreToolUse",
            tool_name="mcp__crm__update_contact",
            tool_input={"id": "c1", "email": "a@b.test"},
        )
    )
    assert out.decision == "deny" and "can't be read before the write" in out.reason


# -- install ----------------------------------------------------------------------


def test_install_merges_and_uninstall_removes_only_ours(tmp_path: Path) -> None:
    _systems(tmp_path)
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    mine = {"type": "command", "command": "echo mine"}
    settings.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(ls)"]},
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [mine]}]},
            }
        )
    )
    done = install(tmp_path, "claude-code", python="/usr/bin/python3")
    assert done["events"] == ["PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"]
    install(tmp_path, "claude-code", python="/usr/bin/python3")  # idempotent
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    pre = data["hooks"]["PreToolUse"]
    assert pre[0] == {"matcher": "Bash", "hooks": [mine]}
    assert (
        len(pre) == 2 and str(launcher_path(tmp_path)) in pre[1]["hooks"][0]["command"]
    )
    assert pre[1]["matcher"].startswith("Bash|") and "mcp__.*" in pre[1]["matcher"]
    assert "matcher" not in data["hooks"]["Stop"][0]
    launcher = launcher_path(tmp_path).read_text()
    assert "/usr/bin/python3 -m openadapt_flow.hooks run" in launcher
    assert "exit 2" in launcher
    assert 'cd "$(dirname "$0")"' in launcher  # no project module shadows Python's
    assert pre[1]["hooks"][0]["onFailure"] == "block"
    assert "onFailure" not in data["hooks"]["PostToolUse"][0]["hooks"][0]
    assert (
        (tmp_path / ".openadapt" / "hooks" / ".gitignore").read_text().endswith("*\n")
    )

    info = status(tmp_path)
    assert info["agents"]["claude-code"]["installed"] is True
    assert info["agents"]["claude-code"]["missing_events"] == []
    assert info["agents"]["codex"]["installed"] is False
    assert [s["name"] for s in info["systems"]] == ["clinic-db", "notes-api", "crm"]

    uninstall(tmp_path)
    data = json.loads(settings.read_text())
    assert data == {
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [mine]}]},
    }
    assert not launcher_path(tmp_path).exists()


def test_codex_install_creates_and_removes_its_file(tmp_path: Path) -> None:
    _systems(tmp_path)
    install(tmp_path, "codex", python="/usr/bin/python3")
    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())["hooks"]
    assert sorted(hooks) == ["PostToolUse", "PreToolUse", "Stop"]
    assert (
        hooks["PreToolUse"][0]["matcher"]
        == "^(Bash|exec_command|apply_patch|Edit|Write|mcp__.*)$"
    )
    assert status(tmp_path)["agents"]["codex"]["note"].startswith(
        "Codex runs project hooks"
    )
    uninstall(tmp_path, "codex")
    assert not (tmp_path / ".codex").exists()


def test_install_refuses_strict_home_and_no_systems(
    tmp_path: Path, monkeypatch
) -> None:
    with pytest.raises(InstallError, match="broker"):
        install(tmp_path, "claude-code", strict=True)
    with pytest.raises(InstallError, match="openadapt.systems.yaml"):
        install(tmp_path, "claude-code")
    monkeypatch.setenv("HOME", str(tmp_path))
    _systems(tmp_path)
    with pytest.raises(InstallError, match="home folder"):
        install(tmp_path, "claude-code")
    assert not (tmp_path / ".claude").exists()


def test_install_refuses_a_broken_settings_file(tmp_path: Path) -> None:
    _systems(tmp_path)
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text("{not json")
    with pytest.raises(InstallError, match="not valid JSON"):
        install(tmp_path, "claude-code")
    assert settings.read_text() == "{not json"


def test_a_hold_is_for_the_write_not_the_wording(tmp_path: Path) -> None:
    systems = _systems(tmp_path)
    command = (
        'sqlite3 data/clinic.sqlite "INSERT INTO notes (patient_id, note) '
        "VALUES ('p1', 'hi')\""
    )
    first = {"command": command, "description": "add the note"}
    reworded = {"command": "  " + command, "description": "try once more", "timeout": 5}
    a = classify("Bash", first, systems, tmp_path)
    b = classify("Bash", reworded, systems, tmp_path)
    assert write_key(a, "Bash", first) == write_key(b, "Bash", reworded)
    other = classify(
        "Bash", {"command": command.replace("'hi'", "'bye'")}, systems, tmp_path
    )
    assert write_key(other, "Bash", {}) != write_key(a, "Bash", first)
    unknown = {"command": 'sqlite3 data/clinic.sqlite "DELETE FROM notes"'}
    u = classify("Bash", unknown, systems, tmp_path)
    assert write_key(u, "Bash", unknown) == write_key(
        u, "Bash", {**unknown, "description": "x"}
    )
