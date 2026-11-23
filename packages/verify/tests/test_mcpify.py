"""End-to-end tests for openadapt_verify.mcpify."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from openadapt_verify import (
    McpifyServer,
    ReadbackResult,
    WorkflowToolSpec,
    generate_mcp_client_config,
    load_workflow_spec,
    load_workflow_specs,
    verify_mcpify_receipt,
)
from openadapt_verify.mcpify import (
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    MCP_PROTOCOL_VERSION,
)


def _sample_spec_dict(db_path: Path) -> dict[str, Any]:
    return {
        "schema_version": "openadapt.oapack/v1",
        "name": "record_ledger_entry",
        "description": "Deterministically submit a ledger entry and verify via read-back.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entry_code": {"type": "string", "minLength": 1},
                "quantity": {"type": "integer", "minimum": 1},
                "status": {"type": "string", "enum": ["pending", "posted"]},
            },
            "required": ["entry_code", "quantity"],
            "additionalProperties": False,
        },
        "steps": [
            {"op": "focus_field", "target": "entry_code_input"},
            {"op": "type_text", "value": "{entry_code}"},
            {"op": "focus_field", "target": "quantity_input"},
            {"op": "type_text", "value": "{quantity}"},
            {"op": "click", "target": "submit_button"},
        ],
        "readback_contract": {
            "kind": "sqlite",
            "database": str(db_path),
            "table": "ledger_entries",
            "match": {
                "entry_code": "{entry_code}",
                "quantity": "{quantity}",
            },
            "expected_count": 1,
        },
        "safety_policy": {
            "fail_closed": True,
            "require_readback": True,
            "require_signed_receipt": True,
            "handoff_action": "Verify the ledger table manually before retrying.",
            "escalation_contact": "ops-oncall",
        },
    }


def _init_sqlite_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE ledger_entries ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "entry_code TEXT NOT NULL, "
            "quantity INTEGER NOT NULL)"
        )


def _run_stdio_session(
    server: McpifyServer, messages: list[str]
) -> list[dict[str, Any]]:
    stdin = io.StringIO("\n".join(messages) + "\n")
    stdout = io.StringIO()
    server.serve_stdio(stdin=stdin, stdout=stdout)
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def test_load_workflow_spec_and_fail_closed_validation(tmp_path: Path) -> None:
    db_path = tmp_path / "store.sqlite"
    spec_path = tmp_path / "entry.oapack.json"
    spec_data = _sample_spec_dict(db_path)
    spec_path.write_text(json.dumps(spec_data), encoding="utf-8")

    spec = load_workflow_spec(spec_path)
    assert spec.name == "record_ledger_entry"
    assert spec.safety_policy.fail_closed is True
    assert spec.to_mcp_tool()["inputSchema"]["required"] == ["entry_code", "quantity"]

    # Reject fail_closed=False (fail-closed is non-negotiable).
    unsafe = dict(spec_data)
    unsafe["safety_policy"] = {"fail_closed": False}
    unsafe_path = tmp_path / "unsafe.oapack.json"
    unsafe_path.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(ValueError, match="fail_closed must be true"):
        load_workflow_spec(unsafe_path)

    # Multi-tool pack loading.
    second = dict(spec_data)
    second["name"] = "archive_ledger_entry"
    pack_path = tmp_path / "pack.oapack.json"
    pack_path.write_text(json.dumps({"tools": [spec_data, second]}), encoding="utf-8")
    specs = load_workflow_specs(pack_path)
    assert [s.name for s in specs] == ["record_ledger_entry", "archive_ledger_entry"]


def test_stdio_roundtrip_verified_with_ed25519_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "store.sqlite"
    _init_sqlite_db(db_path)
    spec_path = tmp_path / "entry.oapack.json"
    spec_path.write_text(json.dumps(_sample_spec_dict(db_path)), encoding="utf-8")
    spec = load_workflow_spec(spec_path)

    def deterministic_writer(
        tool_spec: WorkflowToolSpec, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        del tool_spec
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO ledger_entries (entry_code, quantity) VALUES (?, ?)",
                (args["entry_code"], args["quantity"]),
            )
        return {"written": True, "llm_calls": 0}

    server = McpifyServer(
        spec,
        server_name="ledger-mcp",
        runner=deterministic_writer,
    )

    responses = _run_stdio_session(
        server,
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "clientInfo": {"name": "test-client", "version": "1.0"},
                    },
                }
            ),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "record_ledger_entry",
                        "arguments": {"entry_code": "ORD-1001", "quantity": 4},
                    },
                }
            ),
        ],
    )

    # Notification produces no response -> 3 responses for ids 1, 2, 3.
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert server.initialized is True

    init_result = responses[0]["result"]
    assert init_result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert init_result["serverInfo"]["name"] == "ledger-mcp"

    list_result = responses[1]["result"]
    assert len(list_result["tools"]) == 1
    assert list_result["tools"][0]["name"] == "record_ledger_entry"

    call_result = responses[2]["result"]
    assert call_result["isError"] is False
    structured = call_result["structuredContent"]
    assert structured["status"] == "VERIFIED"
    assert structured["llm_calls"] == 0
    assert structured["readback"]["verdict"] == "VERIFIED"

    receipt = structured["receipt"]
    assert receipt["algorithm"] == "Ed25519"
    assert len(receipt["receipt_digest"]) == 64
    assert verify_mcpify_receipt(receipt) is True

    # Tampering with any payload field invalidates the Ed25519 receipt.
    tampered = json.loads(json.dumps(receipt))
    tampered["payload"]["tool_name"] = "forged_tool"
    assert verify_mcpify_receipt(tampered) is False


def test_stdio_silent_fail_readback_mismatch_halts(tmp_path: Path) -> None:
    db_path = tmp_path / "store.sqlite"
    _init_sqlite_db(db_path)
    spec = WorkflowToolSpec.from_dict(_sample_spec_dict(db_path))

    # Runner claims success ("saved": True) but silently persists the wrong quantity.
    def silent_fail_writer(
        tool_spec: WorkflowToolSpec, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        del tool_spec
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO ledger_entries (entry_code, quantity) VALUES (?, ?)",
                (args["entry_code"], 999),
            )
        return {"claimed_success": True, "llm_calls": 0}

    server = McpifyServer(spec, runner=silent_fail_writer)
    responses = _run_stdio_session(
        server,
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "record_ledger_entry",
                        "arguments": {"entry_code": "ORD-2002", "quantity": 7},
                    },
                }
            )
        ],
    )

    assert len(responses) == 1
    result = responses[0]["result"]
    assert result["isError"] is True
    structured = result["structuredContent"]
    assert structured["status"] == "HALT"
    assert structured["verdict"] == "MISMATCH"
    assert structured["receipt"] is None
    assert structured["human_review"]["required"] is True
    assert (
        structured["human_review"]["handoff_action"]
        == "Verify the ledger table manually before retrying."
    )
    assert structured["discrepancies"]


def test_stdio_readback_abstain_halts_fail_closed(tmp_path: Path) -> None:
    # Database path does not exist -> independent read-back must ABSTAIN -> HALT.
    missing_db = tmp_path / "missing.sqlite"
    spec = WorkflowToolSpec.from_dict(_sample_spec_dict(missing_db))
    server = McpifyServer(spec)

    responses = _run_stdio_session(
        server,
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {
                        "name": "record_ledger_entry",
                        "arguments": {"entry_code": "ORD-3003", "quantity": 2},
                    },
                }
            )
        ],
    )

    result = responses[0]["result"]
    assert result["isError"] is True
    structured = result["structuredContent"]
    assert structured["status"] == "HALT"
    assert structured["verdict"] == "ABSTAIN"
    assert structured["receipt"] is None
    assert structured["human_review"]["required"] is True


def test_stdio_invalid_args_and_unknown_tool_fail_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "store.sqlite"
    _init_sqlite_db(db_path)
    spec = WorkflowToolSpec.from_dict(_sample_spec_dict(db_path))

    runner_calls = 0

    def spy_runner(
        tool_spec: WorkflowToolSpec, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        nonlocal runner_calls
        del tool_spec, args
        runner_calls += 1
        return {"llm_calls": 0}

    server = McpifyServer(spec, runner=spy_runner)
    responses = _run_stdio_session(
        server,
        [
            # Missing required argument 'quantity'.
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "record_ledger_entry",
                        "arguments": {"entry_code": "ORD-1"},
                    },
                }
            ),
            # Wrong type for 'quantity' (string instead of integer).
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "record_ledger_entry",
                        "arguments": {"entry_code": "ORD-1", "quantity": "five"},
                    },
                }
            ),
            # Unknown tool name.
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "nonexistent_tool",
                        "arguments": {},
                    },
                }
            ),
            # Unknown JSON-RPC method.
            json.dumps({"jsonrpc": "2.0", "id": 4, "method": "resources/list"}),
            # Malformed JSON line.
            "{not valid json",
        ],
    )

    assert runner_calls == 0
    assert responses[0]["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert "quantity" in responses[0]["error"]["message"]
    assert responses[1]["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert "integer" in responses[1]["error"]["message"]
    assert responses[2]["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert "Unknown tool" in responses[2]["error"]["message"]
    assert responses[3]["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
    assert responses[4]["error"]["code"] == JSONRPC_PARSE_ERROR


def test_nonzero_llm_calls_violates_zero_token_invariant(tmp_path: Path) -> None:
    db_path = tmp_path / "store.sqlite"
    spec = WorkflowToolSpec.from_dict(_sample_spec_dict(db_path))

    def leaky_runner(
        tool_spec: WorkflowToolSpec, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        del tool_spec, args
        return {"llm_calls": 2}

    def always_ok_oracle(
        tool_spec: WorkflowToolSpec,
        args: Mapping[str, Any],
        output: Mapping[str, Any],
    ) -> ReadbackResult:
        del tool_spec, args, output
        return ReadbackResult(verdict="VERIFIED", reason="ok")

    server = McpifyServer(spec, runner=leaky_runner, readback_oracle=always_ok_oracle)
    resp = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "record_ledger_entry",
                "arguments": {"entry_code": "ORD-99", "quantity": 1},
            },
        }
    )
    assert resp is not None
    assert resp["result"]["isError"] is True
    assert resp["result"]["structuredContent"]["status"] == "HALT"
    assert "$0-token" in resp["result"]["structuredContent"]["reason"]


def test_generate_mcp_client_config_claude_and_cursor(tmp_path: Path) -> None:
    db_path = tmp_path / "store.sqlite"
    spec_path = tmp_path / "entry.oapack.json"
    spec_path.write_text(json.dumps(_sample_spec_dict(db_path)), encoding="utf-8")

    claude_cfg = generate_mcp_client_config(
        spec_path, server_name="legacy-ledger", client="claude"
    )
    assert "mcpServers" in claude_cfg
    claude_entry = claude_cfg["mcpServers"]["legacy-ledger"]
    assert claude_entry["command"] == sys.executable
    assert claude_entry["args"][:4] == [
        "-m",
        "openadapt_verify.mcpify",
        "serve",
        "--spec",
    ]
    assert Path(claude_entry["args"][4]) == spec_path.resolve()
    assert "type" not in claude_entry

    cursor_cfg = generate_mcp_client_config(
        spec_path, server_name="legacy-ledger", client="cursor"
    )
    cursor_entry = cursor_cfg["mcpServers"]["legacy-ledger"]
    assert cursor_entry["type"] == "stdio"
    assert cursor_entry["args"] == claude_entry["args"]

    with pytest.raises(ValueError, match="Unsupported client"):
        generate_mcp_client_config(spec_path, client="unknown-client")


def test_cli_serve_and_config_subprocess(tmp_path: Path) -> None:
    json_target = tmp_path / "record.json"
    json_target.write_text(
        json.dumps({"entry_code": "CLI-1", "quantity": 3}), encoding="utf-8"
    )
    spec_data = _sample_spec_dict(tmp_path / "unused.sqlite")
    spec_data["readback_contract"] = {
        "kind": "json_file",
        "path": str(json_target),
        "expected": {"entry_code": "{entry_code}", "quantity": "{quantity}"},
    }
    spec_path = tmp_path / "cli.oapack.json"
    spec_path.write_text(json.dumps(spec_data), encoding="utf-8")

    verify_src = Path(__file__).resolve().parents[1] / "src"
    flow_root = Path(__file__).resolve().parents[2] / "flow"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(verify_src), str(flow_root)])

    # Test `config` subcommand.
    cfg_run = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_verify.mcpify",
            "config",
            "--spec",
            str(spec_path),
            "--client",
            "claude",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert cfg_run.returncode == 0, cfg_run.stderr
    parsed_cfg = json.loads(cfg_run.stdout)
    assert "record_ledger_entry" in parsed_cfg["mcpServers"]

    # Test `serve` subcommand over real stdio pipes.
    stdin_payload = (
        "\n".join(
            [
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                ),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "record_ledger_entry",
                            "arguments": {"entry_code": "CLI-1", "quantity": 3},
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    serve_run = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_verify.mcpify",
            "serve",
            "--spec",
            str(spec_path),
        ],
        input=stdin_payload,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert serve_run.returncode == 0, serve_run.stderr
    lines = [json.loads(line) for line in serve_run.stdout.strip().splitlines()]
    assert [line["id"] for line in lines] == [1, 2, 3]
    call_res = lines[2]["result"]
    assert call_res["isError"] is False
    assert call_res["structuredContent"]["status"] == "VERIFIED"
    assert verify_mcpify_receipt(call_res["structuredContent"]["receipt"]) is True
