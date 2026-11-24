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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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
    JSONRPC_INVALID_REQUEST,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    MCP_PROTOCOL_VERSION,
    issue_mcpify_receipt,
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


def _handshake_messages() -> list[str]:
    return [
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
    ]


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
        return {"written": True, "writes_applied": 1, "llm_calls": 0}

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
    assert (
        verify_mcpify_receipt(receipt, trusted_public_key=server.public_key_b64) is True
    )

    # Tampering with any payload field invalidates the Ed25519 receipt.
    tampered = json.loads(json.dumps(receipt))
    tampered["payload"]["tool_name"] = "forged_tool"
    assert (
        verify_mcpify_receipt(tampered, trusted_public_key=server.public_key_b64)
        is False
    )

    # Self-vouching forgery with an unrelated Ed25519 key must be rejected.
    attacker_key = Ed25519PrivateKey.generate()
    forged = issue_mcpify_receipt(
        tool_name="steal_funds",
        arguments={"amount": 1000000},
        steps=[{"op": "wire"}],
        readback=ReadbackResult(verdict="VERIFIED", reason="invented"),
        signing_key=attacker_key,
    )
    assert (
        verify_mcpify_receipt(forged, trusted_public_key=server.public_key_b64) is False
    )

    # Different arguments produce different steps_sha256 (hashes interpolated steps, not templates).
    second_resp = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "record_ledger_entry",
                "arguments": {"entry_code": "ORD-1002", "quantity": 9},
            },
        }
    )
    assert second_resp is not None
    receipt2 = second_resp["result"]["structuredContent"]["receipt"]
    assert receipt2["payload"]["steps_sha256"] != receipt["payload"]["steps_sha256"]


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
        return {"claimed_success": True, "writes_applied": 1, "llm_calls": 0}

    server = McpifyServer(spec, runner=silent_fail_writer)
    responses = _run_stdio_session(
        server,
        [
            *_handshake_messages(),
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
            ),
        ],
    )

    assert len(responses) == 2
    result = responses[1]["result"]
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
            *_handshake_messages(),
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
            ),
        ],
    )

    result = responses[1]["result"]
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
    # notifications/initialized without prior initialize must NOT mark server initialized.
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert server.initialized is False
    # Calling tools/call before initialize + notifications/initialized must be rejected.
    early = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "tools/call",
            "params": {
                "name": "record_ledger_entry",
                "arguments": {"entry_code": "ORD-1", "quantity": 1},
            },
        }
    )
    assert early is not None
    assert early["error"]["code"] == JSONRPC_INVALID_REQUEST
    assert runner_calls == 0

    responses = _run_stdio_session(
        server,
        [
            *_handshake_messages(),
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
    # responses[0] is initialize
    assert responses[1]["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert "quantity" in responses[1]["error"]["message"]
    assert responses[2]["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert "integer" in responses[2]["error"]["message"]
    assert responses[3]["error"]["code"] == JSONRPC_INVALID_PARAMS
    assert "Unknown tool" in responses[3]["error"]["message"]
    assert responses[4]["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
    assert responses[5]["error"]["code"] == JSONRPC_PARSE_ERROR


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
    server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
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
    # Operator-pinned Ed25519 key loaded via --signing-key.
    op_key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "operator.pem"
    key_path.write_bytes(
        op_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    trusted_pub = op_key.public_key()

    # Executable json_write step: target file does NOT exist beforehand.
    spec_data = _sample_spec_dict(tmp_path / "unused.sqlite")
    spec_data["steps"] = [
        {
            "op": "json_write",
            "path": str(json_target),
            "data": {"entry_code": "{entry_code}", "quantity": "{quantity}"},
        }
    ]
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

    # Test `config` subcommand with --signing-key.
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
            "--signing-key",
            str(key_path),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert cfg_run.returncode == 0, cfg_run.stderr
    parsed_cfg = json.loads(cfg_run.stdout)
    assert "record_ledger_entry" in parsed_cfg["mcpServers"]
    assert "--signing-key" in parsed_cfg["mcpServers"]["record_ledger_entry"]["args"]

    # Test `serve` subcommand over real stdio pipes with pinned --signing-key.
    stdin_payload = (
        "\n".join(
            [
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
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
            "--signing-key",
            str(key_path),
        ],
        input=stdin_payload,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert serve_run.returncode == 0, serve_run.stderr
    assert json_target.is_file()
    lines = [json.loads(line) for line in serve_run.stdout.strip().splitlines()]
    assert [line["id"] for line in lines] == [1, 2, 3]
    call_res = lines[2]["result"]
    assert call_res["isError"] is False
    assert call_res["structuredContent"]["status"] == "VERIFIED"
    assert (
        verify_mcpify_receipt(
            call_res["structuredContent"]["receipt"], trusted_public_key=trusted_pub
        )
        is True
    )

    # Non-executing GUI-only spec against a pre-existing matching file MUST HALT (ABSTAIN), never VERIFIED.
    noop_spec = _sample_spec_dict(tmp_path / "unused.sqlite")
    noop_spec["readback_contract"] = spec_data["readback_contract"]
    noop_server = McpifyServer(
        WorkflowToolSpec.from_dict(noop_spec), signing_key=op_key
    )
    noop_resps = _run_stdio_session(
        noop_server,
        [
            *_handshake_messages(),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "record_ledger_entry",
                        "arguments": {"entry_code": "CLI-1", "quantity": 3},
                    },
                }
            ),
        ],
    )
    noop_call = noop_resps[1]["result"]
    assert noop_call["isError"] is True
    assert noop_call["structuredContent"]["status"] == "HALT"
    assert noop_call["structuredContent"]["verdict"] == "ABSTAIN"
    assert noop_call["structuredContent"]["receipt"] is None

    # Grok P1 probe 1: unexecuted UI op + json_write audit file against pre-existing SQLite row MUST HALT.
    db_path = tmp_path / "claims.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE claims (id TEXT, status TEXT)")
        conn.execute("CREATE TABLE audit_log (msg TEXT)")
        conn.execute("INSERT INTO claims (id, status) VALUES ('C1', 'open')")
    partial_spec = _sample_spec_dict(db_path)
    partial_spec["input_schema"] = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    }
    partial_spec["steps"] = [
        {"op": "click", "target": "Submit {id}"},
        {
            "op": "json_write",
            "path": str(tmp_path / "audit.json"),
            "data": {"id": "{id}"},
        },
    ]
    partial_spec["readback_contract"] = {
        "kind": "sqlite",
        "database": str(db_path),
        "table": "claims",
        "match": {"id": "{id}", "status": "open"},
        "expected_count": 1,
    }
    partial_srv = McpifyServer(
        WorkflowToolSpec.from_dict(partial_spec), signing_key=op_key
    )
    partial_res = _run_stdio_session(
        partial_srv,
        [
            *_handshake_messages(),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "record_ledger_entry",
                        "arguments": {"id": "C1"},
                    },
                }
            ),
        ],
    )[1]["result"]
    assert partial_res["isError"] is True
    assert partial_res["structuredContent"]["verdict"] == "ABSTAIN"

    # Grok P1 probe 2: sqlite_insert into a different table ('audit_log') MUST NOT verify 'claims'.
    wrong_tbl_spec = dict(partial_spec)
    wrong_tbl_spec["steps"] = [
        {
            "op": "sqlite_insert",
            "database": str(db_path),
            "table": "audit_log",
            "values": {"msg": "ran {id}"},
        }
    ]
    wrong_tbl_srv = McpifyServer(
        WorkflowToolSpec.from_dict(wrong_tbl_spec), signing_key=op_key
    )
    wrong_tbl_res = _run_stdio_session(
        wrong_tbl_srv,
        [
            *_handshake_messages(),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {
                        "name": "record_ledger_entry",
                        "arguments": {"id": "C1"},
                    },
                }
            ),
        ],
    )[1]["result"]
    assert wrong_tbl_res["isError"] is True
    assert wrong_tbl_res["structuredContent"]["verdict"] == "ABSTAIN"

    # Grok P1 probe 3: custom runner omitting writes_applied MUST fail closed (ABSTAIN).
    omit_srv = McpifyServer(
        WorkflowToolSpec.from_dict(wrong_tbl_spec),
        runner=lambda _s, _a: {"llm_calls": 0},
        signing_key=op_key,
    )
    omit_res = _run_stdio_session(
        omit_srv,
        [
            *_handshake_messages(),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "tools/call",
                    "params": {
                        "name": "record_ledger_entry",
                        "arguments": {"id": "C1"},
                    },
                }
            ),
        ],
    )[1]["result"]
    assert omit_res["isError"] is True
    assert omit_res["structuredContent"]["verdict"] == "ABSTAIN"

    # P3: `pubkey --signing-key` prints the exact operator public key.
    pub_run = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_verify.mcpify",
            "pubkey",
            "--signing-key",
            str(key_path),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert pub_run.returncode == 0, pub_run.stderr
    assert pub_run.stdout.strip() == noop_server.public_key_b64


def test_stale_row_in_same_table_does_not_verify_mismatched_insert(
    tmp_path: Path,
) -> None:
    """Probe E: an insert into the read-back table with wrong status must not
    verify against a pre-existing stale row in the same table."""
    db_path = tmp_path / "claims.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE claims (id TEXT, status TEXT)")
        conn.execute("INSERT INTO claims (id, status) VALUES ('C1', 'open')")

    pack_path = tmp_path / "probe_e.json"
    pack_path.write_text(
        json.dumps(
            {
                "name": "open_claim",
                "description": "Open a claim record.",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
                "steps": [
                    {
                        "op": "sqlite_insert",
                        "database": str(db_path),
                        "table": "claims",
                        "values": {"id": "{id}", "status": "closed"},
                    }
                ],
                "readback_contract": {
                    "kind": "sqlite",
                    "database": str(db_path),
                    "table": "claims",
                    "match": {"id": "{id}", "status": "open"},
                    "expected_count": 1,
                },
            }
        ),
        encoding="utf-8",
    )

    server = McpifyServer(load_workflow_spec(pack_path))
    res = _run_stdio_session(
        server,
        [
            *_handshake_messages(),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "tools/call",
                    "params": {"name": "open_claim", "arguments": {"id": "C1"}},
                }
            ),
        ],
    )[1]["result"]
    assert res["isError"] is True
    assert res["structuredContent"]["status"] == "HALT"
    assert res["structuredContent"]["verdict"] == "MISMATCH"
    assert res["structuredContent"]["receipt"] is None


def test_custom_runner_without_applied_targets_fails_closed(tmp_path: Path) -> None:
    """Probe F: a custom runner returning writes_applied=1 without applied_targets
    must fail closed (ABSTAIN), never matching pre-existing rows."""
    db_path = tmp_path / "claims.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE claims (id TEXT, status TEXT)")
        conn.execute("INSERT INTO claims (id, status) VALUES ('C1', 'open')")

    pack_path = tmp_path / "probe_f.json"
    pack_path.write_text(
        json.dumps(
            {
                "name": "open_claim",
                "description": "Open a claim record.",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
                "steps": [{"op": "click", "target": "#save"}],
                "readback_contract": {
                    "kind": "sqlite",
                    "database": str(db_path),
                    "table": "claims",
                    "match": {"id": "{id}", "status": "open"},
                    "expected_count": 1,
                },
            }
        ),
        encoding="utf-8",
    )

    server = McpifyServer(
        load_workflow_spec(pack_path),
        runner=lambda _s, _a: {"writes_applied": 1, "llm_calls": 0},
    )
    res = _run_stdio_session(
        server,
        [
            *_handshake_messages(),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 21,
                    "method": "tools/call",
                    "params": {"name": "open_claim", "arguments": {"id": "C1"}},
                }
            ),
        ],
    )[1]["result"]
    assert res["isError"] is True
    assert res["structuredContent"]["status"] == "HALT"
    assert res["structuredContent"]["verdict"] in ("MISMATCH", "ABSTAIN")
    assert res["structuredContent"]["receipt"] is None


def test_probe_h_spoofed_preexisting_rowid_halts_instead_of_verifying(
    tmp_path: Path,
) -> None:
    """Probe H: a no-op runner spoofing a pre-existing rowid must never verify."""
    db_path = tmp_path / "store.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE ledger_entries (entry_code TEXT NOT NULL, quantity INTEGER NOT NULL)"
        )
        cur = conn.execute(
            "INSERT INTO ledger_entries (entry_code, quantity) VALUES (?, ?)",
            ("ORD-EXISTING", 5),
        )
        preexisting_rowid = cur.lastrowid

    spec = WorkflowToolSpec.from_dict(_sample_spec_dict(db_path))

    def spoofed_noop_runner(
        _spec: WorkflowToolSpec, _arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "writes_applied": 1,
            "applied_targets": [
                {
                    "kind": "sqlite",
                    "database": str(db_path.resolve()),
                    "table": "ledger_entries",
                    "rowid": preexisting_rowid,
                }
            ],
            "llm_calls": 0,
        }

    server = McpifyServer(spec, runner=spoofed_noop_runner)
    server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    resp = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "record_ledger_entry",
                "arguments": {"entry_code": "ORD-EXISTING", "quantity": 5},
            },
        }
    )
    assert resp is not None
    assert resp["result"]["isError"] is True
    assert resp["result"]["structuredContent"]["status"] == "HALT"
    assert resp["result"]["structuredContent"]["verdict"] == "MISMATCH"
    assert resp["result"]["structuredContent"]["receipt"] is None
