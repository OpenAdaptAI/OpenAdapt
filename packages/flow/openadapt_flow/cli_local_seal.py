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


def _registry(args: argparse.Namespace) -> Any:
    from openadapt_types.seal import parse_key_registry

    from openadapt_flow.offline_seal import load_registry, public_key_entry

    if args.keys:
        return load_registry(Path(args.keys))
    # No pinned file: trust only this machine's own key, and say so.
    print(
        "keys: this machine's own Seal key (pass --keys to pin others)", file=sys.stderr
    )
    return parse_key_registry([public_key_entry(_home(args))])


def _cmd_run(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import LocalSealError, seal_run

    try:
        seal = seal_run(Path(args.run_dir), home=_home(args))
    except LocalSealError as exc:
        print(f"not sealed: {exc.code}", file=sys.stderr)
        return 1
    print(json.dumps(seal.wire(), indent=2, sort_keys=True))
    return 0


def _cmd_file(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import enqueue_for_batch, sign_file

    seal = sign_file(
        Path(args.path), subject_kind=args.kind, run_id=args.run_id, home=_home(args)
    )
    out = Path(args.out) if args.out else Path(str(args.path) + ".seal.json")
    out.write_text(
        json.dumps(seal.wire(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    enqueue_for_batch(seal, _home(args))
    print(str(out))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import verify_local_seal, verify_run

    registry = _registry(args)
    target = Path(args.target)
    if target.is_dir():
        result = verify_run(target, registry)
    else:
        if not args.seal:
            print("verify FILE needs --seal FILE.seal.json", file=sys.stderr)
            return 2
        seal = json.loads(Path(args.seal).read_text(encoding="utf-8"))
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        result = verify_local_seal(seal, digest, registry)
    if result.ok:
        where = f" over {result.subject_file}" if result.subject_file else ""
        print(f"valid offline Seal{where}, key {result.key_id}")
        return 0
    print(f"not valid: {result.reason}", file=sys.stderr)
    return 1


def _cmd_key(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import public_key_entry

    print(json.dumps({"keys": [public_key_entry(_home(args))]}, indent=2))
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
    v.set_defaults(func=_cmd_verify)

    k = s.add_parser("key", help="Print this machine's public Seal key for pinning")
    k.set_defaults(func=_cmd_key)

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
