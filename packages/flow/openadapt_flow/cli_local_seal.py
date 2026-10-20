"""``openadapt-flow local-seal``: offline Seals and optional batched timestamps."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def _home(args: argparse.Namespace) -> Path:
    from openadapt_flow.offline_seal import seal_home

    return Path(args.home).expanduser() if args.home else seal_home()


def _signer(args: argparse.Namespace) -> Any:
    """The configured Seal key. Raises LocalSealError when it can't be opened."""
    from openadapt_flow.offline_seal import LocalSealError, signer_for
    from openadapt_flow.seal_keys import KeyUnavailable

    signer = signer_for(_home(args), spec=args.key)
    try:
        signer.public_raw()  # a token or keychain that can't answer stops here
    except KeyUnavailable as exc:
        raise LocalSealError(exc.code, str(exc)) from exc
    return signer


def _registry(args: argparse.Namespace) -> Any:
    from openadapt_types.seal import parse_key_registry

    from openadapt_flow.offline_seal import load_registry

    if args.keys:
        return load_registry(Path(args.keys))
    # No pinned file: trust only the configured Seal key, and say so.
    signer = _signer(args)
    print(
        f"keys: this machine's Seal key ({signer.describe()}); "
        "pass --keys to pin others",
        file=sys.stderr,
    )
    return parse_key_registry([signer.registry_entry()])


def _refused(exc: Any) -> int:
    print(f"not sealed: {exc}", file=sys.stderr)
    return 1


def _cmd_run(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import LocalSealError, seal_run

    try:
        seal = seal_run(Path(args.run_dir), home=_home(args), signer=_signer(args))
    except LocalSealError as exc:
        return _refused(exc)
    print(json.dumps(seal.wire(), indent=2, sort_keys=True))
    return 0


def _cmd_file(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import LocalSealError, enqueue_for_batch, sign_file

    try:
        seal = sign_file(
            Path(args.path),
            subject_kind=args.kind,
            run_id=args.run_id,
            home=_home(args),
            signer=_signer(args),
        )
    except LocalSealError as exc:
        return _refused(exc)
    out = Path(args.out) if args.out else Path(str(args.path) + ".seal.json")
    out.write_text(
        json.dumps(seal.wire(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    enqueue_for_batch(seal, _home(args))
    print(str(out))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from openadapt_flow import no_outbound
    from openadapt_flow.offline_seal import (
        LocalSealError,
        check_anchor,
        loads_strict,
        verify_local_seal,
        verify_run,
    )
    from openadapt_flow.seal_anchor import fetch_entry

    try:
        registry = _registry(args)
    except LocalSealError as exc:
        print(f"not valid: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"not valid: unusable key registry ({exc})", file=sys.stderr)
        return 1
    try:
        log_keys = _log_keys(args)
    except (OSError, ValueError) as exc:
        print(f"not valid: unusable log key ({exc})", file=sys.stderr)
        return 1
    fetch = None
    if args.live:
        if not args.require_anchor:
            print("--live needs --require-anchor", file=sys.stderr)
            return 2
        if no_outbound.enabled():
            print("not checked live: strict no-outbound mode is on", file=sys.stderr)
            return 1
        fetch = fetch_entry
    target = Path(args.target)
    if target.is_dir():
        result = verify_run(
            target,
            registry,
            strict=args.strict,
            require_anchor=args.require_anchor,
            log_keys=log_keys,
            log_url=args.log_url,
            fetch=fetch,
        )
    else:
        if not args.seal:
            print("verify FILE needs --seal FILE.seal.json", file=sys.stderr)
            return 2
        try:
            seal = loads_strict(Path(args.seal).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("not valid: malformed", file=sys.stderr)
            return 1
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        result = verify_local_seal(seal, digest, registry)
        if args.require_anchor:
            result = check_anchor(
                seal,
                Path(args.seal),
                result,
                registry,
                log_keys=log_keys,
                log_url=args.log_url,
                fetch=fetch,
            )
    if result.ok:
        where = target / result.subject_file if result.subject_file else target
        print(f"valid offline Seal over {where}, key {result.key_id}")
        if result.subject_file == "report.pre-seal.json":
            print(
                "note: report.json differs from what was sealed; the Seal covers "
                "report.pre-seal.json, the copy attach-seal kept (--strict refuses this)"
            )
        if result.anchor is not None:
            when = _utc(result.anchor.integrated_time)
            how = "checked live and offline" if fetch else "checked offline"
            print(
                f"anchored in the transparency log at {when}: entry "
                f"{result.anchor.uuid}, log index {result.anchor.log_index} ({how})"
            )
        return 0
    print(f"not valid: {result.reason}", file=sys.stderr)
    return 1


def _log_keys(args: argparse.Namespace) -> Any:
    from urllib.parse import urlparse

    from openadapt_flow.seal_anchor import LogKey, default_log_keys

    if not args.log_key:
        return default_log_keys()
    origin = args.log_origin or urlparse(args.log_url).hostname or ""
    return [LogKey.from_pem(Path(p).read_bytes(), origin=origin) for p in args.log_key]


def _utc(seconds: Any) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(seconds), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _cmd_anchor(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import SEAL_FILE, LocalSealError
    from openadapt_flow.seal_anchor import AnchorError, anchor_path, anchor_seal

    target = Path(args.target)
    seal_path = target / SEAL_FILE if target.is_dir() else target
    try:
        signer = _signer(args)
        anchor = anchor_seal(
            seal_path, signer, log_url=args.log_url, log_keys=_log_keys(args)
        )
    except (LocalSealError, AnchorError, OSError, ValueError) as exc:
        print(f"not anchored: {exc}", file=sys.stderr)
        return 1
    entry = anchor["entry"]
    print(f"anchored: {anchor_path(seal_path)}")
    print(
        f"entry {entry['uuid']}, log index {entry['logIndex']}, at {_utc(entry['integratedTime'])}"
    )
    print(f"{args.log_url.rstrip('/')}/api/v1/log/entries/{entry['uuid']}")
    return 0


def _cmd_key(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import LocalSealError

    try:
        signer = _signer(args)
    except LocalSealError as exc:
        print(f"no key: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"keys": [signer.registry_entry()]}, indent=2))
    return 0


def _cmd_key_create(args: argparse.Namespace) -> int:
    from openadapt_flow.seal_keys import KeyUnavailable, create_key

    if not args.key:
        print(
            "key create needs --key file:PATH or --key keychain:NAME", file=sys.stderr
        )
        return 2
    try:
        signer = create_key(args.key)
    except KeyUnavailable as exc:
        print(f"no key created: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"keys": [signer.registry_entry()]}, indent=2))
    return 0


def _read_registry_file(path: Path) -> list[dict[str, Any]]:
    from openadapt_flow.offline_seal import load_registry, loads_strict

    if not path.exists():
        return []
    load_registry(path)  # refuse to edit a registry that does not parse
    data = loads_strict(path.read_text(encoding="utf-8"))
    entries = data.get("keys") if isinstance(data, dict) else data
    return [dict(e) for e in entries or []]


def _write_registry_file(path: Path, entries: list[dict[str, Any]]) -> None:
    from openadapt_types.seal import parse_key_registry

    parse_key_registry(entries)
    path.write_text(json.dumps({"keys": entries}, indent=2) + "\n", encoding="utf-8")


def _cmd_key_pin(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import LocalSealError

    try:
        entry = _signer(args).registry_entry()
    except LocalSealError as exc:
        print(f"no key: {exc}", file=sys.stderr)
        return 1
    path = Path(args.registry)
    try:
        entries = _read_registry_file(path)
    except ValueError as exc:
        print(f"not pinned: {exc}", file=sys.stderr)
        return 1
    if any(e.get("key_id") == entry["key_id"] for e in entries):
        print(f"already pinned: {entry['key_id']}")
        return 0
    _write_registry_file(path, entries + [entry])
    print(f"pinned {entry['key_id']} in {path}")
    return 0


def _cmd_key_revoke(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    path = Path(args.registry)
    try:
        entries = _read_registry_file(path)
    except ValueError as exc:
        print(f"not revoked: {exc}", file=sys.stderr)
        return 1
    at = args.at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = False
    for entry in entries:
        if entry.get("key_id") == args.key_id:
            entry["status"], entry["revoked_at"] = "revoked", at
            found = True
    if not found:
        print(f"{args.key_id} is not in {path}", file=sys.stderr)
        return 1
    try:
        _write_registry_file(path, entries)
    except ValueError as exc:
        print(f"not revoked: {exc}", file=sys.stderr)
        return 1
    print(
        f"revoked {args.key_id} at {at}; every Seal it signed is now refused, "
        "unless a transparency-log anchor shows the log recorded it before then "
        "(verify --require-anchor)"
    )
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    from openadapt_flow.seal_batch import build_batch

    path = build_batch(_home(args))
    print(str(path) if path else "nothing queued")
    return 0


def _authority(args: argparse.Namespace) -> Any:
    try:
        from openadapt_flow.countersign.tsa import SIGSTORE_TSA_URL, HttpTsa
    except ImportError:
        return None
    return HttpTsa(args.tsa_url or SIGSTORE_TSA_URL)


def _cmd_submit(args: argparse.Namespace) -> int:
    from openadapt_flow.seal_batch import submit_all

    authority = _authority(args)
    if authority is None:
        print(
            "not submitted: countersign_unavailable (this build has no "
            "openadapt_flow.countersign; install the countersign extra)",
            file=sys.stderr,
        )
        return 1
    results = submit_all(authority, _home(args))
    for r in results:
        print(f"{r.batch.name}: {r.status}{(' ' + r.detail) if r.detail else ''}")
    return (
        0
        if all(r.status in ("timestamped", "already_timestamped") for r in results)
        else 1
    )


def _cmd_proof(args: argparse.Namespace) -> int:
    from openadapt_flow.seal_batch import proof_for

    seal = json.loads(Path(args.seal).read_text(encoding="utf-8"))
    found = proof_for(seal, _home(args))
    if not found:
        print("not in any batch yet", file=sys.stderr)
        return 1
    print(json.dumps(found, indent=2, sort_keys=True))
    return 0 if found["verified_inclusion"] else 1


def _log_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--log-url",
        default="https://rekor.sigstore.dev",
        help="The transparency log's URL (default the public Sigstore Rekor)",
    )
    p.add_argument(
        "--log-origin",
        default=None,
        help="Checkpoint origin name for --log-key (default the --log-url host)",
    )


def register(sub: Any) -> None:
    p = sub.add_parser(
        "local-seal",
        help="Offline Seals signed with this machine's key; optional batched timestamps",
    )
    p.add_argument(
        "--home",
        default=None,
        help="Seal home (or OPENADAPT_SEAL_HOME; default ~/.openadapt/seal)",
    )
    p.add_argument(
        "--key",
        default=None,
        help=(
            "Seal key: file:PATH, keychain:NAME or pkcs11:URI (or "
            "OPENADAPT_SEAL_KEY; default a key generated under the Seal home)"
        ),
    )
    s = p.add_subparsers(dest="local_seal_cmd", required=True)

    r = s.add_parser("run", help="Seal a run directory's report.json")
    r.add_argument("run_dir")
    r.set_defaults(func=_cmd_run)

    f = s.add_parser("file", help="Seal any file: a record, an export, an audit report")
    f.add_argument("path")
    f.add_argument("--kind", choices=["record", "export", "file"], default="file")
    f.add_argument("--run-id", default=None)
    f.add_argument("--out", default=None, help="Default: PATH.seal.json")
    f.set_defaults(func=_cmd_file)

    v = s.add_parser("verify", help="Verify a run directory, or a file with --seal")
    v.add_argument("target")
    v.add_argument("--seal", default=None)
    v.add_argument("--keys", default=None, help="Pinned key registry JSON")
    v.add_argument(
        "--strict",
        action="store_true",
        help="Require the Seal to match report.json itself, not report.pre-seal.json",
    )
    v.add_argument(
        "--require-anchor",
        action="store_true",
        help="Also require a transparency-log anchor (seal.anchor.json), checked offline",
    )
    v.add_argument(
        "--log-key",
        action="append",
        default=[],
        help="Pinned log public key PEM (default: the public Sigstore Rekor key)",
    )
    v.add_argument(
        "--live",
        action="store_true",
        help="Also fetch the entry from the log at --log-url and compare (needs network)",
    )
    _log_args(v)
    v.set_defaults(func=_cmd_verify)

    a = s.add_parser(
        "anchor",
        help=(
            "Log a Seal's digest in Sigstore Rekor (opt-in; sends a digest and "
            "the public key, never report content)"
        ),
    )
    a.add_argument("target", help="A run directory, or a .seal.json file")
    a.add_argument(
        "--log-key",
        action="append",
        default=[],
        help="The log's public key PEM, to check its answer (default: Sigstore Rekor)",
    )
    _log_args(a)
    a.set_defaults(func=_cmd_anchor)

    k = s.add_parser("key", help="Print the Seal key's public half for pinning")
    k.set_defaults(func=_cmd_key)
    ks = k.add_subparsers(dest="key_cmd")
    kc = ks.add_parser(
        "create", help="Make a new key at --key file:PATH or keychain:NAME"
    )
    kc.set_defaults(func=_cmd_key_create)
    kp = ks.add_parser("pin", help="Add the Seal key to a pinned key registry")
    kp.add_argument("registry", help="Key registry JSON to create or extend")
    kp.set_defaults(func=_cmd_key_pin)
    kr = ks.add_parser("revoke", help="Mark a pinned key revoked from a time on")
    kr.add_argument("registry")
    kr.add_argument("key_id")
    kr.add_argument("--at", default=None, help="UTC YYYY-MM-DDTHH:MM:SSZ (default now)")
    kr.set_defaults(func=_cmd_key_revoke)

    b = s.add_parser("batch", help="Fold queued Seals into one Merkle batch (local)")
    b.set_defaults(func=_cmd_batch)

    t = s.add_parser(
        "submit",
        help="Timestamp every unsent batch root (sends one 32-byte digest per batch)",
    )
    t.add_argument(
        "--tsa-url", default=None, help="RFC 3161 TSA URL (default Sigstore)"
    )
    t.set_defaults(func=_cmd_submit)

    q = s.add_parser(
        "proof", help="Show the batch, root and inclusion proof for a Seal"
    )
    q.add_argument("seal")
    q.set_defaults(func=_cmd_proof)
