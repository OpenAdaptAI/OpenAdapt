"""CLI: ``openadapt-flow handoff`` -- attach a run's receipt to what one agent
hands another, and verify one on arrival. See ``docs/HANDOFFS.md``.

    openadapt-flow handoff send result.json --run RUN_DIR --out result.handoff.json
    openadapt-flow handoff send claim.pdf --run RUN_DIR --detached
    openadapt-flow handoff send result.json --run RUN_DIR --post https://agent.example/inbox
    openadapt-flow handoff verify result.handoff.json --keys keys.json
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


def handoff_group(sub: argparse._SubParsersAction) -> argparse._SubParsersAction:
    """The ``handoff`` group's subcommands, created once and shared.

    ``send``, ``verify``, ``key`` and ``trust`` register here; the
    counterparty confirmations' ``list`` registers into the same group,
    whichever module registers first.
    """
    parser = sub.choices.get("handoff")
    if parser is None:
        parser = sub.add_parser(
            "handoff",
            help=(
                "Hand a result to another agent with its run's receipt, verify "
                "one on arrival, and follow handoffs to other parties"
            ),
            allow_abbrev=False,
        )
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return parser.add_subparsers(dest="handoff_command", required=True)


def register(sub: argparse._SubParsersAction) -> None:
    hsub = handoff_group(sub)

    s = hsub.add_parser(
        "send",
        help="Attach a run's receipt to a payload (inline, detached, or POSTed)",
        allow_abbrev=False,
    )
    s.add_argument("payload", help="the file to hand off ('-' reads stdin)")
    source = s.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--run", metavar="RUN_DIR", help="the run whose receipt.json to attach"
    )
    source.add_argument(
        "--receipt", metavar="FILE", help="a receipt.json or receipt.card.json"
    )
    s.add_argument(
        "--public",
        action="store_true",
        help="with --run, attach the public card (receipt.card.json) instead",
    )
    s.add_argument(
        "--detached",
        action="store_true",
        help=(
            "leave the payload out and write PAYLOAD.handoff.json beside it, "
            "for a file sent as an attachment"
        ),
    )
    s.add_argument("--media-type", help="the payload's media type")
    s.add_argument(
        "--unbound",
        action="store_true",
        help=(
            "attach the receipt as it is, without binding this payload to it. "
            "A receiver that checks the payload refuses it (payload_not_bound)"
        ),
    )
    s.add_argument("--out", help="where to write the handoff (default: stdout)")
    s.add_argument(
        "--post",
        metavar="URL",
        help=(
            "POST the handoff to this URL (a webhook or agent inbox). https, "
            "or http only to this computer"
        ),
    )
    s.add_argument(
        "--ledger",
        metavar="DIR",
        help=(
            "also record the handoff in this handoff ledger, so `handoff list` "
            "shows it until the other party's system confirms it"
        ),
    )
    s.add_argument(
        "--counterparty",
        help="with --ledger: who receives it, for people ('receiving clinic')",
    )
    s.add_argument(
        "--key-field",
        help=(
            "with --ledger: the payload's JSON field that identifies the item "
            "in the other party's system (default: the payload digest)"
        ),
    )
    s.add_argument(
        "--follow-up-hours",
        type=float,
        default=72.0,
        help="with --ledger: when to follow up if nothing is confirmed (default 72)",
    )
    s.set_defaults(func=cmd_send)

    v = hsub.add_parser(
        "verify",
        help="Verify a handoff's receipt against your pinned keys",
        allow_abbrev=False,
    )
    v.add_argument("handoff", help="a .handoff.json document ('-' reads stdin)")
    v.add_argument(
        "--payload",
        help=(
            "the detached payload file (default: the handoff's path without "
            ".handoff.json, if it exists)"
        ),
    )
    v.add_argument(
        "--keys",
        metavar="REGISTRY",
        help=(
            "your pinned key registry (openadapt.receipt-keys/v1). Default: "
            "this computer's key and pinned OpenAdapt Cloud keys"
        ),
    )
    v.add_argument("--json", action="store_true", help="print the verdict as JSON")
    v.set_defaults(func=cmd_verify)

    k = hsub.add_parser(
        "key",
        help="Print this computer's receipt key, for another agent to trust",
        allow_abbrev=False,
    )
    k.set_defaults(func=cmd_key)

    t = hsub.add_parser(
        "trust",
        help="Add another party's key (from `handoff key`) to your key registry",
        allow_abbrev=False,
    )
    t.add_argument("key_file", help="the other party's `handoff key` output")
    t.add_argument("--org", required=True, help="who they are, e.g. 'Example Clinic'")
    t.add_argument(
        "--keys",
        required=True,
        metavar="REGISTRY",
        help="your registry file (created if missing)",
    )
    t.set_defaults(func=cmd_trust)


def _read(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read()
    return Path(path).read_bytes()


def _post_allowed(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return True
    if parsed.scheme != "http" or not parsed.hostname:
        return False
    if parsed.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def post_handoff(url: str, document: dict[str, Any], *, timeout: float = 30.0) -> int:
    """POST a handoff document as its own media type. Returns the HTTP status."""
    from openadapt_flow.handoff import MEDIA_TYPE

    if not _post_allowed(url):
        raise ValueError("--post needs an https URL, or http to this computer")
    request = urllib.request.Request(
        url,
        data=json.dumps(document).encode("utf-8"),
        headers={"Content-Type": MEDIA_TYPE},
        method="POST",
    )
    # No redirects: an allowed URL must not hand the document on to a target
    # _post_allowed would refuse. A 3xx raises HTTPError (an OSError).
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:  # noqa: S310 - scheme checked
        return int(response.status)


def record_in_ledger(
    directory: Path,
    data: bytes,
    *,
    counterparty: str,
    key_field: Optional[str],
    follow_up_hours: float,
    run_id: Optional[str],
) -> Any:
    """Record a sent handoff in the counterparty-confirmation ledger.

    The same record ``handoff list`` reads (:class:`HandoffLedger`): the
    item's key and each top-level text field of a JSON payload, as salted
    digests only. A payload that isn't a JSON object is recorded by its
    digest, with no fields.
    """
    import hashlib

    try:
        from openadapt_flow.handoffs.ledger import HandoffLedger
    except ImportError:
        raise ImportError(
            "this openadapt-flow has no handoff ledger (openadapt_flow.handoffs)"
        ) from None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        value = None
    fields: dict[str, tuple[str, str]] = {}
    key = None
    if isinstance(value, dict):
        if key_field is not None:
            key = value.get(key_field)
            if not isinstance(key, str) or not key:
                raise ValueError(f"the payload has no text field {key_field!r}")
        for name, field_value in sorted(value.items()):
            if name != key_field and isinstance(field_value, str) and field_value:
                fields[name] = (field_value, "text")
    elif key_field is not None:
        raise ValueError("--key-field needs a JSON object payload")
    if key is None:
        key = "sha256:" + hashlib.sha256(data).hexdigest()
    return HandoffLedger(directory).send(
        key,
        fields,
        counterparty=counterparty,
        follow_up_hours=follow_up_hours,
        source="agent_handoff",
        run_id=run_id,
    )


def cmd_send(args: argparse.Namespace) -> int:
    from openadapt_flow import handoff as h

    if args.detached and args.payload == "-":
        print("handoff send: --detached needs a payload file", file=sys.stderr)
        return 2
    if args.public and not args.run:
        print("handoff send: --public goes with --run", file=sys.stderr)
        return 2
    if args.ledger and not args.counterparty:
        print("handoff send: --ledger needs --counterparty", file=sys.stderr)
        return 2
    if (args.counterparty or args.key_field) and not args.ledger:
        print(
            "handoff send: --counterparty and --key-field go with --ledger",
            file=sys.stderr,
        )
        return 2
    try:
        data = _read(args.payload)
        if args.run:
            receipt = h.receipt_for_run(Path(args.run), public=args.public)
        else:
            receipt = h._loads_strict(Path(args.receipt).read_text("utf-8"))
        signer = None
        if not args.unbound:
            from openadapt_flow.offline_seal import seal_home, signer_for

            signer = signer_for(seal_home())
        document = h.make_handoff(
            data,
            receipt,
            name=None if args.payload == "-" else args.payload,
            media_type=args.media_type,
            detached=args.detached,
            signer=signer,
        )
    except h.HandoffError as exc:
        print(f"handoff send: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(
            f"handoff send: no receipt key to bind the payload ({exc})", file=sys.stderr
        )
        return 2
    except (OSError, ValueError) as exc:
        print(f"handoff send: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.ledger:
        try:
            entry = record_in_ledger(
                Path(args.ledger),
                data,
                counterparty=args.counterparty,
                key_field=args.key_field,
                follow_up_hours=args.follow_up_hours,
                run_id=Path(args.run).name if args.run else None,
            )
        except (ImportError, ValueError) as exc:
            print(f"handoff send: not recorded in the ledger: {exc}", file=sys.stderr)
            return 2
        print(f"handoff: recorded {entry.id} in {args.ledger}", file=sys.stderr)
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    out: Optional[Path] = Path(args.out) if args.out else None
    if out is None and args.detached:
        out = h.detached_path(Path(args.payload))
    if out is not None:
        out.write_text(text, encoding="utf-8")
        print(f"handoff: {out}", file=sys.stderr)
    elif not args.post:
        sys.stdout.write(text)
    if args.post:
        try:
            status = post_handoff(args.post, document)
        except (OSError, ValueError) as exc:
            print(f"handoff send: POST failed: {exc}", file=sys.stderr)
            return 1
        print(f"handoff: POST {args.post} -> HTTP {status}", file=sys.stderr)
        if status >= 300:
            return 1
    return 0


def _print_verdict(verdict: dict[str, Any]) -> None:
    for line in verdict["signer_lines"]:
        print(line)
    if verdict["integrity"] != "intact":
        print(verdict["what_this_proves"][0])
        return
    print(f"Outcome: {verdict['outcome']}")
    checks = verdict["checks"]
    print("Checks passed: " + (", ".join(checks["passed"]) or "none"))
    if checks["incomplete"]:
        print("Checks not complete: " + ", ".join(checks["incomplete"]))
    print("Not checked: " + (", ".join(checks["not_checked"]) or "nothing listed"))
    for line in verdict["confirmation_lines"]:
        print(line)
    for line in verdict["what_this_proves"]:
        print(line)


def cmd_verify(args: argparse.Namespace) -> int:
    from openadapt_types.receipt import RegistryError

    from openadapt_flow import handoff as h

    try:
        raw = _read(args.handoff)
        payload: Optional[bytes] = None
        if args.payload:
            payload = Path(args.payload).read_bytes()
        elif args.handoff != "-" and args.handoff.endswith(h.DETACHED_SUFFIX):
            beside = Path(args.handoff[: -len(h.DETACHED_SUFFIX)])
            if beside.is_file():
                payload = beside.read_bytes()
        registry = h.load_registry(Path(args.keys) if args.keys else None)
    except RegistryError as exc:
        print(
            f"handoff verify: the key registry is refused ({exc.reason})",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError) as exc:
        print(f"handoff verify: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    verdict = h.verify_handoff(raw, registry, payload=payload).to_dict()
    if args.json:
        print(json.dumps(verdict, indent=2, sort_keys=True))
    else:
        _print_verdict(verdict)
    return 0 if verdict["integrity"] == "intact" else 1


def cmd_key(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import public_key_entry, seal_home

    try:
        entry = public_key_entry(seal_home())
    except Exception as exc:  # noqa: BLE001 - no usable key: say so, sign nothing
        print(f"handoff key: no receipt key on this computer ({exc})", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"key_id": entry["key_id"], "public_key": entry["public_key"]}, indent=2
        )
    )
    return 0


def cmd_trust(args: argparse.Namespace) -> int:
    """Pin another party's key as ``customer_key`` with the org you name.

    Their receipts then verify as "Signed by <org>'s key", never as this
    computer. The registry is parsed again after the change, so a bad
    entry is refused before it is written.
    """
    from openadapt_types import receipt as r

    try:
        entry = r.loads_strict(Path(args.key_file).read_text("utf-8"))
        path = Path(args.keys)
        registry = (
            r.loads_strict(path.read_text("utf-8"))
            if path.is_file()
            else {"schema": r.REGISTRY_SCHEMA, "keys": []}
        )
        if not isinstance(entry, dict) or set(entry) - {
            "key_id",
            "public_key",
            "status",
        }:
            raise ValueError("the key file is one `handoff key` entry")
        if not isinstance(registry, dict):
            raise ValueError("the keys file must be a JSON object")
        raw_keys = registry.get("keys", [])
        if not isinstance(raw_keys, list):
            raise ValueError("the keys file 'keys' field must be a list")
        keys: list[Any] = [
            k
            for k in raw_keys
            if not isinstance(k, dict) or k.get("key_id") != entry.get("key_id")
        ]
        keys.append(
            {
                "key_id": entry.get("key_id"),
                "public_key": entry.get("public_key"),
                "role": "customer_key",
                "org": args.org,
                "status": "active",
            }
        )
        updated = {"schema": r.REGISTRY_SCHEMA, "keys": keys}
        r.parse_registry(updated)
    except r.RegistryError as exc:
        print(f"handoff trust: refused ({exc.reason}): {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"handoff trust: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    path.write_text(
        json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"handoff: {args.org}'s key {entry['key_id']} is trusted in {path}",
        file=sys.stderr,
    )
    return 0
