#!/usr/bin/env python3
"""Offline cryptographic verifier for the OpenAdapt Seal GitHub Action.

Performs zero network egress and fails closed (exit code 1) on any missing
receipt, missing/unknown signing key, malformed JSON, or invalid/tampered
Ed25519 signature. Writes outputs to ``$GITHUB_OUTPUT`` and a Markdown summary
table to ``$GITHUB_STEP_SUMMARY`` when those environment variables are set.
"""

from __future__ import annotations

import argparse
import base64
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from openadapt_types.receipt import (
    REGISTRY_SCHEMA,
    ROLES,
    RoleKey,
    key_id,
    loads_strict,
    parse_registry,
    verify_receipt,
)

_ALLOWED_ROLES = frozenset(ROLES)


def _sanitize_output(value: str) -> str:
    """Ensure a single-line value safe for GITHUB_OUTPUT key=value syntax."""
    return " ".join(str(value).splitlines()).strip()


def _escape_md_cell(value: str) -> str:
    """Escape characters that could break a GitHub-Flavored Markdown table cell."""
    return _sanitize_output(value).replace("|", "\\|")


def _write_github_outputs(
    *,
    verified: bool,
    receipt_id: str,
    outcome: str,
    reason: str,
) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not out_path:
        return
    lines = [
        f"verified={'true' if verified else 'false'}",
        f"receipt-id={_sanitize_output(receipt_id)}",
        f"outcome={_sanitize_output(outcome)}",
        f"reason={_sanitize_output(reason)}",
    ]
    with Path(out_path).open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _write_step_summary(markdown: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    with Path(summary_path).open("a", encoding="utf-8") as fh:
        fh.write(markdown.rstrip() + "\n")


def _load_public_key_or_registry(path: Path, role: str) -> list[RoleKey]:
    if not path.is_file():
        raise ValueError(f"public key file not found: {path}")
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace").strip()
    if text.startswith("{"):
        parsed = loads_strict(text)
        if not isinstance(parsed, dict) or parsed.get("schema") != REGISTRY_SCHEMA:
            raise ValueError(f"JSON key file is not an {REGISTRY_SCHEMA} registry")
        return parse_registry(parsed)
    if role not in _ALLOWED_ROLES:
        raise ValueError(
            f"unsupported key-role {role!r}; expected one of {sorted(_ALLOWED_ROLES)}"
        )
    loaded = serialization.load_pem_public_key(raw)
    if not isinstance(loaded, Ed25519PublicKey):
        raise ValueError("public-key PEM must be an Ed25519 public key")
    raw_pub = loaded.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return [
        RoleKey(
            key_id=key_id(raw_pub),
            public_key=base64.b64encode(raw_pub).decode("ascii"),
            role=role,
            status="active",
        )
    ]


def _extract_receipt_id(statement: Optional[dict[str, Any]]) -> str:
    if not isinstance(statement, dict):
        return ""
    predicate = statement.get("predicate")
    if isinstance(predicate, dict):
        for field in ("run_id", "receipt_id"):
            val = predicate.get(field)
            if isinstance(val, str) and val.strip():
                return val.strip()
    subjects = statement.get("subject")
    if isinstance(subjects, list) and subjects:
        first = subjects[0]
        if isinstance(first, dict):
            digest = first.get("digest")
            if isinstance(digest, dict) and isinstance(digest.get("sha256"), str):
                return f"sha256:{digest['sha256'][:16]}"
    return ""


def _fail(
    *,
    receipt_arg: str,
    reason: str,
    detail: str,
) -> int:
    _write_github_outputs(
        verified=False,
        receipt_id="",
        outcome="",
        reason=reason,
    )
    summary = (
        "### ❌ OpenAdapt Seal Verification Failed\n\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        "| **Status** | Refused (fail-closed) |\n"
        f"| **Reason** | `{_escape_md_cell(reason)}` |\n"
        f"| **Detail** | {_escape_md_cell(detail)} |\n"
        f"| **Receipt** | `{_escape_md_cell(receipt_arg)}` |\n"
    )
    _write_step_summary(summary)
    print(f"OpenAdapt Seal verification FAILED ({reason}): {detail}", file=sys.stderr)
    return 1


def verify_seal_cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify-seal",
        description="Offline cryptographic verification of an OpenAdapt receipt.",
        allow_abbrev=False,
    )
    parser.add_argument("--receipt", required=True, help="Path to receipt JSON file.")
    parser.add_argument(
        "--public-key",
        default="",
        help="Optional path to an Ed25519 PEM public key or receipt-keys/v1 JSON.",
    )
    parser.add_argument(
        "--keys",
        default="",
        help="Optional path to an openadapt.receipt-keys/v1 JSON registry.",
    )
    parser.add_argument(
        "--key-role",
        default="this_computer",
        help="Role assigned to a raw PEM public key (default: this_computer).",
    )
    parser.add_argument(
        "--openings",
        default="",
        help="Optional path to receipt.private.json openings sidecar.",
    )
    args = parser.parse_args(argv)

    receipt_arg = args.receipt.strip()
    if not receipt_arg:
        return _fail(
            receipt_arg="<empty>",
            reason="missing_receipt",
            detail="receipt input path is empty",
        )
    receipt_path = Path(receipt_arg)
    if not receipt_path.is_file():
        return _fail(
            receipt_arg=receipt_arg,
            reason="missing_receipt",
            detail=f"receipt file not found: {receipt_path}",
        )

    registry: list[RoleKey] = []
    try:
        if args.public_key.strip():
            registry.extend(
                _load_public_key_or_registry(
                    Path(args.public_key.strip()),
                    args.key_role.strip() or "this_computer",
                )
            )
        if args.keys.strip():
            keys_path = Path(args.keys.strip())
            if not keys_path.is_file():
                raise ValueError(f"keys registry file not found: {keys_path}")
            registry.extend(
                parse_registry(loads_strict(keys_path.read_text(encoding="utf-8")))
            )
    except Exception as exc:  # noqa: BLE001 - fail closed on any key/registry error
        return _fail(
            receipt_arg=receipt_arg,
            reason="invalid_key_material",
            detail=str(exc),
        )

    if not registry:
        return _fail(
            receipt_arg=receipt_arg,
            reason="no_trusted_keys",
            detail="no trusted public-key or keys registry supplied; refusing fail-closed",
        )

    try:
        document = loads_strict(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("receipt JSON must be an object")
    except Exception as exc:  # noqa: BLE001 - fail closed on malformed receipt
        return _fail(
            receipt_arg=receipt_arg,
            reason="malformed_receipt",
            detail=str(exc),
        )

    openings_doc: Optional[dict[str, Any]] = None
    if args.openings.strip():
        openings_path = Path(args.openings.strip())
        if not openings_path.is_file():
            return _fail(
                receipt_arg=receipt_arg,
                reason="missing_openings",
                detail=f"openings file not found: {openings_path}",
            )
        try:
            loaded_openings = loads_strict(openings_path.read_text(encoding="utf-8"))
            if not isinstance(loaded_openings, dict):
                raise ValueError("openings JSON must be an object")
            openings_doc = loaded_openings
        except Exception as exc:  # noqa: BLE001
            return _fail(
                receipt_arg=receipt_arg,
                reason="malformed_openings",
                detail=str(exc),
            )

    result = verify_receipt(document, registry, openings=openings_doc)
    if not result.ok:
        return _fail(
            receipt_arg=receipt_arg,
            reason=str(result.reason or "verification_failed"),
            detail=result.detail
            or "; ".join(result.signer_lines)
            or "signature check failed",
        )

    receipt_id = _extract_receipt_id(result.statement)
    outcome = result.outcome or ""
    signer_summary = "; ".join(result.signer_lines) or "Valid Ed25519 signature"
    offline_cmd = f"openadapt verify {shlex.quote(receipt_arg)}"
    if args.keys.strip():
        offline_cmd += f" --keys {shlex.quote(args.keys.strip())}"

    _write_github_outputs(
        verified=True,
        receipt_id=receipt_id,
        outcome=outcome,
        reason="",
    )
    summary = (
        "### ✅ Verified by OpenAdapt Seal\n\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        "| **Status** | Verified offline (Ed25519) |\n"
        f"| **Receipt ID** | `{_escape_md_cell(receipt_id or 'n/a')}` |\n"
        f"| **Run Outcome** | `{_escape_md_cell(outcome or 'n/a')}` |\n"
        f"| **Ed25519 Signature** | {_escape_md_cell(signer_summary)} |\n"
        f"| **Offline Verification** | `{_escape_md_cell(offline_cmd)}` |\n"
    )
    _write_step_summary(summary)
    print(
        f"Verified by OpenAdapt Seal: receipt-id={receipt_id or 'n/a'} "
        f"outcome={outcome or 'n/a'} ({signer_summary})"
    )
    return 0


def main() -> None:
    raise SystemExit(verify_seal_cli())


if __name__ == "__main__":
    main()
