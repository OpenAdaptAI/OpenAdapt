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


#: Commands that read sealed input without decrypting it to disk:
#: ``inspect report-run`` reads the report in memory; ``shred`` touches keys.
SEALED_READY: frozenset[str] = frozenset(
    {
        "openadapt_flow.__main__:_cmd_report_run",
        "openadapt_flow.cli_at_rest:_cmd_shred",
    }
)
#: Commands that write no recorded data.
STRICT_SAFE: frozenset[str] = frozenset(
    {
        "openadapt_flow.cli_at_rest:_cmd_status",
        "openadapt_flow.cli_at_rest:_cmd_recovery_create",
    }
)


def desktop_recording_key(backend: str, out: object, explicit: object) -> object:
    """The key for a desktop recording: its raw capture is plaintext while it
    records and the converted recording is sealed (docs/phi_at_rest.md).
    Strict mode holds the raw window back; the opt-out records plaintext."""
    from openadapt_flow import at_rest, sealed_set

    if at_rest.strict():
        at_rest.unsealed_write(f"a {backend} raw capture session", str(out))
    if at_rest.opt_out_reason() is not None:
        at_rest.plaintext_allowed(f"a desktop recording at {out}")
        return None
    return explicit or sealed_set.recording_key()


def lacks_key(root: object, kind: str) -> bool:
    """Whether no key is configured to open ``root``. A version 3 set needs
    none: a key this computer holds may open it."""
    from openadapt_flow import crypto, sealed_set

    if sealed_set.read_header(root).get("version") == sealed_set.ENVELOPE_VERSION:  # type: ignore[arg-type]
        return False
    return not (sealed_set.passphrase_for(kind) or crypto.resolve_read_key(None))


def add_no_encrypt(p: argparse.ArgumentParser) -> None:
    """``--no-encrypt`` on ``compile``, ``run`` and ``replay``."""
    p.add_argument(
        "--no-encrypt",
        action="store_true",
        help=(
            "Write this command's output in plaintext (recorded in "
            "~/.openadapt/plaintext-at-rest.jsonl, with a warning). Output is "
            "encrypted by default; same as OPENADAPT_ENCRYPT_AT_REST=0. "
            "Ignored under OPENADAPT_FLOW_AT_REST=strict"
        ),
    )


def apply_no_encrypt(args: argparse.Namespace, record_func: object) -> None:
    """``compile/run/replay --no-encrypt``: this process writes plaintext, with
    the one-line warning and the ledger line at the first write. ``record``
    handles its own flag."""
    from openadapt_flow import at_rest

    if (
        getattr(args, "no_encrypt", False)
        and getattr(args, "func", None) is not record_func
    ):
        at_rest.opt_out_for_this_process("--no-encrypt")


def note_plaintext_recording(artifact: str, out: object) -> None:
    """Name and record a recording Flow can't encrypt yet (strict refuses)."""
    from openadapt_flow import at_rest

    try:
        at_rest.unsealed_write(artifact, str(out or "--out"))
    except at_rest.AtRestRefused as exc:
        raise SystemExit(f"record: {exc}") from exc


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
    s = rsub.add_parser(
        "status",
        help="Where the keys are and who can open new runs",
        allow_abbrev=False,
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=_cmd_status)
    s = rsub.add_parser("recovery", help="The solo recovery key", allow_abbrev=False)
    rec = s.add_subparsers(dest="recovery_cmd", required=True)
    c = rec.add_parser(
        "create", help="Make a recovery key; the code is shown once", allow_abbrev=False
    )
    c.add_argument("--replace", action="store_true")
    c.set_defaults(func=_cmd_recovery_create)
    s = rsub.add_parser(
        "shred",
        help="Destroy an encrypted set's bucket key; nobody can open it after",
        allow_abbrev=False,
    )
    s.add_argument("dir", help="The encrypted directory")
    s.add_argument("--yes", action="store_true", help="Confirm; this can't be undone")
    s.set_defaults(func=_cmd_shred)
