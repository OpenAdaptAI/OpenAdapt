"""Read accounts out of a mock ledger app with proof, review the problem, export.

Synthetic data, local OCR, no network and no model calls. Run from
packages/flow:

    python examples/records/demo.py --out /tmp/records-demo
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from accounts_app import ACCOUNTS, LABELS, AccountsApp, AppClipboard, region  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from openadapt_flow.records.export import CsvSink, DbApiSink, export  # noqa: E402
from openadapt_flow.records.read import add_read, channel_read, read_field  # noqa: E402
from openadapt_flow.records.review import ReviewQueue, apply_decision  # noqa: E402
from openadapt_flow.records.schema import (  # noqa: E402
    FieldSpec,
    GradeGate,
    GradeTooLow,
    ReadProof,
    RecordRead,
    RecordSchema,
    SealRef,
)

SCHEMA = RecordSchema(
    name="ledger_account",
    fields=(
        FieldSpec(name="account_number", kind="code"),
        FieldSpec(name="holder", kind="text"),
        FieldSpec(name="balance", kind="currency"),
        FieldSpec(name="opened", kind="date"),
        FieldSpec(name="status", kind="enum", choices=("Open", "Closed")),
    ),
    identity_fields=("account_number",),
    # Proof is opt-in. This demo turns all of it on; without this line each
    # field is read once, through the clipboard, as unconfirmed.
    proof=ReadProof(double_read=True, committed_readback=True, seal=True),
)
RUN_ID = "demo-run-1"
RUNNER = "runner:demo"


def read_record(
    app: AccountsApp, key: str, *, session: str = "acting", ocr=None
) -> RecordRead:
    app.open_record(key)
    clipboard = AppClipboard(app)
    fields = tuple(
        read_field(
            SCHEMA.field(name),
            app,
            region(name),
            proof=SCHEMA.proof_for(name),
            clipboard=clipboard,
            session=session,
            step_id=f"read-{name}",
            seal=SealRef(run_id=RUN_ID),
            ocr=ocr,
        )
        for name in LABELS
    )
    app.close_record()
    return RecordRead(
        schema_name=SCHEMA.name,
        schema_version=SCHEMA.version,
        record_key=key,
        run_id=RUN_ID,
        read_at=datetime.now(timezone.utc).isoformat(),
        fields=fields,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("records-demo"))
    parser.add_argument("--min-grade", default="committed_record_readback")
    parser.add_argument(
        "--simulated-ocr",
        action="store_true",
        help="Use the app's drawn text instead of the OCR engine (fast, for tests).",
    )
    args = parser.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    app = AccountsApp()
    # The screen draws 7,905.25 as 7,965.25 for one account; copy is right.
    app.display_overrides[("AC-3307", "balance")] = "$7,965.25"
    ocr = app.simulated_ocr if args.simulated_ocr else None

    # 1. Double read (clipboard + OCR) in the acting session.
    records = [read_record(app, key, ocr=ocr) for key in ACCOUNTS]

    # 2. Committed-record read-back: reopen each record by id, read again.
    for i, record in enumerate(records):
        again = read_record(app, record.record_key, session="reopened", ocr=ocr)
        fields = []
        for first, second in zip(record.fields, again.fields):
            spec = SCHEMA.field(first.field)
            clip = next(r for r in second.reads if r.channel == "clipboard")
            fields.append(add_read(spec, first, clip))
        records[i] = record.model_copy(update={"fields": tuple(fields)})

    # 3. Independent channel: the app's scheduled report export, taken from
    #    saved data rather than the open session, so it is committed_data.
    export_file = app.export_csv(out / "app_report_export.csv")
    rows = {row["account_number"]: row for row in csv.DictReader(export_file.open())}
    for i, record in enumerate(records):
        row = rows[record.record_key]
        fields = tuple(
            add_read(
                SCHEMA.field(f.field),
                f,
                channel_read(
                    SCHEMA.field(f.field),
                    "export",
                    row[f.field],
                    export_source="committed_data",
                ),
            )
            for f in record.fields
        )
        records[i] = record.model_copy(update={"fields": fields})

    for record in records:
        print(f"{record.record_key}: grade={record.grade} review={record.needs_review}")
        for f in record.fields:
            if f.needs_review:
                reads = [(r.channel, r.raw) for r in f.reads]
                print(f"  {f.field}: {f.review_reason}; reads={reads}")

    # 4. Review queue: only the problem shows up; a person settles it.
    queue = ReviewQueue(out / "review")
    for record in records:
        queue.add_record(record, created_by=RUNNER)
    open_items = queue.open_items()
    print(f"review queue: {len(open_items)} open item(s)")
    key = Ed25519PrivateKey.generate()
    for item in open_items:
        decision = queue.decide(
            item.item_id,
            action="accept_read",
            channel="export",
            reviewer="person:demo-clerk",
            signing_key=key,
            schema=SCHEMA,
            note="matches the report export",
        )
        print(
            f"  decided {item.field} = {decision.value} (signature ok: {queue.check(item.item_id, key.public_key())})"
        )
        for i, record in enumerate(records):
            if record.record_key == item.record_key:
                fields = tuple(
                    apply_decision(f, decision) if f.field == item.field else f
                    for f in record.fields
                )
                records[i] = record.model_copy(
                    update={"fields": tuple(f for f in fields if f)}
                )

    # 5. A write step that needs committed_record_readback halts below it.
    gate = GradeGate(min_grade="committed_record_readback", fields=("balance",))
    for record in records:
        try:
            gate.check(record)
            print(f"write gate: {record.record_key} may be written")
        except GradeTooLow as exc:
            print(f"write gate: halt: {exc}")

    # 6. Export.
    result = export(
        records, SCHEMA, CsvSink(out / "accounts.csv"), min_grade=args.min_grade
    )
    export(
        records,
        SCHEMA,
        DbApiSink(sqlite3.connect(out / "accounts.db"), "accounts"),
        min_grade=args.min_grade,
    )
    print(
        f"exported {len(result.rows)} record(s) to {out / 'accounts.csv'} and {out / 'accounts.db'}"
    )
    for row in result.rows:
        print(
            f"  {row['_record_key']}: {row['holder']}, {row['balance']}, grade {row['_grade']}"
        )
    for skipped in result.skipped:
        print(f"  skipped {skipped.record_key}: {skipped.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
