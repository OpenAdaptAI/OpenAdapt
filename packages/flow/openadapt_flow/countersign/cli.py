"""``openadapt-flow countersign``: add, disclose, verify, and export a dossier.

The bundle-encryption command already owns ``openadapt-flow seal``, so these
live under ``countersign``. ``countersign verify`` is the Seal check with the executor,
verification tier, timestamp, and disclosures shown.
"""

from __future__ import annotations

import argparse
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
            vault.put_secrets(rsha, kept)
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
        )
        vault.put_json(rsha, "document.json", countersigned, overwrite=True)
        _write_out(args.out, countersigned)
    except (CountersignError, TimestampError) as exc:
        print(f"add REFUSED: {exc.code}", file=sys.stderr)
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
    return 0


def cmd_timestamp_baseline(args: argparse.Namespace) -> int:
    from openadapt_flow.countersign.document import timestamp_baseline
    from openadapt_flow.countersign.tsa import TimestampError

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
    except TimestampError as exc:
        print(f"timestamp-baseline REFUSED: {exc.code}", file=sys.stderr)
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
        data, report = build_dossier(
            document,
            registry=load_pinned_registry(args.keys),
            tsa_certificates_pem=_tsa_pem(args.tsa_cert or []),
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
    n.add_argument(
        "--baseline-timestamp",
        help="Output of 'countersign timestamp-baseline' for this run",
    )
    n.add_argument("--countersign-key", help="Ed25519 PEM for a local countersignature")
    n.add_argument(
        "--vault",
        help="Vault directory (default $OPENADAPT_COUNTERSIGN_VAULT or ~/.openadapt/countersign-vault)",
    )
    n.set_defaults(func=cmd_add)

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
    x.set_defaults(func=cmd_dossier)
