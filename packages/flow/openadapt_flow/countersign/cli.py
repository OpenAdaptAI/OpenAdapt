"""``openadapt-flow countersign``: add, disclose, verify, and export a dossier.

The bundle-encryption command already owns ``openadapt-flow seal``, so these
live under ``countersign``. ``countersign verify`` is the Seal check with the executor,
verification tier, timestamp, and disclosures shown.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

__all__ = ["add_countersign_parser", "load_pinned_registry"]

MAX_DOCUMENT_BYTES = 1 << 20


def load_pinned_registry(
    keys_path: Optional[str] = None, env: Optional[Mapping[str, str]] = None
) -> list[dict[str, Any]]:
    """Pinned Seal keys, from the same places ``openadapt-agent seal`` reads.

    ``--keys``, then ``OPENADAPT_SEAL_KEYS_PATH``, then ``OPENADAPT_SEAL_KEYS_JSON``,
    then ``~/.openadapt/seal-keys.json``. Unreadable means no keys, never a fetch.
    """
    env = os.environ if env is None else env
    try:
        if keys_path:
            raw: Any = json.loads(
                Path(keys_path).expanduser().read_text(encoding="utf-8")
            )
        elif env.get("OPENADAPT_SEAL_KEYS_PATH", "").strip():
            raw = json.loads(
                Path(env["OPENADAPT_SEAL_KEYS_PATH"].strip())
                .expanduser()
                .read_text(encoding="utf-8")
            )
        elif env.get("OPENADAPT_SEAL_KEYS_JSON", "").strip():
            raw = json.loads(env["OPENADAPT_SEAL_KEYS_JSON"])
        else:
            default = Path.home() / ".openadapt" / "seal-keys.json"
            if not default.is_file():
                return []
            raw = json.loads(default.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = raw.get("keys") if isinstance(raw, dict) else raw
    return [dict(e) for e in entries or [] if isinstance(e, dict)]


def _read_json(path: str) -> Any:
    data = Path(path).expanduser().read_bytes()
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"{path} is larger than {MAX_DOCUMENT_BYTES} bytes")
    return json.loads(data.decode("utf-8"))


def _read_document(path: str) -> tuple[Any, list[Any]]:
    """A JSON document, or the data embedded in a dossier page."""
    text = Path(path).expanduser().read_text(encoding="utf-8")
    if text.lstrip().startswith("<"):
        from openadapt_flow.countersign.dossier import extract_dossier_data

        data = extract_dossier_data(text)
        return data["document"], list(data["disclosures"])
    return json.loads(text), []


def _tsa_pem(paths: Sequence[str]) -> Optional[bytes]:
    """Pinned TSA certificates: ``--tsa-cert`` files, else the Sigstore root."""

    if not paths:
        from openadapt_flow.countersign.tsa import sigstore_tsa_root_pem

        return sigstore_tsa_root_pem()
    return b"".join(Path(p).expanduser().read_bytes() for p in paths)


def _authority(args: argparse.Namespace) -> tuple[Any, Optional[bytes]]:
    from openadapt_flow.countersign.tsa import SIGSTORE_TSA_URL, HttpTsa, LocalTsa

    if getattr(args, "no_timestamp", False):
        return None, None
    if getattr(args, "tsa_local", None):
        tsa = LocalTsa.load_or_create(Path(args.tsa_local).expanduser())
        return tsa, tsa.certificate_pem()
    return HttpTsa(getattr(args, "tsa_url", None) or SIGSTORE_TSA_URL), None


def _creation_pins(args: argparse.Namespace, local_pem: Optional[bytes]) -> list[Any]:
    """TSA certificates a new token must verify against before it is kept.

    ``--tsa-cert`` files, else the bundled Sigstore root, plus the local TSA's
    own certificate under ``--tsa-local``. A token from any other TSA is
    refused until that TSA is pinned with ``--tsa-cert``.
    """
    from openadapt_flow.countersign.tsa import load_pem_certificates

    pem = _tsa_pem(getattr(args, "tsa_cert", None) or []) or b""
    return load_pem_certificates(pem + (local_pem or b""))


def _write_out(path: str, value: Any) -> None:
    from openadapt_flow.countersign.vault import write_private_bytes

    write_private_bytes(
        Path(path).expanduser(),
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode(),
    )


def cmd_add(args: argparse.Namespace) -> int:
    from openadapt_flow.countersign.commitments import commit_fields
    from openadapt_flow.countersign.document import (
        CountersignError,
        LocalCountersignIssuer,
        countersign_document,
        locate,
        receipt_sha256,
    )
    from openadapt_flow.countersign.tsa import TimestampError
    from openadapt_flow.countersign.vault import CountersignVault, VaultError

    try:
        document = _read_json(args.document)
        located = locate(document)
        rsha = receipt_sha256(located.receipt)
        vault = CountersignVault(Path(args.vault).expanduser() if args.vault else None)
        commitments = None
        if args.fields_file:
            fields = _read_json(args.fields_file)
            if not isinstance(fields, dict):
                raise ValueError(
                    "the fields file is a JSON object of name -> string value"
                )
            commitments, kept = commit_fields(
                {str(k): str(v) for k, v in fields.items()}
            )
        tsa, tsa_pem = _authority(args)
        baseline = (
            _read_json(args.baseline_timestamp) if args.baseline_timestamp else None
        )
        issuer = None
        if args.countersign_key:
            from cryptography.hazmat.primitives import serialization

            key = serialization.load_pem_private_key(
                Path(args.countersign_key).expanduser().read_bytes(), None
            )
            issuer = LocalCountersignIssuer(key)
        countersigned = countersign_document(
            document,
            tsa=tsa,
            tsa_pem=tsa_pem,
            commitments=commitments,
            baseline_timestamp=baseline,
            issuer=issuer,
            tsa_certificates=_creation_pins(args, tsa_pem),
        )
        # Secrets go to the vault only once the block exists: a TSA failure
        # above must not leave salts behind that no document commits to.
        if commitments:
            vault.put_secrets(rsha, kept)
        vault.put_json(rsha, "document.json", countersigned, overwrite=True)
        _write_out(args.out, countersigned)
        queued = _queue_exception(args, vault, countersigned, tsa_pem)
    except (CountersignError, TimestampError) as exc:
        print(f"add REFUSED: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, VaultError) as exc:
        print(f"add REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"Countersign block added to receipt {rsha}: {args.out}")
    if commitments:
        labels = ", ".join(sorted(commitments["fields"]))
        print(f"Committed {len(commitments['fields'])} field(s): {labels}")
        print(f"Salts and values are in the vault: {vault.root / rsha}")
        print("Delete the fields file once you have checked the vault copy.")
    if tsa is None:
        print("No timestamp requested (--no-timestamp).")
    if queued:
        print(queued)
    return 0


def _queue_exception(
    args: argparse.Namespace, vault: Any, document: Any, tsa_pem: Optional[bytes]
) -> str:
    """Queue a genuine Seal on a REFUTED or INDETERMINATE outcome. Returns a note."""
    from openadapt_flow.countersign.document import verify_countersigned
    from openadapt_flow.countersign.exceptions import ExceptionQueue

    registry = load_pinned_registry(args.keys)
    report = verify_countersigned(
        document, registry=registry, tsa_certificates=_creation_pins(args, tsa_pem)
    )
    if report.outcome_verified:
        return ""
    if not report.signature_genuine:
        return (
            f"Not queued as an exception: the Seal is {report.seal.get('status')} "
            "against the pinned keys."
        )
    entry = ExceptionQueue(vault).enqueue(
        document, report, registry, retention=args.retention_days
    )
    assert entry is not None
    return (
        f"Exception {entry['status']}: {entry['receipt_sha256']} ({entry['verdict']})"
    )


def _queue(args: argparse.Namespace) -> Any:
    from openadapt_flow.countersign.exceptions import ExceptionQueue
    from openadapt_flow.countersign.vault import CountersignVault

    return ExceptionQueue(
        CountersignVault(Path(args.vault).expanduser() if args.vault else None)
    )


def cmd_exceptions(args: argparse.Namespace) -> int:
    entries = _queue(args).entries(None if args.status == "all" else args.status)
    if args.json:
        print(json.dumps(entries, indent=2, sort_keys=True))
        return 0
    if not entries:
        print(f"No {'' if args.status == 'all' else args.status + ' '}exceptions.")
        return 0
    for e in entries:
        line = (
            f"{e['status']:<21} {e['verdict']:<13} {e['receipt_sha256']} "
            f"issued {e.get('issued_at')}"
        )
        if e.get("resolution_receipt_id"):
            line += f", resolved by {e['resolution_receipt_id']}"
        if e.get("disposition"):
            line += f", closed: {e['disposition'].get('disposition_code')}"
        print(line)
    return 0


def _trust_certs(args: argparse.Namespace) -> list[Any]:
    from openadapt_flow.countersign.tsa import load_pem_certificates

    pem = _tsa_pem(args.tsa_cert or [])
    return load_pem_certificates(pem) if pem else []


def cmd_resolve(args: argparse.Namespace) -> int:
    from openadapt_flow.countersign.document import CountersignError

    try:
        resolution, _ = _read_document(args.with_document)
        entry = _queue(args).resolve_by_seal(
            args.entry,
            resolution,
            load_pinned_registry(args.keys),
            tsa_certificates=_trust_certs(args),
        )
    except (CountersignError, OSError, ValueError) as exc:
        print(f"resolve REFUSED: {exc}", file=sys.stderr)
        return 2
    print(
        f"Exception {entry['receipt_sha256']} resolved_by_seal: receipt "
        f"{entry['resolution_receipt_id']}"
    )
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    from cryptography.hazmat.primitives import serialization

    from openadapt_flow.countersign.document import (
        CountersignError,
        LocalCountersignIssuer,
    )
    from openadapt_flow.countersign.vault import VaultError

    try:
        queue = _queue(args)
        key = serialization.load_pem_private_key(
            Path(args.approver_key).expanduser().read_bytes(), None
        )
        notes = (
            Path(args.notes_file).expanduser().read_text(encoding="utf-8")
            if args.notes_file
            else None
        )
        record = queue.disposition_record(
            args.entry,
            signer=LocalCountersignIssuer(key),
            approver_id=args.approver_id,
            role=args.role,
            disposition_code=args.code,
            verification_method=args.method,
            external_reference=args.external_reference,
            notes=notes,
        )
        entry = queue.close_by_attestation(
            args.entry, record, load_pinned_registry(args.approver_keys)
        )
    except (CountersignError, OSError, ValueError, TypeError, VaultError) as exc:
        print(f"close REFUSED: {exc}", file=sys.stderr)
        return 2
    print(
        f"Exception {entry['receipt_sha256']} closed_by_attestation: "
        f"{args.code} by {args.approver_id} ({args.role})"
    )
    if notes:
        print("Notes stay in the vault; only their HMAC is in the record.")
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    from openadapt_flow.countersign.document import CountersignError
    from openadapt_flow.countersign.vault import VaultError

    try:
        purged = _queue(args).purge(Path(args.export).expanduser())
    except (CountersignError, OSError, ValueError, VaultError) as exc:
        print(f"purge REFUSED: {exc}", file=sys.stderr)
        return 2
    if not purged:
        print("Nothing is past its retention period.")
    else:
        print(f"Exported {len(purged)} entries to {args.export}, then purged them.")
    return 0


def cmd_timestamp_baseline(args: argparse.Namespace) -> int:
    from openadapt_flow.countersign.document import timestamp_baseline
    from openadapt_flow.countersign.tsa import TimestampError, verify_timestamp_token

    tsa, tsa_pem = _authority(args)
    if tsa is None:
        print(
            "timestamp-baseline REFUSED: it needs a timestamp; drop --no-timestamp",
            file=sys.stderr,
        )
        return 2
    if args.baseline_sha256:
        digest = args.baseline_sha256
    else:
        digest = (
            "sha256:"
            + hashlib.sha256(Path(args.baseline).expanduser().read_bytes()).hexdigest()
        )
    try:
        entry = timestamp_baseline(
            tsa,
            baseline_sha256=digest,
            baseline_read_at=args.read_at,
            nonce=args.nonce,
            tsa_pem=tsa_pem,
        )
        check = verify_timestamp_token(
            base64.b64decode(entry["token"]),
            bytes.fromhex(entry["imprint_sha256"]),
            _creation_pins(args, tsa_pem),
        )
        if not check.ok:
            raise TimestampError("timestamp_unverified", check.reason)
    except TimestampError as exc:
        print(f"timestamp-baseline REFUSED: {exc}", file=sys.stderr)
        return 2
    _write_out(args.out, entry)
    print(f"Baseline {digest} timestamped for nonce {args.nonce}: {args.out}")
    return 0


def cmd_disclose(args: argparse.Namespace) -> int:
    from openadapt_flow.countersign.commitments import CommitmentError, build_disclosure
    from openadapt_flow.countersign.document import (
        CountersignError,
        locate,
        receipt_sha256,
    )
    from openadapt_flow.countersign.vault import CountersignVault, VaultError

    try:
        located = locate(_read_json(args.document))
        if located.block is None or not located.block.get("commitments"):
            raise CountersignError("no_commitments")
        rsha = receipt_sha256(located.receipt)
        vault = CountersignVault(Path(args.vault).expanduser() if args.vault else None)
        bundle = build_disclosure(
            located.block["commitments"],
            vault.get_secrets(rsha),
            args.field,
            receipt_sha256=rsha,
        )
        _write_out(args.out, bundle)
    except CountersignError as exc:
        print(f"disclose REFUSED: {exc.code}", file=sys.stderr)
        return 2
    except (CommitmentError, VaultError, OSError, ValueError) as exc:
        print(f"disclose REFUSED: {exc}", file=sys.stderr)
        return 2
    print(
        f"Disclosure of {len(args.field)} field(s) written to {args.out} (mode 0600)."
    )
    print("It contains the plaintext values. Send it only to someone entitled to them.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from openadapt_flow.countersign.document import verify_countersigned
    from openadapt_flow.countersign.render import render_report
    from openadapt_flow.countersign.tsa import load_pem_certificates

    try:
        document, embedded = _read_document(args.document)
        disclosures = embedded + [_read_json(p) for p in args.disclosure or []]
        pem = _tsa_pem(args.tsa_cert or [])
        certs = load_pem_certificates(pem) if pem else []
    except (OSError, ValueError) as exc:
        print(f"NOT VERIFIED: unreadable input ({exc})", file=sys.stderr)
        return 1
    report = verify_countersigned(
        document,
        registry=load_pinned_registry(args.keys),
        tsa_certificates=certs,
        disclosures=disclosures,
        accept_non_flow=args.accept_non_flow,
    )
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(render_report(report))
    return report.exit_code


def cmd_dossier(args: argparse.Namespace) -> int:
    from openadapt_flow.countersign.document import CountersignError
    from openadapt_flow.countersign.dossier import build_dossier, write_dossier
    from openadapt_flow.countersign.render import headline

    try:
        document = _read_json(args.document)
        disclosures = [_read_json(p) for p in args.disclosure or []]
        registry = load_pinned_registry(args.keys)
        pem = _tsa_pem(args.tsa_cert or [])
        from openadapt_flow.countersign.tsa import load_pem_certificates

        ledger = _queue(args).ledger(
            registry, load_pem_certificates(pem) if pem else []
        )
        data, report = build_dossier(
            document,
            registry=registry,
            tsa_certificates_pem=pem,
            ledger=ledger,
            disclosures=disclosures,
            screenshots=[Path(p).expanduser() for p in args.screenshot or []],
            accept_non_flow=args.accept_non_flow,
        )
        fmt = args.format or ("zip" if args.out.endswith(".zip") else "html")
        out = write_dossier(data, report, Path(args.out).expanduser(), fmt=fmt)
    except CountersignError as exc:
        print(f"dossier REFUSED: {exc.code}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"dossier REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"Dossier written: {out}")
    print(headline(report))
    print(
        f"Exceptions: Total Seals {ledger['total_seals']} = Verified "
        f"{ledger['counts']['verified']} + Resolved_by_seal "
        f"{ledger['counts']['resolved_by_seal']} + Closed_by_attestation "
        f"{ledger['counts']['closed_by_attestation']} + Open "
        f"{ledger['counts']['open']}"
    )
    for note in data["notes"]:
        print(note)
    if disclosures:
        print(
            "It contains disclosed values. Share it only with someone entitled to them."
        )
    return 0


def _add_tsa(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--tsa-url",
        help="RFC 3161 TSA URL (default: Sigstore's timestamp.sigstore.dev). Only a 32-byte digest is sent",
    )
    g.add_argument(
        "--no-timestamp",
        action="store_true",
        help="Do not request a timestamp (offline use)",
    )
    g.add_argument(
        "--tsa-local",
        metavar="DIR",
        help="Offline TSA keeping its key in DIR. Test only: its time is this machine's clock",
    )


def _add_creation_pin(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--tsa-cert",
        action="append",
        metavar="PEM",
        help="Pinned TSA certificate or CA the new token must verify against before it is kept (repeatable; default: the bundled Sigstore TSA root)",
    )


def _add_trust(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--keys", help="Pinned Seal key file (default: same as openadapt-agent seal)"
    )
    p.add_argument(
        "--tsa-cert",
        action="append",
        metavar="PEM",
        help="Pinned TSA certificate or CA (repeatable; default: the bundled Sigstore TSA root). Certificates inside the document are never trusted",
    )
    p.add_argument(
        "--disclosure",
        action="append",
        metavar="JSON",
        help="Disclosure bundle to check (repeatable)",
    )
    p.add_argument(
        "--accept-non-flow",
        action="store_true",
        help="Exit 0 for a verified effect receipt from an agent, script, or person",
    )


def add_countersign_parser(sub: Any) -> None:
    p = sub.add_parser(
        "countersign",
        help="Timestamp, countersign, selectively disclose, and export a dispute dossier for a Seal",
    )
    nsub = p.add_subparsers(dest="countersign_cmd", required=True)

    n = nsub.add_parser(
        "add",
        help="Add a countersign block: field commitments, RFC 3161 timestamp, countersignature",
    )
    n.add_argument(
        "document", help="Receipt, effect-receipt wire document, or report.json"
    )
    n.add_argument(
        "-o", "--out", required=True, help="Countersigned document to write (mode 0600)"
    )
    n.add_argument(
        "--fields-file",
        help="JSON object of sensitive fields to commit; values go to the vault only",
    )
    _add_tsa(n)
    _add_creation_pin(n)
    n.add_argument(
        "--baseline-timestamp",
        help="Output of 'countersign timestamp-baseline' for this run",
    )
    n.add_argument("--countersign-key", help="Ed25519 PEM for a local countersignature")
    n.add_argument(
        "--vault",
        help="Vault directory (default $OPENADAPT_COUNTERSIGN_VAULT or ~/.openadapt/countersign-vault)",
    )
    n.add_argument(
        "--keys",
        help="Pinned Seal key registry used to decide whether to queue an exception",
    )
    n.add_argument(
        "--retention-days",
        type=int,
        help="Keep a closed exception this long (default 2190, minimum 183, or $OPENADAPT_EXCEPTION_RETENTION_DAYS)",
    )
    n.set_defaults(func=cmd_add)

    from openadapt_flow.countersign.exceptions import (
        DISPOSITION_CODES,
        VERIFICATION_METHODS,
    )

    q = nsub.add_parser(
        "exceptions", help="List Seals queued on a REFUTED or INDETERMINATE outcome"
    )
    q.add_argument(
        "--status",
        choices=["open", "resolved_by_seal", "closed_by_attestation", "all"],
        default="open",
    )
    q.add_argument("--json", action="store_true")
    q.add_argument("--vault")
    q.set_defaults(func=cmd_exceptions)

    r = nsub.add_parser(
        "resolve",
        help="Resolve an exception with a later verified Seal that links to it",
    )
    r.add_argument("entry", help="receipt_sha256 of the open entry")
    r.add_argument(
        "--with",
        dest="with_document",
        required=True,
        metavar="DOCUMENT",
        help="Countersigned document of the later receipt (added with 'countersign add')",
    )
    r.add_argument(
        "--keys", help="Pinned Seal key file; the keys the entry was queued under"
    )
    r.add_argument(
        "--tsa-cert",
        action="append",
        metavar="PEM",
        help="Pinned TSA certificate or CA (repeatable)",
    )
    r.add_argument("--vault")
    r.set_defaults(func=cmd_resolve)

    c = nsub.add_parser(
        "close",
        help="Close an exception with a disposition signed by a person whose key is pinned",
    )
    c.add_argument("entry", help="receipt_sha256 of the open entry")
    c.add_argument(
        "--approver-id", required=True, help="Opaque approver id (no name or email)"
    )
    c.add_argument(
        "--role", required=True, help="Approver role, for example nurse-manager"
    )
    c.add_argument("--code", required=True, choices=sorted(DISPOSITION_CODES))
    c.add_argument("--method", required=True, choices=sorted(VERIFICATION_METHODS))
    c.add_argument(
        "--external-reference",
        required=True,
        help="Ticket or case id; only its HMAC is recorded",
    )
    c.add_argument(
        "--notes-file", help="Free-text notes; kept only in the vault (0600)"
    )
    c.add_argument("--approver-key", required=True, help="The approver's Ed25519 PEM")
    c.add_argument(
        "--approver-keys",
        required=True,
        help="Pinned approver key registry; the signature must verify against it",
    )
    c.add_argument("--vault")
    c.set_defaults(func=cmd_close)

    g = nsub.add_parser(
        "purge",
        help="Export, then delete, closed exceptions past their retention period",
    )
    g.add_argument("--export", required=True, help="Export file to write first (0600)")
    g.add_argument("--vault")
    g.set_defaults(func=cmd_purge)

    b = nsub.add_parser(
        "timestamp-baseline", help="Timestamp a baseline read before the executor runs"
    )
    src = b.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--baseline", help="Baseline file to hash locally (it is never sent)"
    )
    src.add_argument("--baseline-sha256", help="sha256:<hex> of the baseline")
    b.add_argument("--read-at", required=True, help="baseline_read_at (RFC 3339)")
    b.add_argument(
        "--nonce", required=True, help="The run nonce the baseline belongs to"
    )
    b.add_argument("-o", "--out", required=True)
    _add_tsa(b)
    _add_creation_pin(b)
    b.set_defaults(func=cmd_timestamp_baseline)

    d = nsub.add_parser(
        "disclose", help="Write a disclosure bundle revealing chosen committed fields"
    )
    d.add_argument("document", help="Countersigned document")
    d.add_argument(
        "--field",
        action="append",
        required=True,
        metavar="LABEL",
        help="Commitment label to reveal (repeatable)",
    )
    d.add_argument("-o", "--out", required=True)
    d.add_argument("--vault")
    d.set_defaults(func=cmd_disclose)

    v = nsub.add_parser(
        "verify",
        help="Check a Seal offline and show executor, verification tier, timestamps, and disclosures",
    )
    v.add_argument(
        "document",
        help="Countersigned document, receipt, report.json, or dossier .html",
    )
    _add_trust(v)
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_verify)

    x = nsub.add_parser(
        "dossier", help="Export one offline-verifiable HTML or zip for a dispute"
    )
    x.add_argument("document", help="Countersigned document")
    x.add_argument("-o", "--out", required=True, help="Output .html or .zip")
    x.add_argument("--format", choices=["html", "zip"])
    x.add_argument(
        "--screenshot",
        action="append",
        metavar="PNG",
        help="Embedded only after automatic redaction; left out when no redaction provider is installed",
    )
    _add_trust(x)
    x.add_argument("--vault", help="Vault whose exception ledger the dossier includes")
    x.set_defaults(func=cmd_dossier)
