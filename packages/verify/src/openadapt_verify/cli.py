"""Operator CLI; the write command is an argv, never a shell expression."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence


def _run(args: argparse.Namespace) -> int:
    from openadapt_verify import (
        Actor,
        AlreadyWritten,
        Contract,
        WriteBlocked,
        begin_write,
        verify_write,
    )

    command = list(args.command)
    if command[:1] == ["--"]:
        command.pop(0)
    if not command:
        raise ValueError("run needs a write command after --")
    values = json.loads(Path(args.params).read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError("params must be a JSON object")
    try:
        pending = begin_write(
            Contract.load(args.contract),
            values,
            actor=Actor("agent", args.agent_id),
            workdir=args.state,
        )
    except AlreadyWritten as exc:
        print(json.dumps({**exc.result, "not_sent_again": True}, sort_keys=True))
        return 1
    except WriteBlocked as exc:
        print(json.dumps({"verified": False, "written": False, "reason": str(exc)}))
        return 1

    returncode = None
    error = None
    delivered = True
    try:
        # Keep stdout a single verifier document. The operator's command can
        # report diagnostics on stderr; none become signed evidence of success.
        returncode = subprocess.run(
            command, check=False, stdout=sys.stderr, stderr=sys.stderr
        ).returncode
    except Exception as exc:
        error = type(exc).__name__
    except BaseException:
        delivered = False
        raise
    finally:
        result = verify_write(
            pending,
            write_attempted=True,
            delivered=delivered,
            write_receipt=True,
        )
        reply = result.to_dict()
        reply.update(command_returncode=returncode, command_error=error)
        print(json.dumps(reply, sort_keys=True))
    return (
        0
        if result.verified and result.write_receipt == "issued" and returncode == 0
        else 1
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="openadapt-verify")
    sub = parser.add_subparsers(dest="command_name", required=True)
    run = sub.add_parser(
        "run", help="Read, run an explicit write command, read back and sign"
    )
    run.add_argument("--contract", required=True)
    run.add_argument(
        "--params", required=True, help="operator-supplied JSON parameter file"
    )
    run.add_argument("--state", required=True, help="private verifier state directory")
    run.add_argument("--agent-id", default="external-agent")
    run.add_argument("command", nargs=argparse.REMAINDER)
    verify = sub.add_parser(
        "verify", help="Verify a portable COSE receipt with pinned public keys"
    )
    verify.add_argument("file")
    verify.add_argument("--issuer-key", required=True)
    verify.add_argument("--log-key", required=True)
    verify.add_argument("--evidence")
    serve = sub.add_parser("serve", help="Serve the canonical MCP verification tools")
    serve.add_argument("--contracts", required=True)
    serve.add_argument("--state", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command_name == "run":
            return _run(args)
        if args.command_name == "verify":
            from openadapt_flow.cli_receipt import cmd_verify

            args.no_log = False
            args.require_anchor = False
            args.json = True
            return cmd_verify(args)
        from openadapt_verify.mcp import serve as serve_mcp

        serve_mcp(Path(args.contracts), Path(args.state))
        return 0
    except (OSError, ValueError) as exc:
        print(f"openadapt-verify: {exc}", file=sys.stderr)
        return 2
