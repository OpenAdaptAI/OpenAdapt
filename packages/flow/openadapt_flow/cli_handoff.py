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


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "handoff",
        help=(
            "Attach a run's receipt to what one agent hands another, and "
            "verify one on arrival"
        ),
        allow_abbrev=False,
    )
    hsub = p.add_subparsers(dest="handoff_command", required=True)

    s = hsub.add_parser(
        "send",
        help="Attach a run's receipt to a payload (inline, detached, or POSTed)",
        allow_abbrev=False,
    )
    s.add_argument("payload", help="the file to hand off ('-' reads stdin)")
    source = s.add_mutually_exclusive_group(required=True)
    source.add_argument("--run", metavar="RUN_DIR", help="the run whose receipt.json to attach")
    source.add_argument("--receipt", metavar="FILE", help="a receipt.json or receipt.card.json")
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
    s.add_argument("--out", help="where to write the handoff (default: stdout)")
    s.add_argument(
        "--post",
        metavar="URL",
        help=(
            "POST the handoff to this URL (a webhook or agent inbox). https, "
            "or http only to this computer"
        ),
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
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - scheme checked
        return int(response.status)


def cmd_send(args: argparse.Namespace) -> int:
    from openadapt_flow import handoff as h

    if args.detached and args.payload == "-":
        print("handoff send: --detached needs a payload file", file=sys.stderr)
        return 2
    if args.public and not args.run:
        print("handoff send: --public goes with --run", file=sys.stderr)
        return 2
    try:
        data = _read(args.payload)
        if args.run:
            receipt = h.receipt_for_run(Path(args.run), public=args.public)
        else:
            receipt = h._loads_strict(Path(args.receipt).read_text("utf-8"))
        document = h.make_handoff(
            data,
            receipt,
            name=None if args.payload == "-" else args.payload,
            media_type=args.media_type,
            detached=args.detached,
        )
    except h.HandoffError as exc:
        print(f"handoff send: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"handoff send: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
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
        print(f"handoff verify: the key registry is refused ({exc.reason})", file=sys.stderr)
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
