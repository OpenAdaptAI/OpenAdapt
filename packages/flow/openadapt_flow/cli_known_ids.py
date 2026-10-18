"""``openadapt-flow known-ids``: admit a known-ID export and check it."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cmd_confirm(args: argparse.Namespace) -> int:
    from openadapt_flow.runtime.known_ids import (
        KnownIdListError,
        confirm_export,
        file_sha256,
        load_export,
    )

    export = Path(args.export)
    try:
        ids = load_export(export, args.column)
    except (OSError, KnownIdListError) as exc:
        print(f"known-ids: {exc}", file=sys.stderr)
        return 2
    exported = _parse_time(args.exported_at) or datetime.fromtimestamp(
        export.stat().st_mtime, tz=timezone.utc
    )
    print(f"export:      {export}")
    print(f"source:      {args.source}")
    print(f"rows:        {len(ids)}")
    print(f"sha256:      {file_sha256(export)}")
    print(f"exported at: {exported.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    if not args.yes:
        answer = input(
            "Is this export from the system of record, and does it list EVERY "
            "identifier in the namespace? Type 'yes' to confirm: "
        )
        if answer.strip().lower() != "yes":
            print("not confirmed", file=sys.stderr)
            return 1
    try:
        record = confirm_export(
            export,
            source=args.source,
            confirmed_by=args.by or getpass.getuser(),
            column=args.column,
            exported_at=exported,
            confirmation_path=args.confirmation,
        )
    except KnownIdListError as exc:
        print(f"known-ids: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(record.wire(), indent=2, sort_keys=True))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from openadapt_flow.runtime.known_ids import (
        KnownIdAdmission,
        KnownIdAdmissionHalt,
        KnownIdListError,
    )

    try:
        adm = KnownIdAdmission.load(
            args.export,
            column=args.column,
            confirmation_path=args.confirmation,
            max_age_s=args.max_age_hours * 3600.0,
        )
    except KnownIdAdmissionHalt as exc:
        print(json.dumps({"usable": False, "reason": exc.reason, "detail": exc.detail}))
        return 1
    except (OSError, KnownIdListError) as exc:
        print(f"known-ids: {exc}", file=sys.stderr)
        return 2
    c = adm.confirmation
    print(
        json.dumps(
            {
                "usable": True,
                "source": c.source,
                "rows": c.row_count,
                "confirmed_by": c.confirmed_by,
                "confirmed_at": c.confirmed_at,
                "exported_at": c.exported_at,
                "sha256": c.sha256,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "known-ids",
        help="Admit a system-of-record export as the known-ID list, and check it",
    )
    s = p.add_subparsers(dest="known_ids_cmd", required=True)

    def common(a: argparse.ArgumentParser) -> None:
        a.add_argument("export", help="Export file (CSV/TSV, JSON, or one ID per line)")
        a.add_argument("--column", default=None, help="Identifier column (CSV/TSV)")
        a.add_argument(
            "--confirmation",
            default=None,
            help="Confirmation record (default: EXPORT.confirmation.json)",
        )

    c = s.add_parser(
        "confirm", help="Confirm the export's source and completeness (recorded)"
    )
    common(c)
    c.add_argument("--source", required=True, help="Which system and export this is")
    c.add_argument("--by", default=None, help="Who confirms (default: your user name)")
    c.add_argument(
        "--exported-at",
        default=None,
        help="When the export was taken (default: file time)",
    )
    c.add_argument("--yes", action="store_true", help="Skip the interactive prompt")
    c.set_defaults(func=_cmd_confirm)

    st = s.add_parser("status", help="Would replay accept this list now?")
    common(st)
    st.add_argument("--max-age-hours", type=float, default=24.0)
    st.set_defaults(func=_cmd_status)
