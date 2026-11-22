"""End-to-end tests for ``openadapt onboard`` on 2 unseen non-healthcare systems of record.

Fixtures (zero engine code references either app):
- Fixture A: ERP / Inventory SQLite + HTTP REST service (orders, SKUs, stock).
- Fixture B: ITSM / Ticketing SQLite + HTTP REST service (tickets, assignees, SLA comments).

Verifies on both unseen fixtures:
- 100% planted faults caught (wrong_value, null_vs_value, missing_write,
  duplicate_row, sibling_edit, wrong_target) -> 0% false receipts.
- 0% clean false stops, including novel composed field unions/subsets (#2490)
  and explicit ``None`` fields in SQL read filters (#2489).
- Signed ``record/run/v1`` DSSE receipts issued for every confirmed clean write.
- CLI ``openadapt-flow onboard`` / ``setup onboard`` parity and fail-closed refusal.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from openadapt_flow import __main__ as flow_cli
from openadapt_flow.onboard import (
    CleanWriteCase,
    KindOnboardingSpec,
    OnboardSpec,
    onboard_pack,
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.setenv("OPENADAPT_RECEIPT", "on")


def _http_json(url: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _assert_signed_receipt(receipt_path: str) -> None:
    p = Path(receipt_path)
    assert p.is_file(), f"missing receipt file: {receipt_path}"
    env = json.loads(p.read_text(encoding="utf-8"))
    assert "payload" in env and "signatures" in env and env["signatures"]
    stmt = json.loads(base64.b64decode(env["payload"]).decode("utf-8"))
    assert stmt.get("predicateType") == "https://openadapt.ai/record/run/v1"


# -- Fixture A: Unseen ERP / Inventory SQLite+REST app -----------------------


def _start_erp_fixture(tmp_path: Path) -> tuple[Path, str, ThreadingHTTPServer]:
    db = tmp_path / "erp_inventory.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE orders ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  order_code TEXT NOT NULL,"
        "  sku TEXT NOT NULL,"
        "  qty TEXT NOT NULL,"
        "  warehouse TEXT,"
        "  notes TEXT,"
        "  status TEXT NOT NULL DEFAULT 'pending',"
        "  updated_at TEXT NOT NULL DEFAULT '2026-11-01'"
        ");"
        "INSERT INTO orders (order_code, sku, qty, warehouse, notes, status, updated_at) "
        "VALUES ('ORD-100', 'SKU-BOLT-4', '25', 'WEST-1', 'initial stock', 'pending', '2026-11-01');"
        "INSERT INTO orders (order_code, sku, qty, warehouse, notes, status, updated_at) "
        "VALUES ('ORD-101', 'SKU-NUT-4', '50', 'EAST-2', 'sibling order', 'pending', '2026-11-01');"
    )
    con.commit()
    con.close()

    class ErpHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            c = sqlite3.connect(db)
            cur = c.execute(
                "INSERT INTO orders (order_code, sku, qty, warehouse, notes, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    data["order_code"],
                    data["sku"],
                    str(data["qty"]),
                    data.get("warehouse"),
                    data.get("notes"),
                    data.get("status", "pending"),
                ),
            )
            c.commit()
            oid = cur.lastrowid
            c.close()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": oid}).encode("utf-8"))

        def do_PATCH(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            oid = int(self.path.rstrip("/").rsplit("/", 1)[-1])
            updates = {
                k: v
                for k, v in data.items()
                if k in ("qty", "warehouse", "notes", "status")
            }
            updates["updated_at"] = "2026-11-22"
            sets = ", ".join(f"{k} = ?" for k in updates)
            c = sqlite3.connect(db)
            c.execute(
                f"UPDATE orders SET {sets} WHERE id = ?",
                [*updates.values(), oid],
            )
            c.commit()
            c.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": oid, "ok": True}).encode("utf-8"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), ErpHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return db, f"http://127.0.0.1:{port}", server


# -- Fixture B: Unseen ITSM / Ticketing SQLite+REST app ----------------------


def _start_itsm_fixture(tmp_path: Path) -> tuple[Path, str, ThreadingHTTPServer]:
    db = tmp_path / "itsm_tickets.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE tickets ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ticket_key TEXT NOT NULL,"
        "  title TEXT NOT NULL,"
        "  assignee TEXT,"
        "  priority TEXT,"
        "  sla_comment TEXT,"
        "  status TEXT NOT NULL DEFAULT 'open',"
        "  updated_at TEXT NOT NULL DEFAULT '2026-11-01'"
        ");"
        "INSERT INTO tickets (ticket_key, title, assignee, priority, sla_comment, status, updated_at) "
        "VALUES ('INC-500', 'VPN timeout', 'alice', 'P2', 'initial triage', 'open', '2026-11-01');"
        "INSERT INTO tickets (ticket_key, title, assignee, priority, sla_comment, status, updated_at) "
        "VALUES ('INC-501', 'Badge reader', 'bob', 'P3', 'sibling ticket', 'open', '2026-11-01');"
    )
    con.commit()
    con.close()

    class ItsmHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            c = sqlite3.connect(db)
            cur = c.execute(
                "INSERT INTO tickets (ticket_key, title, assignee, priority, sla_comment, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    data["ticket_key"],
                    data["title"],
                    data.get("assignee"),
                    data.get("priority"),
                    data.get("sla_comment"),
                    data.get("status", "open"),
                ),
            )
            c.commit()
            tid = cur.lastrowid
            c.close()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": tid}).encode("utf-8"))

        def do_PATCH(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            tid = int(self.path.rstrip("/").rsplit("/", 1)[-1])
            updates = {
                k: v
                for k, v in data.items()
                if k in ("assignee", "priority", "sla_comment", "status")
            }
            updates["updated_at"] = "2026-11-22"
            sets = ", ".join(f"{k} = ?" for k in updates)
            c = sqlite3.connect(db)
            c.execute(
                f"UPDATE tickets SET {sets} WHERE id = ?",
                [*updates.values(), tid],
            )
            c.commit()
            c.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": tid, "ok": True}).encode("utf-8"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), ItsmHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return db, f"http://127.0.0.1:{port}", server


def test_onboard_unseen_erp_inventory_fixture(tmp_path: Path) -> None:
    """Fixture A (ERP/Inventory): 100% planted faults caught, 0% clean false stops, signed receipts."""
    db, base_url, server = _start_erp_fixture(tmp_path)
    try:
        spec = OnboardSpec(
            name="erp-inventory",
            read={"sqlite_database": str(db)},
            kinds=(
                KindOnboardingSpec(
                    kind="create_order",
                    mode="create",
                    table="orders",
                    required_fields=("order_code", "sku", "qty"),
                    optional_fields=("warehouse", "notes"),
                    invariant_pins={"status": "pending"},
                    demonstrated_field_sets=(
                        ("order_code", "sku", "qty", "warehouse"),
                        ("order_code", "sku", "qty", "notes"),
                    ),
                ),
                KindOnboardingSpec(
                    kind="update_order",
                    mode="update",
                    table="orders",
                    identity_params=("id",),
                    change_fields=("qty", "warehouse", "notes", "status"),
                    bookkeeping_fields=("updated_at",),
                    demonstrated_field_sets=(
                        ("id", "qty"),
                        ("id", "warehouse"),
                        ("id", "notes"),
                        ("id", "status"),
                    ),
                ),
            ),
        )

        clean_cases = [
            CleanWriteCase(
                kind="create_order",
                name="exact_with_warehouse",
                values={
                    "order_code": "ORD-201",
                    "sku": "SKU-WASHER-8",
                    "qty": "100",
                    "warehouse": "NORTH-1",
                },
                apply_write=lambda: _http_json(
                    f"{base_url}/orders",
                    "POST",
                    {
                        "order_code": "ORD-201",
                        "sku": "SKU-WASHER-8",
                        "qty": "100",
                        "warehouse": "NORTH-1",
                    },
                ),
            ),
            CleanWriteCase(
                kind="create_order",
                name="novel_union_warehouse_and_notes",
                values={
                    "order_code": "ORD-202",
                    "sku": "SKU-GASKET-2",
                    "qty": "40",
                    "warehouse": "SOUTH-3",
                    "notes": "rush pallet",
                },
                apply_write=lambda: _http_json(
                    f"{base_url}/orders",
                    "POST",
                    {
                        "order_code": "ORD-202",
                        "sku": "SKU-GASKET-2",
                        "qty": "40",
                        "warehouse": "SOUTH-3",
                        "notes": "rush pallet",
                    },
                ),
            ),
            CleanWriteCase(
                kind="create_order",
                name="novel_subset_required_only",
                values={
                    "order_code": "ORD-203",
                    "sku": "SKU-PIN-1",
                    "qty": "12",
                },
                apply_write=lambda: _http_json(
                    f"{base_url}/orders",
                    "POST",
                    {
                        "order_code": "ORD-203",
                        "sku": "SKU-PIN-1",
                        "qty": "12",
                    },
                ),
            ),
            CleanWriteCase(
                kind="update_order",
                name="novel_union_multi_field_update",
                values={
                    "id": "1",
                    "qty": "30",
                    "warehouse": "CENTRAL-9",
                    "notes": "reallocated",
                },
                apply_write=lambda: _http_json(
                    f"{base_url}/orders/1",
                    "PATCH",
                    {
                        "qty": "30",
                        "warehouse": "CENTRAL-9",
                        "notes": "reallocated",
                    },
                ),
            ),
        ]

        result = onboard_pack(spec, tmp_path / "pack_erp", clean_cases)
        assert result.certified, result.refusal_reasons
        assert result.planted_faults_total >= 15
        assert result.planted_faults_caught == result.planted_faults_total
        assert result.clean_writes_total == 4
        assert result.clean_writes_verified == 4
        assert result.false_stop_rate == 0.0
        assert sum(1 for c in result.clean_outcomes if c.composed) == 3
        assert result.receipts_issued == 4
        for rp in result.receipt_paths:
            _assert_signed_receipt(rp)
        assert (tmp_path / "pack_erp" / "QUALIFICATION.json").is_file()
    finally:
        server.shutdown()
        server.server_close()


def test_onboard_unseen_itsm_ticketing_fixture(tmp_path: Path) -> None:
    """Fixture B (ITSM/Ticketing): 100% planted faults caught, 0% clean false stops,
    including novel unions (#2490), explicit None SQL filter binding (#2489), and signed receipts."""
    db, base_url, server = _start_itsm_fixture(tmp_path)
    try:
        spec = OnboardSpec(
            name="itsm-ticketing",
            read={"sqlite_database": str(db)},
            kinds=(
                KindOnboardingSpec(
                    kind="create_ticket",
                    mode="create",
                    table="tickets",
                    required_fields=("ticket_key", "title"),
                    optional_fields=("assignee", "priority", "sla_comment"),
                    invariant_pins={"status": "open"},
                    demonstrated_field_sets=(
                        ("ticket_key", "title", "assignee", "priority"),
                        ("ticket_key", "title", "assignee", "sla_comment"),
                        ("ticket_key", "title", "priority"),
                    ),
                ),
                KindOnboardingSpec(
                    kind="update_ticket",
                    mode="update",
                    table="tickets",
                    identity_params=("id",),
                    change_fields=("assignee", "priority", "sla_comment", "status"),
                    bookkeeping_fields=("updated_at",),
                    demonstrated_field_sets=(
                        ("id", "assignee"),
                        ("id", "priority"),
                        ("id", "sla_comment"),
                        ("id", "status"),
                    ),
                ),
            ),
        )

        clean_cases = [
            CleanWriteCase(
                kind="create_ticket",
                name="exact_with_priority",
                values={
                    "ticket_key": "INC-601",
                    "title": "SSO redirect loop",
                    "assignee": "carol",
                    "priority": "P1",
                },
                apply_write=lambda: _http_json(
                    f"{base_url}/tickets",
                    "POST",
                    {
                        "ticket_key": "INC-601",
                        "title": "SSO redirect loop",
                        "assignee": "carol",
                        "priority": "P1",
                    },
                ),
            ),
            CleanWriteCase(
                kind="create_ticket",
                name="novel_union_priority_and_sla",
                values={
                    "ticket_key": "INC-602",
                    "title": "Printer offline",
                    "assignee": "dave",
                    "priority": "P3",
                    "sla_comment": "4h response target",
                },
                apply_write=lambda: _http_json(
                    f"{base_url}/tickets",
                    "POST",
                    {
                        "ticket_key": "INC-602",
                        "title": "Printer offline",
                        "assignee": "dave",
                        "priority": "P3",
                        "sla_comment": "4h response target",
                    },
                ),
            ),
            CleanWriteCase(
                kind="create_ticket",
                name="unassigned_null_assignee",
                values={
                    "ticket_key": "INC-603",
                    "title": "Unassigned intake",
                    "assignee": None,
                    "priority": "P2",
                },
                apply_write=lambda: _http_json(
                    f"{base_url}/tickets",
                    "POST",
                    {
                        "ticket_key": "INC-603",
                        "title": "Unassigned intake",
                        "assignee": None,
                        "priority": "P2",
                    },
                ),
            ),
            CleanWriteCase(
                kind="update_ticket",
                name="novel_union_multi_field_update",
                values={
                    "id": "1",
                    "priority": "P1",
                    "sla_comment": "escalated to L2",
                    "status": "in_progress",
                },
                apply_write=lambda: _http_json(
                    f"{base_url}/tickets/1",
                    "PATCH",
                    {
                        "priority": "P1",
                        "sla_comment": "escalated to L2",
                        "status": "in_progress",
                    },
                ),
            ),
        ]

        result = onboard_pack(spec, tmp_path / "pack_itsm", clean_cases)
        assert result.certified, result.refusal_reasons
        assert result.planted_faults_total >= 15
        assert result.planted_faults_caught == result.planted_faults_total
        assert result.clean_writes_total == 4
        assert result.clean_writes_verified == 4
        assert result.false_stop_rate == 0.0
        assert sum(1 for c in result.clean_outcomes if c.composed) == 2
        assert result.receipts_issued == 4
        for rp in result.receipt_paths:
            _assert_signed_receipt(rp)

        # Also verify #2489 null-safe SQL filter binding (WHERE t.assignee = :assignee
        # rewritten to IS NULL) directly against the live ITSM REST+SQLite fixture.
        from openadapt_flow.verifier import (
            Actor,
            Contract,
            MemoryLedger,
            Outcome,
            begin_write,
            verify_write,
        )

        null_filter_contract = Contract.from_dict(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": (
                        "SELECT id, ticket_key, title, assignee, priority "
                        "FROM tickets WHERE ticket_key = :ticket_key AND assignee = :assignee"
                    ),
                    "sql_query_params": {
                        "ticket_key": {"param": "ticket_key"},
                        "assignee": {"param": "assignee"},
                    },
                },
                "effects": [
                    {
                        "kind": "record_written",
                        "match": {
                            "ticket_key": {"param": "ticket_key"},
                            "title": {"param": "title"},
                            "assignee": {"param": "assignee"},
                        },
                        "expected_count": 1,
                        "count_new_only": True,
                    }
                ],
                "frame": {"identity_field": "id"},
            }
        )
        pending_null = begin_write(
            null_filter_contract,
            {"ticket_key": "INC-604", "title": "Null SQL filter", "assignee": None},
            actor=Actor(kind="agent", id="bot"),
            ledger=MemoryLedger(),
            workdir=tmp_path / "null_runs",
        )
        _http_json(
            f"{base_url}/tickets",
            "POST",
            {
                "ticket_key": "INC-604",
                "title": "Null SQL filter",
                "assignee": None,
                "priority": "P2",
            },
        )
        res_null = verify_write(pending_null)
        assert res_null.outcome is Outcome.CONFIRMED, res_null.reasons
    finally:
        server.shutdown()
        server.server_close()


def test_onboard_refuses_certification_when_db_unreadable(tmp_path: Path) -> None:
    spec = OnboardSpec(
        name="missing-db",
        read={"sqlite_database": str(tmp_path / "does_not_exist.sqlite")},
        kinds=(
            KindOnboardingSpec(
                kind="create_item",
                mode="create",
                table="items",
                required_fields=("name",),
                demonstrated_field_sets=(("name",),),
            ),
        ),
    )
    res = onboard_pack(spec, tmp_path / "out")
    assert not res.certified
    assert res.refusal_reasons


def test_onboard_refuses_certification_when_required_fault_class_unplanted(
    tmp_path: Path,
) -> None:
    """Blocker 2: an empty table cannot plant sibling_edit, so certification MUST refuse."""
    db = tmp_path / "empty.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)"
    )
    con.commit()
    con.close()

    spec = OnboardSpec(
        name="empty-table-pack",
        read={"sqlite_database": str(db)},
        kinds=(
            KindOnboardingSpec(
                kind="create_item",
                mode="create",
                table="items",
                required_fields=("name",),
                demonstrated_field_sets=(("name",),),
            ),
        ),
    )

    def _insert_clean() -> None:
        c = sqlite3.connect(db)
        c.execute("INSERT INTO items (name) VALUES (?)", ("Widget",))
        c.commit()
        c.close()

    res = onboard_pack(
        spec,
        tmp_path / "out_empty",
        [
            CleanWriteCase(
                kind="create_item",
                values={"name": "Widget"},
                apply_write=_insert_clean,
            )
        ],
    )
    assert not res.certified
    assert any(
        "missing required planted fault class" in r and "sibling_edit" in r
        for r in res.refusal_reasons
    ), res.refusal_reasons


def test_onboard_cli_end_to_end_and_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db, base_url, server = _start_erp_fixture(tmp_path)
    try:
        spec_path = tmp_path / "erp_onboard.yaml"
        spec_doc = {
            "name": "erp-cli-pack",
            "read": {"sqlite_database": str(db)},
            "kinds": [
                {
                    "kind": "create_order",
                    "mode": "create",
                    "table": "orders",
                    "required_fields": ["order_code", "sku", "qty"],
                    "optional_fields": ["warehouse"],
                    "invariant_pins": {"status": "pending"},
                    "demonstrated_field_sets": [
                        ["order_code", "sku", "qty", "warehouse"]
                    ],
                    "sample_values": {
                        "order_code": "ORD-900",
                        "sku": "SKU-CLI-1",
                        "qty": "5",
                        "warehouse": "WEST-1",
                    },
                }
            ],
        }
        spec_path.write_text(
            yaml.safe_dump(spec_doc, sort_keys=False), encoding="utf-8"
        )

        # Dry-run synthesizes pack without certifying.
        rc_dry = flow_cli.main(
            [
                "onboard",
                "--spec",
                str(spec_path),
                "--out",
                str(tmp_path / "cli_dry"),
                "--dry-run",
                "--json",
            ]
        )
        assert rc_dry == 0
        dry_out = json.loads(capsys.readouterr().out)
        assert dry_out["dry_run"] is True
        assert dry_out["certified"] is False

        # Blocker 1: CLI without clean writes refuses vacuous certification (exit 2).
        rc_no_clean = flow_cli.main(
            [
                "onboard",
                "--spec",
                str(spec_path),
                "--out",
                str(tmp_path / "cli_no_clean"),
                "--json",
            ]
        )
        assert rc_no_clean == 2
        no_clean_out = json.loads(capsys.readouterr().out)
        assert no_clean_out["certified"] is False
        assert no_clean_out["clean_writes_total"] == 0
        assert any("no clean writes" in r for r in no_clean_out["refusal_reasons"])

        # Full CLI run via task route ``setup onboard`` WITH --clean-writes certifies
        # and issues a signed record/run/v1 receipt.
        clean_path = tmp_path / "erp_clean_writes.yaml"
        clean_doc = [
            {
                "kind": "create_order",
                "name": "cli_live_order",
                "values": {
                    "order_code": "ORD-901",
                    "sku": "SKU-CLI-2",
                    "qty": "7",
                    "warehouse": "WEST-1",
                },
                "http": {
                    "url": f"{base_url}/orders",
                    "method": "POST",
                    "body": {
                        "order_code": "ORD-901",
                        "sku": "SKU-CLI-2",
                        "qty": "7",
                        "warehouse": "WEST-1",
                    },
                },
            }
        ]
        clean_path.write_text(
            yaml.safe_dump(clean_doc, sort_keys=False), encoding="utf-8"
        )

        rc = flow_cli.main(
            [
                "setup",
                "onboard",
                "--spec",
                str(spec_path),
                "--clean-writes",
                str(clean_path),
                "--out",
                str(tmp_path / "cli_full"),
                "--json",
            ]
        )
        assert rc == 0
        full_out = json.loads(capsys.readouterr().out)
        assert full_out["certified"] is True
        assert full_out["planted_faults_total"] >= 10
        assert full_out["planted_faults_caught"] == full_out["planted_faults_total"]
        assert full_out["clean_writes_total"] == 1
        assert full_out["clean_writes_verified"] == 1
        assert full_out["receipts_issued"] == 1
        _assert_signed_receipt(full_out["receipt_paths"][0])
    finally:
        server.shutdown()
        server.server_close()
