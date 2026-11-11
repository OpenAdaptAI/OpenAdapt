"""Operator CLI; the write command is an argv, never a shell expression."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

from openadapt_verify._ledger_config import (
    ProtectedLedgerUnsupported,
    require_protected_default_support,
)


def _outcome_exit(outcome: str, provisional: bool = False) -> int:
    if outcome == "CONFIRMED":
        return 5 if provisional else 0
    return {"UNCERTAIN": 3, "REFUTED": 4}.get(outcome, 2)


def _params(path: str) -> dict:
    values = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise ValueError("params must be a JSON object")
    return values


def _ledger_hold_code(exc: RuntimeError) -> Optional[str]:
    from openadapt_flow import write_ledger

    held = getattr(write_ledger, "LedgerIntegrityHeld", None)
    if isinstance(held, type) and isinstance(exc, held):
        code = getattr(exc, "code", None)
        return code if isinstance(code, str) else None
    return None


def _report_ledger_hold(code: str, *, before_write: bool = False) -> int:
    reply: dict[str, str | bool] = {
        "verified": False,
        "outcome": "UNCERTAIN",
        "hold_reason": code,
    }
    if before_write:
        reply["written"] = False
    print(json.dumps(reply, sort_keys=True))
    return 3


def _run(args: argparse.Namespace) -> int:
    require_protected_default_support()

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
    values = _params(args.params)
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
    except RuntimeError as exc:
        code = _ledger_hold_code(exc)
        if code is None:
            raise
        return _report_ledger_hold(code, before_write=True)

    returncode = None
    error = None
    delivered = True
    try:
        # Command diagnostics never become signed evidence of success.
        returncode = subprocess.run(
            command, check=False, stdout=sys.stderr, stderr=sys.stderr
        ).returncode
    except Exception as exc:
        error = type(exc).__name__
    except BaseException:
        delivered = False
        raise
    finally:
        try:
            result = verify_write(
                pending,
                write_attempted=True,
                delivered=delivered,
                write_receipt=True,
            )
        except RuntimeError as exc:
            if delivered or _ledger_hold_code(exc) is None:
                raise
        else:
            reply = result.to_dict()
            reply.update(
                command_returncode=returncode,
                command_error=error,
                recheck_state_path=str(result.recheck.path) if result.recheck else None,
            )
            print(json.dumps(reply, sort_keys=True))
    outcome = _outcome_exit(result.outcome.value, result.provisional)
    if outcome:
        return outcome
    return 0 if result.write_receipt == "issued" and returncode == 0 else 1


def _verify(args: argparse.Namespace) -> int:
    from openadapt_flow.write_receipt import load_public_key

    from openadapt_verify import ReceiptError, verify_receipt

    try:
        result = verify_receipt(
            Path(args.file).read_bytes(),
            issuer_public=load_public_key(Path(args.issuer_key)),
            log_public=load_public_key(Path(args.log_key)),
            evidence=Path(args.evidence).read_bytes() if args.evidence else None,
        )
    except ReceiptError as exc:
        print(json.dumps({"valid": False, "code": exc.code, "detail": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "valid": True,
                "receipt_id": result.sha256,
                "log_index": result.log_index,
                "evidence_opened": result.evidence_opened,
                "anchor": result.anchor,
                "notes": result.notes,
                "payload": result.payload.wire(),
            },
            sort_keys=True,
        )
    )
    return _outcome_exit(result.payload.verdict, result.payload.provisional)


def _recheck(args: argparse.Namespace) -> int:
    require_protected_default_support()

    from openadapt_flow.verifier import RecheckNotDue, RecheckState
    from openadapt_flow.write_ledger import default_ledger

    from openadapt_verify import Actor, Contract, recheck

    values = _params(args.params)
    state = RecheckState.load(args.recheck_state, params=values)
    ledger = default_ledger()
    try:
        result = recheck(
            Contract.load(args.contract),
            state,
            params=values,
            actor=Actor("agent", args.agent_id),
            ledger=ledger,
        )
    except RecheckNotDue as exc:
        print(
            json.dumps(
                {"due": False, "recheck_due_at": exc.due_at, "wait_s": exc.wait_s}
            )
        )
        return 5
    write_id = str((state.payload.get("receipt") or {}).get("correlation_id") or "")
    found = ledger.find_attempt(write_id) if write_id else None
    attempt = found[1] if found is not None else None
    resolution = attempt.resolved or {} if attempt is not None else {}
    ledger_state = ledger.state(attempt) if attempt is not None else "missing"
    ledger_outcome = resolution.get("outcome")
    recorded = (
        attempt is not None
        and ledger_outcome == "CONFIRMED"
        and resolution.get("receipt_sha256") == result.receipt_sha256
        and attempt.rechecked is not None
        and attempt.rechecked.get("receipt_sha256") == result.receipt_sha256
        and not attempt.recheck_owed
        and not attempt.review
        and not attempt.released
    )
    reasons = list(result.reasons)
    code = _outcome_exit(result.outcome)
    # Older supported Flow releases have no canonical hold details.
    hold_reason = getattr(result, "hold_reason", None)
    if ledger_state == "held" or ledger_outcome == "REFUTED":
        code = 4
        reasons.append("the exact ledger attempt remains held for person review")
    elif result.verified and not recorded:
        code = 3
        reasons.append("the exact ledger attempt has no recorded final confirmation")
    print(
        json.dumps(
            {
                "due": True,
                "verified": result.verified and recorded,
                **(
                    {
                        "hold_reason": hold_reason,
                        "current_observation": result.current_observation,
                    }
                    if hold_reason is not None
                    else {}
                ),
                "ledger_state": ledger_state,
                "ledger_outcome": ledger_outcome,
                "outcome": result.outcome,
                "frame": result.frame,
                "reasons": reasons,
                "receipt_sha256": result.receipt_sha256,
                "post_save_read_at": result.post_save_read_at,
                "recheck_due_at": result.due_at,
                "read_at": result.read_at,
            },
            sort_keys=True,
        )
    )
    return code


def _quickstart(args: argparse.Namespace) -> int:
    from openadapt_verify import quickstart

    def connect_args(text: Optional[str]) -> Optional[dict]:
        if text is None:
            return None
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("database connect arguments must be a JSON object")
        return value

    rows = quickstart.run(
        args.path or ("rest", "sqlite", "fhir"),
        postgres=connect_args(args.postgres),
        mysql=connect_args(args.mysql),
    )
    if args.json:
        print(json.dumps([row.to_dict() for row in rows], indent=2, sort_keys=True))
    else:
        print(quickstart.render(rows))
    return 0 if rows and all(row.ok for row in rows) else 1


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
        "verify", help="Check pinned signatures and require a final CONFIRMED verdict"
    )
    verify.add_argument("file")
    verify.add_argument("--issuer-key", required=True)
    verify.add_argument("--log-key", required=True)
    verify.add_argument("--evidence")
    check = sub.add_parser("recheck", help="Re-read a provisional write's store")
    check.add_argument("--contract", required=True)
    check.add_argument("--params", required=True)
    check.add_argument(
        "--recheck-state", required=True, help="private recheck.json from run"
    )
    check.add_argument("--agent-id", default="external-agent")
    serve = sub.add_parser("serve", help="Serve the canonical MCP verification tools")
    serve.add_argument("--contracts", required=True)
    serve.add_argument("--state", required=True)
    quick = sub.add_parser(
        "quickstart",
        help="Check scripted agent writes against disposable local stores",
    )
    quick.add_argument(
        "--path",
        action="append",
        choices=("rest", "sqlite", "fhir"),
        help="read path to run (repeatable; default: rest, sqlite and fhir)",
    )
    quick.add_argument(
        "--postgres",
        metavar="JSON",
        help="psycopg connect arguments for a disposable PostgreSQL database",
    )
    quick.add_argument(
        "--mysql",
        metavar="JSON",
        help="PyMySQL connect arguments for a disposable MySQL database",
    )
    quick.add_argument("--json", action="store_true", help="print JSON rows")
    args = parser.parse_args(argv)
    try:
        if args.command_name == "quickstart":
            return _quickstart(args)
        if args.command_name == "run":
            return _run(args)
        if args.command_name == "verify":
            return _verify(args)
        if args.command_name == "recheck":
            return _recheck(args)
        try:
            from openadapt_verify.mcp import serve as serve_mcp

            serve_mcp(Path(args.contracts), Path(args.state))
        except ModuleNotFoundError as exc:
            if exc.name != "mcp" and not (exc.name or "").startswith("mcp."):
                raise
            print(
                "openadapt-verify: MCP is missing; pip install 'openadapt-verify[mcp]'",
                file=sys.stderr,
            )
            return 2
        return 0
    except ProtectedLedgerUnsupported as exc:
        if args.command_name == "serve":
            print(f"openadapt-verify: {exc}", file=sys.stderr)
            return 3
        return _report_ledger_hold(str(exc), before_write=args.command_name == "run")
    except (OSError, ValueError) as exc:
        print(f"openadapt-verify: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        code = _ledger_hold_code(exc)
        if code is None:
            raise
        return _report_ledger_hold(code)
