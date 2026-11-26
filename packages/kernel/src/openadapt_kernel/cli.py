"""Minimal CLI for openadapt-kernel (induce, qualify, keygen, verify-receipt)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from openadapt_kernel.copilot import (
    generate_ephemeral_ed25519_keypair,
    qualify_verifier_offline,
    verify_receipt,
)
from openadapt_kernel.extension_bridge import (
    induce_pack_from_extension_recordings,
    parse_extension_evidence_jsonl,
)
from openadapt_kernel.mcp_server import serve_stdio
from openadapt_kernel.runner import format_run_result_json, run_portal_pack


def _token_boundary_verifier(fields: Dict[str, str], text: str) -> bool:
    compact = " ".join(text.split())
    return all(
        bool(re.search(rf"(?<!\w){re.escape(v)}(?!\w)", compact))
        for v in fields.values()
        if str(v).strip()
    )


def _cmd_induce(args: argparse.Namespace) -> int:
    sessions = []
    for spec in args.session:
        if "=" not in spec:
            raise ValueError(f"--session must be <evidence.jsonl>=<dossier.json>, got {spec!r}")
        ev_raw, dos_raw = spec.split("=", 1)
        ev_path = Path(ev_raw)
        dossier = json.loads(Path(dos_raw).read_text(encoding="utf-8"))
        sessions.append((ev_path, dossier, None))

    pack = induce_pack_from_extension_recordings(args.pack_id, sessions)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(asdict(pack), indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "pack_id": pack.pack_id,
                "portal_origin": pack.portal_origin,
                "variable_fields": pack.variable_fields,
                "static_defaults": pack.static_defaults,
                "provenance_map": pack.provenance_map,
                "out": str(out_path),
            }
        )
    )
    return 0


def _cmd_qualify(args: argparse.Namespace) -> int:
    summary = parse_extension_evidence_jsonl(Path(args.evidence))
    pre_text = args.pre_text if args.pre_text is not None else summary.pre_write_text
    if not pre_text or not pre_text.strip():
        raise ValueError(
            "qualify requires a captured pre-write DOM snapshot (--pre-text or pre_write event in evidence)"
        )
    pack_data = json.loads(Path(args.pack).read_text(encoding="utf-8"))
    var_fields: List[str] = list(pack_data.get("variable_fields") or summary.submitted_fields.keys())
    expected = {k: summary.submitted_fields[k] for k in var_fields if k in summary.submitted_fields}
    report = qualify_verifier_offline(
        expected_fields=expected,
        pre_write_text=pre_text,
        post_write_text=summary.readback_text,
        verifier_fn=_token_boundary_verifier,
    )
    print(json.dumps(asdict(report), indent=2))
    return 0 if report.qualified else 1


def _cmd_keygen(args: argparse.Namespace) -> int:
    priv_pem, pub_pem = generate_ephemeral_ed25519_keypair()
    priv_path = Path(args.priv)
    pub_path = Path(args.pub)
    priv_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.parent.mkdir(parents=True, exist_ok=True)
    priv_path.write_bytes(priv_pem)
    priv_path.chmod(0o600)
    pub_path.write_bytes(pub_pem)
    print(json.dumps({"priv": str(priv_path), "pub": str(pub_path)}))
    return 0


def _cmd_verify_receipt(args: argparse.Namespace) -> int:
    receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    pub_pem = Path(args.pub).read_bytes()
    valid = verify_receipt(receipt, pub_pem)
    payload = receipt.get("payload", {})
    print(
        json.dumps(
            {
                "valid": valid,
                "outcome": payload.get("outcome"),
                "episode_id": payload.get("episode_id"),
                "workflow_name": payload.get("workflow_name"),
            }
        )
    )
    return 0 if valid else 1


def _cmd_run(args: argparse.Namespace) -> int:
    dossier = json.loads(Path(args.dossier).read_text(encoding="utf-8"))
    result = run_portal_pack(
        pack_path=Path(args.pack),
        dossier=dossier,
        mode=args.mode,
        url=args.url,
        cdp_url=args.cdp_url,
        commit_selector=args.commit_selector,
        private_key_path=Path(args.priv) if args.priv else None,
        receipt_out=Path(args.receipt_out) if args.receipt_out else None,
        episode_id=args.episode_id,
        headless=not args.headed,
    )
    print(format_run_result_json(result))
    return 0 if result.outcome in ("READY_FOR_HUMAN_COMMIT", "VERIFIED") else 1


def _cmd_shadow_sync(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    sessions = []
    for ev_path in sorted(root.rglob("extension_evidence.jsonl")):
        dossier_path = ev_path.parent / "dossier.json"
        if not dossier_path.exists():
            continue
        dossier = json.loads(dossier_path.read_text(encoding="utf-8"))
        sessions.append((ev_path, dossier, None))
    if not sessions:
        raise ValueError(f"No extension_evidence.jsonl + dossier.json pairs found under {root}")
    pack = induce_pack_from_extension_recordings(args.pack_id, sessions)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(asdict(pack), indent=2), encoding="utf-8")
    print(json.dumps({"pack_id": pack.pack_id, "sessions_ingested": len(sessions), "out": str(out_path)}))
    return 0


def _cmd_serve_mcp(_args: argparse.Namespace) -> int:
    serve_stdio()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openadapt-kernel")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ind = sub.add_parser("induce", help="Induce a zero-PII PortalPack from extension JSONL logs")
    p_ind.add_argument("--pack-id", required=True)
    p_ind.add_argument("--session", action="append", required=True, help="<evidence.jsonl>=<dossier.json>")
    p_ind.add_argument("--out", required=True)
    p_ind.set_defaults(func=_cmd_induce)

    p_qual = sub.add_parser("qualify", help="Run 4-mutant offline qualification on a recorded trace")
    p_qual.add_argument("--pack", required=True)
    p_qual.add_argument("--evidence", required=True)
    p_qual.add_argument("--pre-text", default=None)
    p_qual.set_defaults(func=_cmd_qualify)

    p_key = sub.add_parser("keygen", help="Generate an Ed25519 keypair for receipt signing")
    p_key.add_argument("--priv", required=True)
    p_key.add_argument("--pub", required=True)
    p_key.set_defaults(func=_cmd_keygen)

    p_ver = sub.add_parser("verify-receipt", help="Verify an Ed25519-signed kernel receipt offline")
    p_ver.add_argument("--receipt", required=True)
    p_ver.add_argument("--pub", required=True)
    p_ver.set_defaults(func=_cmd_verify_receipt)

    p_run = sub.add_parser("run", help="Run a PortalPack in draft or verified autopilot mode")
    p_run.add_argument("--pack", required=True)
    p_run.add_argument("--dossier", required=True)
    p_run.add_argument("--mode", choices=["draft", "autopilot"], default="draft")
    p_run.add_argument("--url")
    p_run.add_argument("--cdp-url")
    p_run.add_argument("--commit-selector")
    p_run.add_argument("--priv")
    p_run.add_argument("--receipt-out")
    p_run.add_argument("--episode-id", default=None)
    p_run.add_argument("--headed", action="store_true")
    p_run.set_defaults(func=_cmd_run)

    p_sync = sub.add_parser("shadow-sync", help="Scan a directory of Chrome Co-Pilot recordings and induce a PortalPack")
    p_sync.add_argument("--dir", required=True)
    p_sync.add_argument("--pack-id", required=True)
    p_sync.add_argument("--out", required=True)
    p_sync.set_defaults(func=_cmd_shadow_sync)

    p_mcp = sub.add_parser("serve-mcp", help="Run the zero-dependency stdio JSON-RPC 2.0 MCP server")
    p_mcp.set_defaults(func=_cmd_serve_mcp)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
