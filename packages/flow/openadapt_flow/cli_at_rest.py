"""``openadapt-flow at-rest``: who can open encrypted runs, and shredding them.

- ``status`` prints where this computer's device key is kept, its public
  recipient, the organization recipients new sets are wrapped for, and the
  encryption mode. It prints no key material.
- ``share DIR --recipient age1...`` wraps an encrypted recording, bundle or
  run for more recipients. No file is re-encrypted.
- ``shred DIR --yes`` deletes every wrapped key of an encrypted set, which
  makes it unreadable by anyone. It can't be undone.

See docs/phi_at_rest.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _cmd_status(args: argparse.Namespace) -> int:
    from openadapt_flow import at_rest, envelope

    try:
        org = envelope.org_recipients()
        device = envelope.device_identity(create=False)
        mode = (
            "strict"
            if at_rest.strict()
            else ("off" if at_rest.opt_out_reason() is not None else "on")
        )
    except (envelope.EnvelopeError, at_rest.LocalKeyError, ValueError) as exc:
        print(f"at-rest: {exc}", file=sys.stderr)
        return 2
    status = {
        "encryption": mode,
        "configured_key": at_rest.explicit_key(),
        "device_key": at_rest.key_location(),
        "device_recipient": envelope.device_recipient(device) if device else None,
        "org_recipients": org,
        "policy_file": str(envelope.policy_path()),
    }
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    print(f"Encryption at rest: {mode}")
    print(f"Device key: {status['device_key'] or 'not created yet'}")
    if status["device_recipient"]:
        print(f"Device recipient: {status['device_recipient']}")
    print("Organization recipients: " + (", ".join(org) if org else "none"))
    if status["configured_key"]:
        print("A configured key (OPENADAPT_BUNDLE_KEY) is also a recipient.")
    return 0


def _cmd_share(args: argparse.Namespace) -> int:
    from openadapt_flow import envelope, sealed_set

    try:
        recipients = sealed_set.add_recipients(Path(args.dir), list(args.recipient))
    except (sealed_set.SealedSetError, envelope.EnvelopeError) as exc:
        print(f"at-rest share: {exc}", file=sys.stderr)
        return 2
    print(f"{args.dir} is now wrapped for {len(recipients)} recipient(s):")
    for item in recipients:
        print(f"  {item}")
    return 0


def _cmd_shred(args: argparse.Namespace) -> int:
    from openadapt_flow import sealed_set

    root = Path(args.dir)
    if not sealed_set.is_sealed(root):
        print(f"at-rest shred: {root} is not an encrypted set", file=sys.stderr)
        return 2
    if not args.yes:
        print(
            f"at-rest shred: this makes {root} unreadable for everyone, and it "
            "can't be undone. Pass --yes to do it.",
            file=sys.stderr,
        )
        return 2
    try:
        removed = sealed_set.shred(root)
    except sealed_set.SealedSetError as exc:
        print(f"at-rest shred: {exc}", file=sys.stderr)
        return 2
    print(
        f"Shredded {root}: its wrapped keys are deleted, so nobody can open it"
        + (f"; also deleted {len(removed)} checkpoint file(s)." if removed else ".")
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "at-rest",
        help=(
            "Encryption at rest: show this computer's device key and recipients, "
            "share an encrypted run with another recipient, or crypto-shred it"
        ),
        allow_abbrev=False,
    )
    rsub = p.add_subparsers(dest="at_rest_cmd", required=True)
    s = rsub.add_parser(
        "status", help="Where the key is kept and who can read new runs"
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_status)
    s = rsub.add_parser(
        "share", help="Wrap an encrypted recording, bundle or run for more recipients"
    )
    s.add_argument("dir", help="The encrypted directory")
    s.add_argument(
        "--recipient",
        action="append",
        required=True,
        metavar="AGE1...",
        help="An age X25519 recipient (repeat for more)",
    )
    s.set_defaults(func=_cmd_share)
    s = rsub.add_parser(
        "shred", help="Delete an encrypted set's wrapped keys; nobody can open it after"
    )
    s.add_argument("dir", help="The encrypted directory")
    s.add_argument("--yes", action="store_true", help="Confirm; this can't be undone")
    s.set_defaults(func=_cmd_shred)
