"""CLI: ``openadapt-flow receipt`` -- check a signed write receipt offline,
export the public keys a checker pins, and anchor a receipt in Rekor. See
``docs/WRITE_RECEIPTS.md``.

    openadapt-flow receipt verify write.receipt.cbor \\
        --issuer-key issuer.pub.pem --log-key log.pub.pem
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "receipt",
        help="Check a signed write receipt offline, or export the keys to check it",
        allow_abbrev=False,
    )
    rsub = p.add_subparsers(dest="receipt_command", required=True)

    v = rsub.add_parser(
        "verify", help="Check a write receipt against pinned keys", allow_abbrev=False
    )
    v.add_argument("file", help="a .receipt.cbor file")
    v.add_argument(
        "--issuer-key", required=True, help="the issuer's public key (PEM or key entry)"
    )
    v.add_argument("--log-key", help="the transparency log's public key (PEM)")
    v.add_argument(
        "--no-log",
        action="store_true",
        help="skip the log inclusion check (the output says so)",
    )
    v.add_argument(
        "--evidence", help="the private evidence file, to check the commitment opens"
    )
    v.add_argument(
        "--require-anchor",
        action="store_true",
        help="also require a Rekor anchor (FILE.anchor.json), checked offline",
    )
    v.add_argument("--json", action="store_true", help="print the result as JSON")
    v.set_defaults(func=cmd_verify)

    k = rsub.add_parser(
        "keys", help="Export the public keys a checker pins", allow_abbrev=False
    )
    ksub = k.add_subparsers(dest="keys_command", required=True)
    e = ksub.add_parser(
        "export", help="Write issuer.pub.pem and log.pub.pem to DIR", allow_abbrev=False
    )
    e.add_argument("dir")
    e.set_defaults(func=cmd_keys_export)

    a = rsub.add_parser(
        "anchor",
        help="Put the receipt's digest in Sigstore Rekor (sends a digest, the "
        "public key and a signature; nothing else)",
        allow_abbrev=False,
    )
    a.add_argument("file")
    a.set_defaults(func=cmd_anchor)


def _pem(raw: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    return Ed25519PublicKey.from_public_bytes(raw).public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def cmd_keys_export(args: argparse.Namespace) -> int:
    from openadapt_flow import write_receipt as wr
    from openadapt_flow.receipt_log import LocalLog

    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        issuer = wr.default_signer()
        (out / "issuer.pub.pem").write_bytes(_pem(issuer.public_raw()))
        (out / "log.pub.pem").write_bytes(_pem(LocalLog().public_raw()))
    except Exception as exc:  # noqa: BLE001
        print(f"receipt keys: {exc}", file=sys.stderr)
        return 2
    print(f"issuer {issuer.key_id}: {out / 'issuer.pub.pem'}")
    print(f"log: {out / 'log.pub.pem'}")
    return 0


def cmd_anchor(args: argparse.Namespace) -> int:
    from openadapt_flow import write_receipt as wr
    from openadapt_flow.seal_anchor import AnchorError

    try:
        doc = wr.anchor(Path(args.file))
    except (AnchorError, wr.ReceiptError, OSError, ValueError) as exc:
        print(f"receipt anchor: {exc}", file=sys.stderr)
        return 2
    entry = doc.get("entry") or {}
    print(
        f"anchored: Rekor log index {entry.get('logIndex')}, {wr.anchor_path(args.file)}"
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from openadapt_flow import write_receipt as wr

    if not args.log_key and not args.no_log:
        print("receipt verify: give --log-key (or --no-log)", file=sys.stderr)
        return 2
    path = Path(args.file)
    try:
        data = path.read_bytes()
        result = wr.verify(
            data,
            issuer_public=wr.load_public_key(Path(args.issuer_key)),
            log_public=None if args.no_log else wr.load_public_key(Path(args.log_key)),
            evidence=Path(args.evidence).read_bytes() if args.evidence else None,
        )
        if args.require_anchor:
            from openadapt_flow.offline_seal import loads_strict

            anchor_file = wr.anchor_path(path)
            doc = (
                loads_strict(anchor_file.read_text(encoding="utf-8"))
                if anchor_file.is_file()
                else None
            )
            checked = wr.verify_anchor(
                data, doc, wr.load_public_key(Path(args.issuer_key))
            )
            if not checked.ok:
                raise wr.ReceiptError("anchor", str(checked.reason))
            result.anchor = f"Rekor log index {checked.log_index}"
    except wr.ReceiptError as exc:
        _fail(args, exc.code, str(exc))
        return 1
    except ImportError as exc:
        print(
            f"receipt verify: {exc.name or exc} is missing; "
            "pip install 'openadapt-flow[receipts]'",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError) as exc:
        _fail(args, "unreadable", str(exc))
        return 1
    p = result.payload
    if args.json:
        print(
            json.dumps(
                {
                    "valid": True,
                    "receipt_id": result.sha256,
                    "log_index": result.log_index,
                    "evidence_opened": result.evidence_opened,
                    "anchor": result.anchor,
                    "notes": result.notes,
                    "payload": p.wire(),
                },
                indent=2,
            )
        )
        return 0
    s = p.scope
    print(f"VALID write receipt {result.sha256[:16]}, signed by {p.issuer}")
    print(
        f"verdict:   {p.verdict}"
        + (f" (provisional until {p.recheck_due_at})" if p.provisional else "")
    )
    print(f"source:    {p.source}; write {p.subject.write_id}")
    print(
        f"read:      {', '.join(s.stores) or 'nothing'} ({', '.join(s.read_paths) or '-'})"
    )
    print(f"strength:  {s.strength}")
    print(f"window:    {s.window.baseline_read_at or '-'} to {s.window.read_at or '-'}")
    if p.checked:
        print(
            "checked:   " + ", ".join(f"{k} {v}" for k, v in sorted(p.checked.items()))
        )
    if p.supersedes:
        print(f"replaces:  {p.supersedes[:16]}")
    print(
        "logged:    "
        + (
            "not checked"
            if result.log_index is None
            else f"local log leaf {result.log_index}"
        )
    )
    if result.anchor:
        print(f"anchored:  {result.anchor}")
    if result.evidence_opened:
        print("evidence:  the evidence file opens the commitment")
    for note in result.notes:
        print(f"note:      {note}")
    print("NOT checked:")
    for line in p.not_checked:
        print(f"  - {line}")
    return 0


def _fail(args: argparse.Namespace, code: str, detail: str) -> None:
    if args.json:
        print(json.dumps({"valid": False, "code": code, "detail": detail}))
    else:
        print(f"INVALID write receipt: {detail}", file=sys.stderr)
