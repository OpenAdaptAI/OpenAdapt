"""``openadapt-flow no-outbound``: see and edit the strict-mode allow list."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


def _file(args: argparse.Namespace) -> Path:
    from openadapt_flow.no_outbound import ENV_ALLOW_FILE

    value = args.file or os.environ.get(ENV_ALLOW_FILE)
    if not value:
        raise SystemExit(f"no-outbound: pass --file or set {ENV_ALLOW_FILE}")
    return Path(value)


def _cmd_list(args: argparse.Namespace) -> int:
    from openadapt_flow.no_outbound_allow import AllowList, AllowListError, describe

    try:
        allow = AllowList.load_or_empty(_file(args))
    except AllowListError as exc:
        print(f"no-outbound: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps([asdict(e) for e in allow.entries], indent=2, sort_keys=True))
    elif not allow.entries:
        print("allow list is empty: only this machine is reachable")
    else:
        for e in allow.entries:
            print(describe(e))
    return 0


def _cmd_derive(args: argparse.Namespace) -> int:
    from openadapt_flow.no_outbound_allow import AllowListError, derive, describe

    try:
        added, present = derive(Path(args.profile), _file(args))
    except AllowListError as exc:
        print(f"no-outbound: {exc}", file=sys.stderr)
        return 2
    for e in added:
        print(f"added: {describe(e)}")
    for e in present:
        print(f"already allowed: {describe(e)}")
    if not added and not present:
        print("nothing added")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    from openadapt_flow.no_outbound_allow import (
        AllowList,
        AllowListError,
        describe,
        manual_entry,
    )

    path = _file(args)
    try:
        allow = AllowList.load_or_empty(path)
        added = allow.add([manual_entry(args.network, args.port)])
    except AllowListError as exc:
        print(f"no-outbound: {exc}", file=sys.stderr)
        return 2
    allow.save(path)
    print(f"added: {describe(added[0])}" if added else "already allowed")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    from openadapt_flow.no_outbound_allow import AllowList, AllowListError, describe

    path = _file(args)
    try:
        allow = AllowList.load_or_empty(path)
        gone = allow.remove(args.network)
    except (AllowListError, ValueError) as exc:
        print(f"no-outbound: {exc}", file=sys.stderr)
        return 2
    allow.save(path)
    for e in gone:
        print(f"removed: {describe(e)}")
    if not gone:
        print(f"{args.network} was not in the allow list")
        return 1
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "no-outbound", help="Strict no-outbound mode: list and edit the allow list"
    )
    s = p.add_subparsers(dest="no_outbound_cmd", required=True)
    a = s.add_parser("allow", help="The allow-list file")
    a.add_argument(
        "--file",
        default=None,
        help="Allow-list JSON (default: OPENADAPT_NO_OUTBOUND_ALLOW_FILE)",
    )
    t = a.add_subparsers(dest="allow_cmd", required=True)

    ls = t.add_parser("list", help="Show every allowed address and where it came from")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=_cmd_list)

    d = t.add_parser(
        "derive",
        help="Allow only the host(s) a remote-desktop session profile connects to",
    )
    d.add_argument("profile", help="FreeRDP session JSON or .rdp file")
    d.set_defaults(func=_cmd_derive)

    ad = t.add_parser("add", help="Allow an IP address or private CIDR range by hand")
    ad.add_argument("network")
    ad.add_argument("--port", type=int, default=None)
    ad.set_defaults(func=_cmd_add)

    rm = t.add_parser("remove", help="Remove an address or range")
    rm.add_argument("network")
    rm.set_defaults(func=_cmd_remove)
