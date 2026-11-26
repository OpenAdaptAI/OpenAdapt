"""Zero-dependency stdio JSON-RPC 2.0 MCP server for openadapt-kernel.

Exposes the kernel's core tools to any MCP client (Claude Desktop, Claude Code,
Cursor, or custom agent loops) without importing openadapt-flow:
  - kernel_induce_pack
  - kernel_qualify_offline
  - kernel_run_pack (draft or verified autopilot)
  - kernel_verify_receipt
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO

from openadapt_kernel.copilot import qualify_verifier_offline, verify_receipt
from openadapt_kernel.extension_bridge import (
    induce_pack_from_extension_recordings,
    parse_extension_evidence_jsonl,
)
from openadapt_kernel.runner import run_portal_pack

SERVER_INFO = {"name": "openadapt-kernel", "version": "0.1.0"}

TOOLS_SPEC: List[Dict[str, Any]] = [
    {
        "name": "kernel_induce_pack",
        "description": "Induce a zero-PII PortalPack from passive Chrome Co-Pilot extension_evidence.jsonl recordings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pack_id": {"type": "string"},
                "sessions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "evidence_path": {"type": "string"},
                            "dossier": {"type": "object"},
                        },
                        "required": ["evidence_path", "dossier"],
                    },
                },
                "out_path": {"type": "string"},
            },
            "required": ["pack_id", "sessions", "out_path"],
        },
    },
    {
        "name": "kernel_qualify_offline",
        "description": "Run the 4-mutant offline Plant-and-Catch qualification suite on a recorded trace (zero staging DB).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pack_path": {"type": "string"},
                "evidence_path": {"type": "string"},
                "pre_text": {"type": "string"},
            },
            "required": ["pack_path", "evidence_path"],
        },
    },
    {
        "name": "kernel_run_pack",
        "description": "Execute a PortalPack in draft (stop before submit) or verified autopilot mode with fresh-reload/cross-tab read-back.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pack_path": {"type": "string"},
                "dossier": {"type": "object"},
                "mode": {"type": "string", "enum": ["draft", "autopilot"]},
                "url": {"type": "string"},
                "cdp_url": {"type": "string"},
                "commit_selector": {"type": "string"},
                "private_key_path": {"type": "string"},
                "receipt_out": {"type": "string"},
                "episode_id": {"type": "string"},
            },
            "required": ["pack_path", "dossier"],
        },
    },
    {
        "name": "kernel_verify_receipt",
        "description": "Verify an Ed25519-signed openadapt.kernel.receipt/v1 artifact offline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "receipt_path": {"type": "string"},
                "public_key_path": {"type": "string"},
            },
            "required": ["receipt_path", "public_key_path"],
        },
    },
]


def _token_boundary_verifier(fields: Dict[str, str], text: str) -> bool:
    compact = " ".join(text.split())
    return all(
        bool(re.search(rf"(?<!\w){re.escape(v)}(?!\w)", compact))
        for v in fields.values()
        if str(v).strip()
    )


def dispatch_tool_call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name == "kernel_induce_pack":
        sessions = [
            (Path(s["evidence_path"]), dict(s["dossier"]), None)
            for s in arguments["sessions"]
        ]
        pack = induce_pack_from_extension_recordings(arguments["pack_id"], sessions)
        out = Path(arguments["out_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(asdict(pack), indent=2), encoding="utf-8")
        return {"ok": True, "pack": asdict(pack), "out_path": str(out)}

    if name == "kernel_qualify_offline":
        summary = parse_extension_evidence_jsonl(Path(arguments["evidence_path"]))
        pre_text = arguments.get("pre_text") if arguments.get("pre_text") is not None else summary.pre_write_text
        if not str(pre_text or "").strip():
            raise ValueError(
                "kernel_qualify_offline requires a captured pre-write DOM snapshot via pre_text or pre_write event"
            )
        pack_data = json.loads(Path(arguments["pack_path"]).read_text(encoding="utf-8"))
        var_fields = list(pack_data.get("variable_fields") or summary.submitted_fields.keys())
        expected = {k: summary.submitted_fields[k] for k in var_fields if k in summary.submitted_fields}
        report = qualify_verifier_offline(
            expected_fields=expected,
            pre_write_text=str(pre_text),
            post_write_text=summary.readback_text,
            verifier_fn=_token_boundary_verifier,
        )
        return {"ok": report.qualified, "report": asdict(report)}

    if name == "kernel_run_pack":
        res = run_portal_pack(
            pack_path=Path(arguments["pack_path"]),
            dossier={str(k): str(v) for k, v in arguments["dossier"].items()},
            mode=arguments.get("mode", "draft"),
            url=arguments.get("url"),
            cdp_url=arguments.get("cdp_url"),
            commit_selector=arguments.get("commit_selector"),
            private_key_path=Path(arguments["private_key_path"]) if arguments.get("private_key_path") else None,
            receipt_out=Path(arguments["receipt_out"]) if arguments.get("receipt_out") else None,
            episode_id=arguments.get("episode_id") or None,
            headless=True,
        )
        return {"ok": res.outcome in ("READY_FOR_HUMAN_COMMIT", "VERIFIED"), "result": asdict(res)}

    if name == "kernel_verify_receipt":
        receipt = json.loads(Path(arguments["receipt_path"]).read_text(encoding="utf-8"))
        pub_pem = Path(arguments["public_key_path"]).read_bytes()
        valid = verify_receipt(receipt, pub_pem)
        return {"ok": valid, "valid": valid, "outcome": receipt.get("payload", {}).get("outcome")}

    raise ValueError(f"Unknown tool: {name}")


def handle_jsonrpc_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = msg.get("id")
    method = str(msg.get("method") or "")
    params = msg.get("params") or {}

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": SERVER_INFO,
                "capabilities": {"tools": {}},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SPEC}}
    if method == "tools/call":
        try:
            tool_name = str(params.get("name") or "")
            tool_args = dict(params.get("arguments") or {})
            payload = dispatch_tool_call(tool_name, tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "isError": not bool(payload.get("ok", False)),
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"ok": False, "error": str(exc)})}],
                    "isError": True,
                },
            }
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve_stdio(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    """Line-delimited JSON-RPC 2.0 stdio loop."""
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            reply = handle_jsonrpc_message(msg)
        except Exception as exc:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
