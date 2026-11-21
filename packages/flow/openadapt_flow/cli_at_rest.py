"""``openadapt-flow at-rest``: who can open encrypted runs, recovery and shredding.

- ``status`` prints the mode, the policy and where it came from, the device
  keys (hardware or software), the organization recipients by fingerprint,
  the recovery key and how many key records await the archive. It prints no
  key material.
- ``recovery create`` makes a solo recovery key: the code is shown once and
  never stored; its public half becomes a recipient of every new run.
- ``shred DIR --yes`` destroys an encrypted set's bucket key (every tracked
  copy), so nobody can open it. It can't be undone.

See docs/phi_at_rest.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RECOVERY_WARNING = (
    "Anyone with this code can open every recording and run on this computer, "
    "including ones that show patient or customer data. Store it offline. "
    "OpenAdapt can't recover it for you."
)


def _cmd_status(args: argparse.Namespace) -> int:
    from openadapt_flow import at_rest
    from openadapt_flow import key_hierarchy as kh

    try:
        policy = kh.load_policy()
        status = kh.describe(policy)
        mode = (
            "strict"
            if at_rest.strict()
            else ("off" if at_rest.opt_out_reason() is not None else "on")
        )
    except (kh.KeyHierarchyError, at_rest.LocalKeyError, ValueError) as exc:
        print(f"at-rest: {exc}", file=sys.stderr)
        return 2
    status = {"encryption": mode, "configured_key": at_rest.explicit_key(), **status}
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    print(f"Encryption at rest: {mode}")
    print(
        f"Mode: {status['mode']} (records {status['records']}); policy: {status['policy_source']}"
    )
    print(f"Device runtime key: {status['device_runtime_key'] or 'not created yet'}")
    print(f"Device viewer key: {status['device_viewer_key'] or 'not created yet'}")
    for item in status["org_recipients"]:
        print(f"Organization recipient ({item['status']}): {item['fingerprint']}")
    if status["mode"] == "org":
        print(f"Key records not yet archived: {status['unarchived']}")
    elif not status["recovery_key"]:
        print("No recovery key. Create one: openadapt-flow at-rest recovery create")
    if status["configured_key"]:
        print("A configured key (OPENADAPT_BUNDLE_KEY) is set: deprecated.")
    return 0


def _cmd_recovery_create(args: argparse.Namespace) -> int:
    from openadapt_flow import key_hierarchy as kh

    if kh.recovery_recipient() is not None and not args.replace:
        print(
            "at-rest recovery: a recovery key exists. Pass --replace to make a new "
            "one for new runs (older runs stay on the old one).",
            file=sys.stderr,
        )
        return 2
    code, recipient = kh.new_recovery_code()
    kh.save_recovery_recipient(recipient)
    print(RECOVERY_WARNING)
    print()
    print(f"Recovery code: {code}")
    print()
    print(
        f"New runs are also wrapped to this key ({recipient.fpr}). To open with it, "
        f"set {kh.RECOVERY_CODE_ENV}."
    )
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
        record = sealed_set.shred(root)
    except sealed_set.SealedSetError as exc:
        print(f"at-rest shred: {exc}", file=sys.stderr)
        return 2
    copies = ", ".join(record.get("copies_destroyed") or []) or "none tracked"  # type: ignore[arg-type]
    print(f"Shredded {root}: bucket key copies destroyed: {copies}.")
    if record.get("records") == "inline":
        print(
            "This run kept its key record inside its files (solo mode), so a "
            "backup copy still opens with the recovery key until you delete it."
        )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "at-rest",
        help=(
            "Encryption at rest: show the device keys, policy and recipients, "
            "make a recovery key, or crypto-shred an encrypted run"
        ),
        allow_abbrev=False,
    )
    rsub = p.add_subparsers(dest="at_rest_cmd", required=True)
    s = rsub.add_parser("status", help="Where the keys are and who can open new runs")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_status)
    s = rsub.add_parser("recovery", help="The solo recovery key")
    rec = s.add_subparsers(dest="recovery_cmd", required=True)
    c = rec.add_parser("create", help="Make a recovery key; the code is shown once")
    c.add_argument("--replace", action="store_true")
    c.set_defaults(func=_cmd_recovery_create)
    s = rsub.add_parser(
        "shred", help="Destroy an encrypted set's bucket key; nobody can open it after"
    )
    s.add_argument("dir", help="The encrypted directory")
    s.add_argument("--yes", action="store_true", help="Confirm; this can't be undone")
    s.set_defaults(func=_cmd_shred)
