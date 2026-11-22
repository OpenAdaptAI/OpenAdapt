"""``openadapt plan``: dry-run diff, idempotent retry, verified undo, receipts.

Real SQLite files, and real HTTP and SMTP servers on loopback
(``txplan_servers``). The ACRFence tests reproduce the semantic-rollback
action-replay attack (arXiv:2603.20625): after a restore, the agent
regenerates the same write with a different payload. The control arm shows
the duplicate lands without the guard; the guarded arm shows it is refused.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

pytest.importorskip("cryptography")

from openadapt_flow.txplan import engine  # noqa: E402
from openadapt_flow.txplan.adapters import RestAdapter  # noqa: E402
from openadapt_flow.txplan.engine import Ctx, TxRefused  # noqa: E402
from openadapt_flow.txplan.spec import load_spec  # noqa: E402
from openadapt_flow.txplan.state import State  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from txplan_servers import MailSink, RestStore  # noqa: E402

SECRET_NOTE = "synthetic-note-7c1f"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.setenv("OPENADAPT_TX_STATE", str(tmp_path / "state"))
    monkeypatch.delenv(engine.CRASH_ENV, raising=False)


def _db(path: Path, *, trigger: bool = False) -> Path:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, balance REAL, note TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer INTEGER, total REAL);
        CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, customer INTEGER);
        """
    )
    for i in range(1, 21):
        con.execute(
            "INSERT INTO customers VALUES (?, ?, ?, ?)", (i, f"cust {i}", 10.0 * i, "")
        )
        con.execute("INSERT INTO orders VALUES (?, ?, ?)", (i, i, 5.0 * i))
    if trigger:
        con.execute(
            "CREATE TRIGGER t AFTER UPDATE ON customers BEGIN "
            "INSERT INTO audit (customer) VALUES (NEW.id); END"
        )
    con.commit()
    con.close()
    return path


def _rows(path: Path) -> dict[str, list[tuple]]:
    con = sqlite3.connect(path)
    try:
        return {
            t: sorted(con.execute(f"SELECT * FROM {t}").fetchall())
            for t in ("customers", "orders", "audit")
        }
    finally:
        con.close()


@pytest.fixture
def rest() -> Iterator[tuple[RestStore, str]]:
    store = RestStore(replace=True)
    for i in range(1, 11):
        store.add(title=f"item {i}", amount=i * 10)
    srv, url = store.server()
    try:
        yield store, url
    finally:
        srv.shutdown()


@pytest.fixture
def mail() -> Iterator[MailSink]:
    sink = MailSink()
    try:
        yield sink
    finally:
        sink.close()


#: The evidence a pack records after probing a server's If-Match handling.
_PROBED = {"status": 412, "observed": "2026-11-22", "server": "RestStore"}


def _spec(
    tmp_path: Path,
    *,
    db: Path | None = None,
    rest_url: str | None = None,
    mail: MailSink | None = None,
    holdback: float = 60,
    precondition: Any = True,
) -> Path:
    systems: list[dict[str, Any]] = []
    if db is not None:
        systems.append(
            {
                "name": "crm",
                "kind": "sql",
                "dialect": "sqlite",
                "path": str(db),
                "tables": {"customers": "id", "orders": "id"},
            }
        )
    if rest_url is not None:
        # The store's ETag: the write is refused if the record moved since read.
        # A recorded probe: the store refuses a mismatched If-Match with 412.
        # A dict is the precondition as given (``{}``: declared, unproven).
        cond: dict[str, Any] = (
            {"precondition": precondition}
            if isinstance(precondition, dict)
            else ({"precondition": {"enforcement": _PROBED}} if precondition else {})
        )
        systems.append(
            {
                "name": "tasks",
                "kind": "rest",
                "base_url": rest_url,
                "collections": [
                    {
                        "name": "item",
                        "id_field": "id",
                        "list": {
                            "path": "/api/items",
                            "page_param": "page",
                            "per_page": 4,
                        },
                        "read": {"path": "/api/items/{id}"},
                        "create": {"method": "POST", "path": "/api/items"},
                        "update": {
                            "method": "PATCH",
                            "path": "/api/items/{id}",
                            "replace": True,
                            **cond,
                        },
                        "delete": {
                            "method": "DELETE",
                            "path": "/api/items/{id}",
                            **cond,
                        },
                        "server_fields": ["updated"],
                        "derived": {"done": ["done_at"]},
                    }
                ],
            }
        )
    if mail is not None:
        systems.append(
            {
                "name": "outbox",
                "kind": "mail",
                "smtp": {"host": "127.0.0.1", "port": mail.smtp_port},
                "mailbox": {"kind": "mailpit", "base_url": mail.api_url},
            }
        )
    path = tmp_path / "openadapt.tx.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "openadapt.tx/v1",
                "holdback_s": holdback,
                "anomalies": [
                    {"field": "amount", "above": 10000, "label": "large refund"},
                    {"op": "delete", "label": "delete"},
                ],
                "systems": systems,
            }
        )
    )
    return path


def _ctx(spec: Path) -> Ctx:
    return Ctx(spec=load_spec(spec), state=State())


def _go(ctx: Ctx, task: str, writes: list[dict[str, Any]]) -> dict[str, Any]:
    plan = engine.make_plan(ctx, task, writes)
    engine.approve_plan(
        ctx.state, plan["digest"], approver="lab", decided_by="automation"
    )
    return plan


# -- dry run, apply, undo: SQL (staged transaction) ----------------------------


def test_sql_plan_apply_undo_round_trip(tmp_path: Path) -> None:
    db = _db(tmp_path / "crm.sqlite")
    before = _rows(db)
    ctx = _ctx(_spec(tmp_path, db=db))
    writes = [
        {
            "system": "crm",
            "collection": "customers",
            "op": "update",
            "id": 3,
            "fields": {"balance": 99.5, "note": SECRET_NOTE},
        },
        {
            "system": "crm",
            "collection": "customers",
            "op": "create",
            "fields": {"name": "new cust", "balance": 1.0, "note": ""},
        },
        {"system": "crm", "collection": "orders", "op": "delete", "id": 7},
    ]
    plan = _go(ctx, "T-sql", writes)
    pred = plan["predicted"]["crm:*"]
    assert plan["dry_run"] == {"crm": "staged_transaction"}
    assert pred["changed"] == {"customers/3": ["balance", "note"]}
    assert pred["added"] == {"customers": 1} and pred["removed"] == ["orders/7"]
    assert _rows(db) == before  # the staged transaction rolled back
    lines = engine.summary_lines(plan)
    assert any("1 anomaly" in x for x in lines) and any("delete" in x for x in lines)

    applied = engine.apply_plan(ctx, plan["digest"])
    assert applied["outcome"] == "VERIFIED", applied
    assert applied["tx"]["missed_effects"] == 0 and applied["tx"]["false_diffs"] == 0
    assert _rows(db) != before

    undone = engine.undo_plan(ctx, plan["digest"])
    assert undone["outcome"] == "ROLLED_BACK", undone
    assert (
        undone["tx"]["undo_verified"] == 3 and undone["tx"]["collateral_changes"] == 0
    )
    assert _rows(db) == before


def test_staged_dry_run_sees_a_trigger_and_undo_reports_its_collateral(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path / "crm.sqlite", trigger=True)
    ctx = _ctx(_spec(tmp_path, db=db))
    plan = _go(
        ctx,
        "T-trig",
        [
            {
                "system": "crm",
                "collection": "customers",
                "op": "update",
                "id": 2,
                "fields": {"balance": 1.0},
            }
        ],
    )
    assert plan["predicted"]["crm:*"]["added"] == {"audit": 1}
    assert any(
        a["label"] == "side effects in undeclared tables" for a in plan["anomalies"]
    )
    applied = engine.apply_plan(ctx, plan["digest"])
    assert applied["tx"]["missed_effects"] == 0, applied
    undone = engine.undo_plan(ctx, plan["digest"])
    # The undo's own UPDATE fires the trigger again: not back to the baseline.
    assert undone["outcome"] == "RECONCILIATION_REQUIRED"
    assert (
        undone["tx"]["undo_verified"] == 0 and undone["tx"]["collateral_changes"] >= 1
    )


def test_missed_effect_is_counted_when_something_else_moves(
    tmp_path: Path, rest
) -> None:
    store, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url))
    plan = _go(
        ctx,
        "T-miss",
        [
            {
                "system": "tasks",
                "collection": "item",
                "op": "update",
                "id": 1,
                "fields": {"title": "renamed"},
            }
        ],
    )
    real = RestAdapter.apply

    def apply_and_touch(
        self, w, base, key
    ):  # a server-side cascade the plan didn't see
        out = real(self, w, base, key)
        store.records[5]["title"] = "cascaded"
        return out

    RestAdapter.apply = apply_and_touch  # type: ignore[method-assign]
    try:
        applied = engine.apply_plan(ctx, plan["digest"])
    finally:
        RestAdapter.apply = real  # type: ignore[method-assign]
    assert (
        applied["tx"]["missed_effects"] == 1
        and applied["outcome"] == "RECONCILIATION_REQUIRED"
    )


# -- approval binding ----------------------------------------------------------


def test_approval_binds_to_one_digest_and_is_single_use(tmp_path: Path) -> None:
    db = _db(tmp_path / "crm.sqlite")
    ctx = _ctx(_spec(tmp_path, db=db))
    w = [
        {
            "system": "crm",
            "collection": "customers",
            "op": "update",
            "id": 1,
            "fields": {"balance": 7},
        }
    ]
    a = engine.make_plan(ctx, "T-a", w)
    b = engine.make_plan(ctx, "T-b", w)
    engine.approve_plan(ctx.state, a["digest"], approver="lab", decided_by="automation")
    with pytest.raises(TxRefused) as exc:
        engine.apply_plan(ctx, b["digest"])
    assert exc.value.code == "not_approved"
    assert engine.apply_plan(ctx, a["digest"])["outcome"] == "VERIFIED"
    with pytest.raises(TxRefused):  # authority resurrection: no second apply
        engine.apply_plan(ctx, a["digest"])


def test_stale_plan_is_refused_before_any_send(tmp_path: Path) -> None:
    db = _db(tmp_path / "crm.sqlite")
    ctx = _ctx(_spec(tmp_path, db=db))
    plan = _go(
        ctx,
        "T-stale",
        [
            {
                "system": "crm",
                "collection": "customers",
                "op": "update",
                "id": 4,
                "fields": {"balance": 0},
            }
        ],
    )
    con = sqlite3.connect(db)
    con.execute("UPDATE orders SET total = 1 WHERE id = 9")
    con.commit()
    con.close()
    snapshot = _rows(db)
    with pytest.raises(TxRefused) as exc:
        engine.apply_plan(ctx, plan["digest"])
    assert exc.value.code == "plan_stale"
    assert _rows(db) == snapshot
    assert (
        ctx.state.approval_record(plan["digest"])["plan"] == plan["digest"]
    )  # not consumed


# -- ACRFence: semantic rollback, action replay -------------------------------


def _payment(ref: str, amount: int = 100) -> dict[str, Any]:
    return {
        "system": "tasks",
        "collection": "item",
        "op": "create",
        "fields": {"title": f"refund {ref}", "amount": amount, "reference": ref},
    }


def test_acrfence_control_arm_without_the_guard_double_charges(
    tmp_path: Path, rest
) -> None:
    store, url = rest
    spec = load_spec(_spec(tmp_path, rest_url=url))
    raw = RestAdapter(spec.system("tasks"))  # type: ignore[arg-type]
    n0 = len(store.records)
    # A request-id idempotency check (the paper's UUID testbed) sees two requests.
    raw.apply(_payment("uuid-a"), None, "req-uuid-a")
    raw.apply(_payment("uuid-b"), None, "req-uuid-b")  # regenerated after a restore
    assert len(store.records) == n0 + 2


def test_acrfence_regenerated_payload_after_restore_is_blocked(
    tmp_path: Path, rest
) -> None:
    store, url = rest
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = _ctx(_spec(tmp_path, rest_url=url))
    first = _go(ctx, "T-refund", [_payment("uuid-a")])
    assert engine.apply_plan(ctx, first["digest"])["outcome"] == "VERIFIED"
    n1 = len(store.records)
    writes_before = store.writes

    # Checkpoint restore: the agent's workspace goes back to before the apply.
    shutil.rmtree(workspace)
    workspace.mkdir()
    # The agent regenerates the batch: same task, same intent, new reference.
    ctx = _ctx(_spec(tmp_path, rest_url=url))
    again = engine.make_plan(ctx, "T-refund", [_payment("uuid-b")])
    assert again["writes"][0]["ledger"] == "replay_mismatch"
    assert any("different payload" in a["label"] for a in again["anomalies"])
    engine.approve_plan(
        ctx.state, again["digest"], approver="lab", decided_by="automation"
    )
    with pytest.raises(TxRefused) as exc:
        engine.apply_plan(ctx, again["digest"])
    assert exc.value.code == "replay_mismatch"
    assert exc.value.report["tx"]["blocked_double_effects"] == 1
    assert len(store.records) == n1 and store.writes == writes_before

    # The identical payload is a replay: the stored result, nothing sent.
    same = _go(ctx, "T-refund", [_payment("uuid-a")])
    out = engine.apply_plan(ctx, same["digest"])
    assert out["tx"]["replays"] == 1 and out["tx"]["sent"] == 0
    assert len(store.records) == n1


def test_acrfence_mutated_update_replay_is_blocked(tmp_path: Path, rest) -> None:
    store, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url))
    p = _go(
        ctx,
        "T-upd",
        [
            {
                "system": "tasks",
                "collection": "item",
                "op": "update",
                "id": 2,
                "fields": {"amount": 500},
            }
        ],
    )
    engine.apply_plan(ctx, p["digest"])
    q = _go(
        ctx,
        "T-upd",
        [
            {
                "system": "tasks",
                "collection": "item",
                "op": "update",
                "id": 2,
                "fields": {"amount": 5000},
            }
        ],
    )
    with pytest.raises(TxRefused) as exc:
        engine.apply_plan(ctx, q["digest"])
    assert exc.value.code == "replay_mismatch"
    assert store.records[2]["amount"] == 500


def test_crash_after_send_then_regenerated_retry_makes_no_second_effect(
    tmp_path: Path, rest
) -> None:
    store, url = rest
    spec_path = _spec(tmp_path, rest_url=url)
    ctx = _ctx(spec_path)
    plan = _go(ctx, "T-crash", [_payment("uuid-c")])
    n0 = len(store.records)
    code = (
        "import sys; from pathlib import Path; from openadapt_flow.txplan import engine;"
        "from openadapt_flow.txplan.spec import load_spec; from openadapt_flow.txplan.state import State;"
        f"engine.apply_plan(engine.Ctx(load_spec(Path({str(spec_path)!r})), State()), {plan['digest']!r})"
    )
    env = {**os.environ, engine.CRASH_ENV: "1"}
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, timeout=120
    )
    assert proc.returncode == 70, proc.stderr.decode()[-2000:]
    assert (
        len(store.records) == n0 + 1
    )  # the send landed; its outcome was never recorded
    key = plan["writes"][0]["key"]
    assert ctx.state.entry(key).status == "begun"

    mutated = _go(ctx, "T-crash", [_payment("uuid-d")])
    with pytest.raises(TxRefused):
        engine.apply_plan(ctx, mutated["digest"])
    same = _go(ctx, "T-crash", [_payment("uuid-c")])
    out = engine.apply_plan(ctx, same["digest"])
    assert (
        out["tx"]["held_uncertain"] == 1 and out["tx"]["sent"] == 0
    )  # read back, never resent
    assert len(store.records) == n0 + 1


# -- REST: computed dry run, replace-style update, derived fields --------------


def test_rest_replace_update_with_derived_field_round_trip(
    tmp_path: Path, rest
) -> None:
    store, url = rest
    snapshot = {
        k: {f: v for f, v in r.items() if f != "updated"}
        for k, r in store.records.items()
    }
    ctx = _ctx(_spec(tmp_path, rest_url=url))
    plan = _go(
        ctx,
        "T-rest",
        [
            {
                "system": "tasks",
                "collection": "item",
                "op": "update",
                "id": 3,
                "fields": {"done": True},
            },
            {
                "system": "tasks",
                "collection": "item",
                "op": "create",
                "fields": {"title": "x", "amount": 1},
            },
        ],
    )
    pred = plan["predicted"]["tasks:item"]
    assert pred["changed"] == {"item/3": ["done"]} and pred["derived"] == {
        "item/3": ["done_at"]
    }
    applied = engine.apply_plan(ctx, plan["digest"])
    assert applied["outcome"] == "VERIFIED", applied
    assert (
        store.records[3]["title"] == "item 3"
    )  # replace-style: the baseline was merged
    undone = engine.undo_plan(ctx, plan["digest"])
    assert undone["outcome"] == "ROLLED_BACK", undone
    now = {
        k: {f: v for f, v in r.items() if f != "updated"}
        for k, r in store.records.items()
    }
    assert now == snapshot


def test_undo_refuses_a_record_changed_since_apply(tmp_path: Path, rest) -> None:
    store, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url))
    plan = _go(
        ctx,
        "T-chg",
        [
            {
                "system": "tasks",
                "collection": "item",
                "op": "update",
                "id": 4,
                "fields": {"amount": 1},
            }
        ],
    )
    engine.apply_plan(ctx, plan["digest"])
    store.records[4]["amount"] = 2  # a person edited it after the apply
    undone = engine.undo_plan(ctx, plan["digest"])
    assert undone["tx"]["codes"] == {"changed_since_apply": 1}
    assert store.records[4]["amount"] == 2


def test_large_amount_anomaly_is_shown_in_the_terminal_only(
    tmp_path: Path, rest
) -> None:
    _, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url))
    plan = engine.make_plan(ctx, "T-big", [_payment("r", amount=42_000)])
    text = "\n".join(engine.summary_lines(plan))
    assert "large refund: amount = 42000" in text
    report = (Path(plan["run_dir"]) / "report.json").read_text()
    assert "42000" not in report and "refund r" not in report


# -- email: holdback queue -----------------------------------------------------


def _mail(to: str = "a@example.test") -> dict[str, Any]:
    return {
        "system": "outbox",
        "op": "send",
        "fields": {"to": to, "subject": "Invoice", "body": SECRET_NOTE},
    }


def test_email_waits_in_holdback_and_cancel_sends_nothing(
    tmp_path: Path, mail: MailSink
) -> None:
    ctx = _ctx(_spec(tmp_path, mail=mail, holdback=3600))
    plan = _go(ctx, "T-mail", [_mail(), _mail("b@example.test")])
    applied = engine.apply_plan(ctx, plan["digest"])
    assert applied["tx"]["held"] == 2 and applied["outcome"] == "COMPLETED_UNVERIFIED"
    assert engine.release(ctx, plan["digest"])["tx"]["waiting"] == 2
    undone = engine.undo_plan(ctx, plan["digest"])
    assert undone["tx"]["cancelled"] == 2 and mail.messages == []


def test_email_release_sends_once_even_after_a_crash(
    tmp_path: Path, mail: MailSink
) -> None:
    ctx = _ctx(_spec(tmp_path, mail=mail, holdback=0))
    plan = _go(ctx, "T-rel", [_mail()])
    engine.apply_plan(ctx, plan["digest"])
    key = plan["writes"][0]["key"]
    # A release that sent and crashed before recording it: the message is there.
    from openadapt_flow.txplan.adapters import MailAdapter

    adapter = ctx.adapter("outbox")
    assert isinstance(adapter, MailAdapter)
    w = json.loads((ctx.state.plan_dir(plan["digest"]) / "private.json").read_text())[
        "writes"
    ][0]
    adapter.send(w, key)
    out = engine.release(ctx, plan["digest"])
    assert out["tx"] == {
        "phase": "release",
        "released": 1,
        "waiting": 0,
        "adopted": 1,
        "double": 0,
    }
    assert len(mail.messages) == 1 and out["outcome"] == "VERIFIED"
    undone = engine.undo_plan(ctx, plan["digest"])
    assert undone["tx"]["irreversible_released"] == 1
    # A sent email can't be taken back: the plan is not rolled back.
    assert undone["outcome"] == "RECONCILIATION_REQUIRED"


# -- receipts ------------------------------------------------------------------


def test_each_phase_gets_a_signed_receipt_with_the_approval_bound(
    tmp_path: Path,
) -> None:
    from openadapt_flow.receipt_record import local_registry, verify_run_receipt

    db = _db(tmp_path / "crm.sqlite")
    ctx = _ctx(_spec(tmp_path, db=db))
    plan = _go(
        ctx,
        "T-rcpt",
        [
            {
                "system": "crm",
                "collection": "customers",
                "op": "update",
                "id": 5,
                "fields": {"note": SECRET_NOTE},
            }
        ],
    )
    applied = engine.apply_plan(ctx, plan["digest"])
    undone = engine.undo_plan(ctx, plan["digest"])
    registry = local_registry(Path(os.environ["OPENADAPT_SEAL_HOME"]))
    for run_dir, outcome in (
        (plan["run_dir"], "UNKNOWN"),
        (applied["run_dir"], "VERIFIED"),
        (undone["run_dir"], "STOPPED"),
    ):
        run = Path(run_dir)
        got = verify_run_receipt(run, registry)
        assert got.ok, (run, got)
        assert got.statement["predicate"]["outcome"] == outcome
        for name in ("report.json", "receipt.json"):
            assert SECRET_NOTE not in (run / name).read_text()
        report = json.loads((run / "report.json").read_text())
        assert report["plan_sha256"] == plan["digest"]
    stmt = verify_run_receipt(Path(applied["run_dir"]), registry).statement
    assert stmt["predicate"]["approval"]["mode"] == "each_item"
    assert stmt["predicate"]["approval"]["approver_kind"] == "agent"
    assert stmt["predicate"]["approval"]["commitments"]
    plan_stmt = verify_run_receipt(Path(plan["run_dir"]), registry).statement
    assert plan_stmt["predicate"]["approval"]["mode"] == "each_item"
    assert plan_stmt["predicate"]["approval"]["approver_kind"] == "none"
    assert plan_stmt["predicate"]["approval"]["commitments"] == {}


def test_cli_create_show_approve_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from openadapt_flow.txplan.cli import main

    db = _db(tmp_path / "crm.sqlite")
    spec = _spec(tmp_path, db=db)
    writes = tmp_path / "w.json"
    writes.write_text(
        json.dumps(
            {
                "writes": [
                    {
                        "system": "crm",
                        "collection": "customers",
                        "op": "update",
                        "id": 6,
                        "fields": {"balance": 3},
                    }
                ]
            }
        )
    )
    assert main(["create", "--tx", str(spec), "--task", "T-cli", str(writes)]) == 0
    out = capsys.readouterr().out
    assert "1 crm.customers updated" in out
    digest = out.split("openadapt plan approve ")[1].split()[0]
    assert main(["approve", digest]) == 2  # not a terminal: a person must approve
    assert main(["approve", digest, "--automation"]) == 0
    assert main(["apply", "--tx", str(spec), digest]) == 0
    assert main(["apply", "--tx", str(spec), digest]) == 3


# -- raw SQL statements and the credential-holding proxy ------------------------


def _users(path: Path, n: int = 500) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, role TEXT, active INTEGER)"
    )
    con.execute("CREATE TABLE logins (id INTEGER PRIMARY KEY, user_id INTEGER)")
    for i in range(1, n + 1):
        con.execute(
            "INSERT INTO users VALUES (?, ?, ?, 1)",
            (i, f"user {i}", "admin" if i in (3, 30, 300) else "member"),
        )
    con.commit()
    con.close()
    return path


def _proxy(db: Path, **kw: Any):  # type: ignore[no-untyped-def]
    from openadapt_flow.txplan.proxy import Proxy, sql_spec

    spec = sql_spec(sqlite=str(db), flags=["users.role=admin"], **kw)
    return Proxy(Ctx(spec=spec, state=State()), task="T-proxy")


def _all(db: Path) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return sorted(con.execute("SELECT * FROM users").fetchall())
    finally:
        con.close()


def test_proxy_bulk_update_shows_exact_diff_then_applies_once_and_undoes(
    tmp_path: Path,
) -> None:
    db = _users(tmp_path / "app.sqlite")
    before = _all(db)
    proxy = _proxy(db)
    got = proxy.preview("UPDATE users SET active = 0 WHERE id <= 412")
    assert got["tables"] == {"users": {"added": 0, "changed": 412, "removed": 0}}
    assert "rows where users.role = admin" in got["anomalies"]
    assert (
        "users: 412 rows changed" in got["summary"]
        and "= admin: users.role = 3" in got["summary"]
    )
    assert got["approval"].startswith("waiting for a person") and _all(db) == before
    with pytest.raises(TxRefused):
        proxy.apply(got["plan"])
    assert proxy.key("y").startswith("approved")
    out = proxy.apply(got["plan"])
    assert out["outcome"] == "VERIFIED" and out["tx"]["missed_effects"] == 0
    con = sqlite3.connect(db)
    assert (
        con.execute("SELECT count(*) FROM users WHERE active = 0").fetchone()[0] == 412
    )
    con.close()
    undone = proxy.undo(got["plan"])
    assert undone["outcome"] == "ROLLED_BACK", undone
    assert _all(db) == before


def test_proxy_insert_and_delete_round_trip_and_policy_auto_approve(
    tmp_path: Path,
) -> None:
    db = _users(tmp_path / "app.sqlite", n=20)
    before = _all(db)
    proxy = _proxy(db, max_rows=5)
    a = proxy.preview(
        "INSERT INTO users (name, role, active) VALUES ('new', 'member', 1)"
    )
    assert a["approval"] == "approved by policy"
    assert proxy.apply(a["plan"])["outcome"] == "VERIFIED"
    b = proxy.preview("DELETE FROM users WHERE id IN (4, 5)")
    assert b["approval"] == "approved by policy"
    assert proxy.apply(b["plan"])["outcome"] == "VERIFIED"
    assert proxy.undo(b["plan"])["outcome"] == "ROLLED_BACK"
    assert proxy.undo(a["plan"])["outcome"] == "ROLLED_BACK"
    assert _all(db) == before


def test_sql_apply_refuses_a_plan_without_its_planned_values(tmp_path: Path) -> None:
    from openadapt_flow.txplan.state import read_json, write_private

    db = _users(tmp_path / "app.sqlite", n=20)
    before = _all(db)
    proxy = _proxy(db, max_rows=100)
    got = proxy.preview("UPDATE users SET role = 'viewer' WHERE id = 4")
    state = proxy.ctx.state
    private = state.plan_dir(state.find_plan(got["plan"])) / "private.json"
    data = read_json(private)
    for w in data["writes"]:
        w.pop("staged_after", None)
    write_private(private, data)
    with pytest.raises(TxRefused) as exc:
        proxy.apply(got["plan"])
    assert exc.value.code == "plan_tampered" and _all(db) == before


def test_transaction_control_is_never_a_write(tmp_path: Path) -> None:
    db = _users(tmp_path / "app.sqlite", n=5)
    proxy = _proxy(db, max_rows=100)
    for sql in (
        "COMMIT",
        "end",
        "BEGIN",
        "SAVEPOINT a",
        "ROLLBACK",
        "PRAGMA foreign_keys = OFF",
    ):
        with pytest.raises(TxRefused) as exc:
            proxy.preview(sql)
        assert exc.value.code == "bad_write", sql


def test_a_key_press_with_several_plans_waiting_approves_none(tmp_path: Path) -> None:
    db = _users(tmp_path / "app.sqlite", n=50)
    proxy = _proxy(db, max_rows=1)
    a = proxy.preview("UPDATE users SET active = 0 WHERE id <= 10")
    b = proxy.preview("UPDATE users SET active = 0 WHERE id <= 20")
    assert a["approval"].startswith("waiting") and b["approval"].startswith("waiting")
    assert "2 plans are waiting" in proxy.key("y")
    with pytest.raises(TxRefused):
        proxy.apply(a["plan"])


def test_only_statements_the_stage_can_roll_back_are_writes(tmp_path: Path) -> None:
    db = _users(tmp_path / "app.sqlite", n=5)
    proxy = _proxy(db, max_rows=100)
    for sql in ("REINDEX users", "ANALYZE", "CALL do_things()", "COPY users FROM '/x'"):
        with pytest.raises(TxRefused) as exc:
            proxy.preview(sql)
        assert exc.value.code == "bad_write", sql


def test_the_plan_binds_the_sqlite_file_a_relative_path_resolved_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.txplan.spec import TxSpec

    def spec(root: Path) -> TxSpec:
        data = {
            "schema_version": "openadapt.tx/v1",
            "systems": [
                {"name": "db", "kind": "sql", "dialect": "sqlite", "path": "app.sqlite"}
            ],
        }
        out = TxSpec.model_validate(data)
        out.source = root / "systems.tx.yaml"
        return out

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert engine._spec_sha(spec(tmp_path / "a")) != engine._spec_sha(
        spec(tmp_path / "b")
    )


def test_rest_path_values_are_one_segment() -> None:
    from openadapt_flow.txplan.adapters import AdapterError, RestAdapter

    fill = RestAdapter._fill
    assert fill("/c/{id}", {"id": "a/../b?x=1"}) == "/c/a%2F..%2Fb%3Fx%3D1"
    for bad in (".", ".."):
        with pytest.raises(AdapterError):
            fill("/c/{id}", {"id": bad})


def test_undo_of_a_write_it_cannot_undo_is_not_rolled_back(tmp_path: Path) -> None:
    db = _users(tmp_path / "app.sqlite", n=5)
    proxy = _proxy(db, max_rows=100)
    got = proxy.preview("CREATE TABLE extra (id INTEGER PRIMARY KEY)")
    proxy.key("y")
    assert proxy.apply(got["plan"])["outcome"] == "VERIFIED"
    undone = proxy.undo(got["plan"])
    assert undone["outcome"] == "RECONCILIATION_REQUIRED", undone
    assert undone["tx"]["no_generated_undo"] == 1


def test_proxy_commits_only_the_planned_diff(tmp_path: Path) -> None:
    db = _users(tmp_path / "app.sqlite", n=20)
    proxy = _proxy(db, max_rows=100)
    got = proxy.preview("UPDATE users SET name = 'x' WHERE active = 1")
    con = sqlite3.connect(db)  # another writer between the dry run and apply
    con.execute("UPDATE users SET active = 0 WHERE id = 7")
    con.commit()
    con.close()
    snapshot = _all(db)
    with pytest.raises(TxRefused) as exc:
        proxy.apply(got["plan"])
    assert exc.value.code == "plan_stale" and _all(db) == snapshot


def test_proxy_regenerated_statement_after_restore_is_held_not_applied(
    tmp_path: Path,
) -> None:
    db = _users(tmp_path / "app.sqlite", n=50)
    proxy = _proxy(db, max_rows=100)
    a = proxy.preview("UPDATE users SET name = 'refund 100' WHERE id = 9")
    proxy.apply(a["plan"])
    # Restored agent regenerates the same intent with a different payload.
    b = proxy.preview("UPDATE users SET name = 'refund 100 (ref 2)' WHERE id = 9")
    assert b["ledger"] == "possible_replay" and b["approval"].startswith("waiting")
    with pytest.raises(TxRefused) as exc:
        proxy.apply(b["plan"])
    assert exc.value.code in ("not_approved", "replay_mismatch")
    con = sqlite3.connect(db)
    assert (
        con.execute("SELECT name FROM users WHERE id = 9").fetchone()[0] == "refund 100"
    )
    con.close()
    # The identical statement now changes nothing: its dry run is empty.
    c = proxy.preview("UPDATE users SET name = 'refund 100' WHERE id = 9")
    assert c["tables"] == {}


def test_proxy_mcp_endpoint_needs_its_token_and_scrubs_the_child(
    tmp_path: Path,
) -> None:
    import httpx

    from openadapt_flow.txplan.proxy import McpServer, child_environment

    db = _users(tmp_path / "app.sqlite", n=5)
    server = McpServer(_proxy(db, max_rows=10))
    server.start()
    try:
        assert (
            httpx.post(
                server.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            ).status_code
            == 401
        )
        h = {"Authorization": f"Bearer {server.token}"}
        init = httpx.post(
            server.url,
            headers=h,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
        ).json()
        assert init["result"]["capabilities"] == {"tools": {}}
        names = [
            t["name"]
            for t in httpx.post(
                server.url,
                headers=h,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ).json()["result"]["tools"]
        ]
        assert names == ["sql_query", "sql_preview", "sql_apply", "sql_undo"]
        call = httpx.post(
            server.url,
            headers=h,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "sql_preview",
                    "arguments": {"sql": "UPDATE users SET active = 0 WHERE id = 2"},
                },
            },
        ).json()
        body = json.loads(call["result"]["content"][0]["text"])
        assert body["approval"] == "approved by policy"
    finally:
        server.stop()
    env = child_environment(
        {
            "DATABASE_URL": "postgres://u:pw@h/db",
            "X": "postgres://u:pw@h/db",
            "PGPASSWORD": "pw",
            "PATH": "/bin",
        },
        ["postgres://u:pw@h/db"],
    )
    assert env == {"PATH": "/bin"}


def test_proxy_overlapping_rows_after_apply_are_a_possible_replay(
    tmp_path: Path,
) -> None:
    db = _users(tmp_path / "app.sqlite", n=50)
    proxy = _proxy(db, max_rows=100)
    a = proxy.preview("UPDATE users SET name = 'refund' WHERE id = 9")
    proxy.apply(a["plan"])
    b = proxy.preview("UPDATE users SET name = 'refund again' WHERE id IN (9, 10)")
    assert b["ledger"] == "possible_replay" and b["approval"].startswith("waiting")
    other = proxy.preview("UPDATE users SET name = 'other' WHERE id IN (11, 12)")
    assert other["ledger"] == "free" and other["approval"] == "approved by policy"


def test_proxy_query_refuses_a_write(tmp_path: Path) -> None:
    db = _users(tmp_path / "app.sqlite", n=5)
    before = _all(db)
    proxy = _proxy(db)
    assert proxy.query("SELECT count(*) FROM users")["rows"] == [[5]]
    for bad in (
        "COMMIT; UPDATE users SET name = 'x'",
        "UPDATE users SET name = 'x'",
        "SELECT 1; DELETE FROM users",
    ):
        text, is_error = proxy.call("sql_query", {"sql": bad})
        assert is_error, bad
    text, is_error = proxy.call(
        "sql_query",
        {
            "sql": "WITH x AS (SELECT 1) INSERT INTO users (name, role, active) SELECT 'y', 'm', 1 FROM x"
        },
    )
    assert is_error
    assert _all(db) == before


def test_flow_cli_proxy_runs_the_command_with_scrubbed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.__main__ import main as flow_main

    db = _users(tmp_path / "app.sqlite", n=10)
    out = tmp_path / "child.json"
    child = (
        "import json, os, sys; json.dump({k: os.environ.get(k) for k in "
        "('OPENADAPT_MCP_URL', 'OPENADAPT_TASK', 'DATABASE_URL')}, open(sys.argv[1], 'w'))"
    )
    monkeypatch.setenv("DATABASE_URL", "postgres://u:pw@h/db")
    try:
        code = flow_main(
            ["proxy", "--sqlite", str(db), "--", sys.executable, "-c", child, str(out)]
        )
    except SystemExit as exc:
        code = exc.code
    assert code in (0, None)
    got = json.loads(out.read_text())
    assert (
        got["OPENADAPT_MCP_URL"].startswith("http://127.0.0.1:")
        and got["DATABASE_URL"] is None
    )
    assert got["OPENADAPT_TASK"].startswith("proxy-")


def test_proxy_commits_only_the_planned_values(tmp_path: Path) -> None:
    db = _users(tmp_path / "app.sqlite", n=10)
    proxy = _proxy(db, max_rows=100)
    got = proxy.preview("UPDATE users SET role = 'viewer' WHERE id = 3")
    digest = proxy.ctx.state.find_plan(got["plan"])
    priv_path = proxy.ctx.state.plan_dir(digest) / "private.json"
    priv = json.loads(priv_path.read_text())
    w = priv["writes"][0]
    w["fields"]["sql"] = (
        "UPDATE users SET role = 'admin' WHERE id = 3"  # same row, same column
    )
    priv_path.write_text(json.dumps(priv))
    before = _all(db)
    # The edited private plan no longer matches the digest a person approves.
    with pytest.raises(TxRefused) as exc:
        proxy.key("y")
    assert exc.value.code == "plan_tampered"
    with pytest.raises(TxRefused) as exc:
        proxy.apply(got["plan"])
    assert exc.value.code == "plan_tampered" and _all(db) == before


def test_proxy_time_defaults_are_volatile_and_random_values_are_flagged(
    tmp_path: Path,
) -> None:
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    con.commit()
    con.close()
    from openadapt_flow.txplan.proxy import Proxy, sql_spec

    proxy = Proxy(
        Ctx(spec=sql_spec(sqlite=str(db), max_rows=10), state=State()), task="T-vol"
    )
    a = proxy.preview("INSERT INTO notes (body) VALUES ('hi')")
    assert a["approval"] == "approved by policy"
    import time as _t

    _t.sleep(1.1)  # CURRENT_TIMESTAMP moves on: still the planned row
    assert proxy.apply(a["plan"])["outcome"] == "VERIFIED"
    b = proxy.preview("UPDATE notes SET body = hex(randomblob(8))")
    assert any("differ between dry runs" in x for x in b["anomalies"])
    assert b["approval"].startswith("waiting")


def test_proxy_serves_an_extra_toolset(tmp_path: Path) -> None:
    class Extra:
        def tools(self) -> list[dict[str, Any]]:
            return [
                {
                    "name": "api_read",
                    "description": "x",
                    "inputSchema": {"type": "object"},
                }
            ]

        def call(self, name: str, args: Any) -> tuple[str, bool]:
            return json.dumps({"tool": name}), False

    from openadapt_flow.txplan.proxy import McpServer, Proxy, sql_spec

    db = _users(tmp_path / "app.sqlite", n=3)
    proxy = Proxy(
        Ctx(spec=sql_spec(sqlite=str(db)), state=State()), task="T-x", extra=Extra()
    )
    server = McpServer(proxy)
    names = [
        t["name"]
        for t in server.handle({"id": 1, "method": "tools/list"})["result"]["tools"]
    ]
    assert names[-1] == "api_read" and "sql_preview" in names
    assert proxy.call("api_read", {}) == ('{"tool": "api_read"}', False)
    server.http.server_close()


# -- stale baseline: a whole-record write or a delete over another caller's edit


def _edit_before_send(
    monkeypatch: pytest.MonkeyPatch, store: RestStore, rid: int
) -> None:
    """Another caller saves a change to the record after every check OpenAdapt
    makes and before its write arrives (the last read-then-send window)."""
    original = RestAdapter.apply

    def apply(self: RestAdapter, write: Any, baseline: Any, key: str) -> Any:
        with store.lock:
            store.records[rid]["title"] = "edited by someone else"
            store.records[rid]["updated"] = store.records[rid]["updated"] + 1.0
        return original(self, write, baseline, key)

    monkeypatch.setattr(RestAdapter, "apply", apply)


def _update(rid: int, **fields: Any) -> dict[str, Any]:
    return {
        "system": "tasks",
        "collection": "item",
        "op": "update",
        "id": rid,
        "fields": fields,
    }


def test_unconditional_replace_over_a_concurrent_edit_is_not_verified(
    tmp_path: Path, rest, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue 2481 item 2, issue 2474 item 1: the replace body is the plan's
    # baseline, so the PUT writes the old title back over the other edit and
    # the read-back of the planned field still passes.
    store, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url, precondition=False))
    plan = _go(ctx, "T-lost", [_update(3, amount=7)])
    _edit_before_send(monkeypatch, store, 3)
    applied = engine.apply_plan(ctx, plan["digest"])
    assert store.records[3]["title"] == "item 3"  # the other edit was erased
    assert applied["outcome"] == "COMPLETED_UNVERIFIED", applied
    assert applied["tx"]["unconditional_writes"] == 1


def test_conditional_replace_over_a_concurrent_edit_is_refused_by_the_server(
    tmp_path: Path, rest, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url))
    plan = _go(ctx, "T-412", [_update(3, amount=7)])
    _edit_before_send(monkeypatch, store, 3)
    applied = engine.apply_plan(ctx, plan["digest"])
    assert store.precondition_failed == 1
    assert store.records[3]["title"] == "edited by someone else"
    assert store.records[3]["amount"] == 30
    assert applied["outcome"] == "RECONCILIATION_REQUIRED", applied
    assert applied["tx"]["stale_baseline"] == 1 and applied["tx"]["sent"] == 0


def test_conditional_replace_without_a_concurrent_edit_is_verified(
    tmp_path: Path, rest
) -> None:
    store, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url))
    plan = _go(ctx, "T-ok", [_update(3, amount=7)])
    applied = engine.apply_plan(ctx, plan["digest"])
    assert applied["outcome"] == "VERIFIED", applied
    assert store.records[3]["amount"] == 7 and store.precondition_failed == 0


def _delete(rid: int) -> dict[str, Any]:
    return {"system": "tasks", "collection": "item", "op": "delete", "id": rid}


def test_unconditional_held_delete_after_its_recheck_is_not_verified(
    tmp_path: Path, rest, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue 2481 item 2: release rechecks the record with a read, then sends;
    # an edit between the two is deleted with it, and the read-back (gone)
    # still passes.
    store, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url, holdback=0, precondition=False))
    plan = _go(ctx, "T-del", [_delete(5)])
    assert engine.apply_plan(ctx, plan["digest"])["tx"]["held"] == 1
    _edit_before_send(monkeypatch, store, 5)
    out = engine.release(ctx, plan["digest"])
    assert 5 not in store.records
    assert out["outcome"] == "COMPLETED_UNVERIFIED", out
    assert out["tx"]["unconditional_writes"] == 1


def test_conditional_held_delete_after_its_recheck_is_refused_by_the_server(
    tmp_path: Path, rest, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url, holdback=0))
    plan = _go(ctx, "T-del412", [_delete(5)])
    engine.apply_plan(ctx, plan["digest"])
    _edit_before_send(monkeypatch, store, 5)
    out = engine.release(ctx, plan["digest"])
    assert store.records[5]["title"] == "edited by someone else"
    assert out["outcome"] == "RECONCILIATION_REQUIRED", out
    assert store.precondition_failed == 1


def test_a_declared_precondition_the_read_cannot_fill_refuses_the_plan(
    tmp_path: Path, rest
) -> None:
    _, url = rest
    path = _spec(tmp_path, rest_url=url)
    data = yaml.safe_load(path.read_text())
    route = data["systems"][0]["collections"][0]["update"]
    route["precondition"] = {"source": "field", "field": "no_such_field"}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(TxRefused) as refused:
        engine.make_plan(_ctx(path), "T-nov", [_update(3, amount=7)])
    assert refused.value.code == "no_version"


# -- replay: a raw statement's stored verdict is not today's database ---------


def test_sql_replay_rereads_the_rows_instead_of_reusing_the_stored_flag(
    tmp_path: Path,
) -> None:
    # Issue 2481 item 3: the statement committed and matched; a person then
    # reverted the row. The same task and payload replay, nothing is sent,
    # and the replay must read the database, not the post-commit flag.
    db = _db(tmp_path / "crm.sqlite")
    ctx = _ctx(_spec(tmp_path, db=db))
    stmt = {
        "system": "crm",
        "op": "sql",
        "fields": {"sql": "UPDATE customers SET balance = 999 WHERE id = 3"},
    }
    first = engine.apply_plan(ctx, _go(ctx, "T1", [stmt])["digest"])
    assert first["outcome"] == "VERIFIED", first
    con = sqlite3.connect(db)
    con.execute("UPDATE customers SET balance = 30 WHERE id = 3")
    con.commit()
    con.close()
    again = engine.apply_plan(ctx, _go(ctx, "T1", [stmt])["digest"])
    assert again["tx"]["replays"] == 1 and again["tx"]["sent"] == 0
    assert again["tx"].get("unverified_replays") == 1, again
    assert again["outcome"] == "COMPLETED_UNVERIFIED", again


def test_sql_replay_whose_rows_still_hold_is_verified(tmp_path: Path) -> None:
    db = _db(tmp_path / "crm.sqlite")
    ctx = _ctx(_spec(tmp_path, db=db))
    stmt = {
        "system": "crm",
        "op": "sql",
        "fields": {"sql": "INSERT INTO orders (customer, total) VALUES (1, 1.5)"},
    }
    assert engine.apply_plan(ctx, _go(ctx, "T1", [stmt])["digest"])["outcome"] == (
        "VERIFIED"
    )
    again = engine.apply_plan(ctx, _go(ctx, "T1", [stmt])["digest"])
    assert again["tx"]["replays"] == 1 and not again["tx"].get("unverified_replays")
    assert again["outcome"] == "VERIFIED", again
    # The inserted row removed out of band: the replay is no longer VERIFIED.
    con = sqlite3.connect(db)
    con.execute("DELETE FROM orders WHERE total = 1.5")
    con.commit()
    con.close()
    third = engine.apply_plan(ctx, _go(ctx, "T1", [stmt])["digest"])
    assert third["tx"].get("unverified_replays") == 1, third


def test_unconditional_replace_is_not_verified_on_replay_either(
    tmp_path: Path, rest
) -> None:
    # The same task and payload again sends nothing; the read-back of the
    # planned field can't rule out a lost update any better the second time.
    _, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url, precondition=False))
    first = engine.apply_plan(ctx, _go(ctx, "T-rp", [_update(3, amount=7)])["digest"])
    assert first["outcome"] == "COMPLETED_UNVERIFIED", first
    again = engine.apply_plan(ctx, _go(ctx, "T-rp", [_update(3, amount=7)])["digest"])
    assert again["tx"]["replays"] == 1 and again["tx"]["sent"] == 0
    assert again["outcome"] == "COMPLETED_UNVERIFIED", again


def test_unconditional_replace_adopted_after_a_crash_is_not_verified(
    tmp_path: Path, rest
) -> None:
    # A crash after the send leaves the key ``begun``; the retry adopts the
    # write from its read-back instead of resending, and must not VERIFY it.
    store, url = rest
    store.records[3]["amount"] = 7  # the send before the crash landed
    ctx = _ctx(_spec(tmp_path, rest_url=url, precondition=False))
    plan = _go(ctx, "T-ad", [_update(3, amount=7)])
    w = plan["writes"][0]
    ctx.state.record(
        w["key"], "begun", plan=plan["digest"], payload=w["payload_sha256"]
    )
    out = engine.apply_plan(ctx, plan["digest"])
    assert out["tx"]["sent"] == 0
    assert out["outcome"] == "COMPLETED_UNVERIFIED", out


# -- closed scope: enforcement evidence, undo preconditions, scope caps -------
# A write is VERIFIED only when nothing it can't see could have changed data
# around it. Each negative below ends VERIFIED (or ROLLED_BACK) on the head
# before this rule.


def test_a_precondition_the_server_ignores_is_not_verified(
    tmp_path: Path, rest, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A declared If-Match the server never checks: the other caller's edit is
    # erased and the read-back of the planned field still passes.
    store, url = rest
    store.ignore_if_match = True
    ctx = _ctx(_spec(tmp_path, rest_url=url, precondition={}))
    plan = _go(ctx, "T-ign", [_update(3, amount=7)])
    _edit_before_send(monkeypatch, store, 3)
    out = engine.apply_plan(ctx, plan["digest"])
    assert store.records[3]["title"] == "item 3"  # the edit was erased
    assert out["outcome"] == "COMPLETED_UNVERIFIED", out
    assert out["tx"]["not_covered"] == ["precondition_unproven"]


def test_recorded_evidence_with_a_status_that_is_not_a_refusal_proves_nothing(
    tmp_path: Path, rest
) -> None:
    _, url = rest
    pre = {"enforcement": {"status": 200, "observed": "2026-11-22"}}
    ctx = _ctx(_spec(tmp_path, rest_url=url, precondition=pre))
    out = engine.apply_plan(ctx, _go(ctx, "T-200", [_update(3, amount=7)])["digest"])
    assert out["outcome"] == "COMPLETED_UNVERIFIED", out


def test_a_probe_the_server_refuses_proves_the_precondition_once_per_route(
    tmp_path: Path, rest
) -> None:
    store, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url, precondition={"probe": True}))
    plan = _go(ctx, "T-pr", [_update(3, amount=7), _update(4, amount=8)])
    out = engine.apply_plan(ctx, plan["digest"])
    assert out["outcome"] == "VERIFIED", out
    assert store.precondition_failed == 1  # one probe, for both writes
    assert store.records[3]["amount"] == 7 and store.records[4]["amount"] == 8
    assert store.writes == 2


def test_a_probe_the_server_accepts_is_the_write_and_is_never_verified(
    tmp_path: Path, rest
) -> None:
    store, url = rest
    store.ignore_if_match = True
    ctx = _ctx(_spec(tmp_path, rest_url=url, precondition={"probe": True}))
    out = engine.apply_plan(ctx, _go(ctx, "T-pa", [_update(3, amount=7)])["digest"])
    assert store.writes == 1  # the probe landed; nothing was sent again
    assert store.records[3]["amount"] == 7
    assert out["outcome"] == "COMPLETED_UNVERIFIED", out
    assert out["tx"]["not_covered"] == ["precondition_unproven"]


def test_an_undo_with_no_precondition_is_not_a_verified_rollback(
    tmp_path: Path, rest
) -> None:
    # The undo of a create is a delete with no version check: an edit made to
    # the new record after the undo's check would be deleted with it.
    store, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url, precondition=False))
    create = {
        "system": "tasks",
        "collection": "item",
        "op": "create",
        "fields": {"title": "new", "amount": 1},
    }
    plan = _go(ctx, "T-uc", [create])
    assert engine.apply_plan(ctx, plan["digest"])["outcome"] == "VERIFIED"
    undo = engine.undo_plan(ctx, plan["digest"])
    assert len(store.records) == 10  # the undo itself ran
    assert undo["outcome"] == "COMPLETED_UNVERIFIED", undo
    assert undo["tx"]["not_covered"] == ["unconditional_undo"]


def test_an_undo_carries_the_version_it_checked(
    tmp_path: Path, rest, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another caller edits the record after the undo's check and before its
    # restore arrives. The restore names the version checked, so the server
    # refuses it and the edit stays.
    store, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url))
    plan = _go(ctx, "T-uv", [_update(3, amount=7)])
    assert engine.apply_plan(ctx, plan["digest"])["outcome"] == "VERIFIED"
    original = RestAdapter._call

    def call(self: RestAdapter, method: str, path: str, *a: Any, **kw: Any) -> Any:
        if str(kw.get("key") or "").endswith(":undo"):
            with store.lock:
                store.records[3]["title"] = "edited by someone else"
                store.records[3]["updated"] = store.records[3]["updated"] + 1.0
        return original(self, method, path, *a, **kw)

    monkeypatch.setattr(RestAdapter, "_call", call)
    undo = engine.undo_plan(ctx, plan["digest"])
    assert store.records[3]["title"] == "edited by someone else", store.records[3]
    assert undo["outcome"] != "ROLLED_BACK", undo
    assert undo["tx"]["codes"] == {"changed_since_apply": 1}


def test_an_undo_with_an_enforced_precondition_is_rolled_back(
    tmp_path: Path, rest
) -> None:
    store, url = rest
    ctx = _ctx(_spec(tmp_path, rest_url=url))
    plan = _go(ctx, "T-ur", [_update(3, amount=7)])
    assert engine.apply_plan(ctx, plan["digest"])["outcome"] == "VERIFIED"
    undo = engine.undo_plan(ctx, plan["digest"])
    assert undo["outcome"] == "ROLLED_BACK", undo
    assert store.records[3]["amount"] == 30
    assert store.if_match_seen[-1] is not None  # the restore was conditional


def _sql_update(rid: int, **fields: Any) -> dict[str, Any]:
    return {
        "system": "crm",
        "collection": "customers",
        "op": "update",
        "id": rid,
        "fields": fields,
    }


def _open_scope(monkeypatch: pytest.MonkeyPatch, *gaps: str) -> None:
    """The adapter's scope inventory finds these gaps (a foreign table, say)."""
    from openadapt_flow.txplan.adapters import SqlAdapter

    monkeypatch.setattr(
        SqlAdapter, "scope_gaps", lambda self, cur, sql="": list(gaps), raising=False
    )


def test_a_database_write_with_an_open_scope_is_capped_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path / "crm.sqlite")
    _open_scope(monkeypatch, "foreign_data")
    ctx = _ctx(_spec(tmp_path, db=db))
    plan = _go(ctx, "T-open", [_sql_update(3, balance=99.5)])
    out = engine.apply_plan(ctx, plan["digest"])
    assert out["outcome"] == "COMPLETED_UNVERIFIED", out
    assert out["tx"]["not_covered"] == ["foreign_data"]
    assert (3, "cust 3", 99.5, "") in _rows(db)["customers"]  # it was written
    assert plan["writes"][0].get("not_covered") == ["foreign_data"]


def test_a_raw_statement_with_an_open_scope_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path / "crm.sqlite")
    _open_scope(monkeypatch, "truncate")
    ctx = _ctx(_spec(tmp_path, db=db))
    sql = {
        "system": "crm",
        "op": "sql",
        "fields": {"sql": "UPDATE customers SET balance = 1 WHERE id = 2"},
    }
    out = engine.apply_plan(ctx, _go(ctx, "T-raw", [sql])["digest"])
    assert out["outcome"] == "COMPLETED_UNVERIFIED", out
    assert out["tx"]["not_covered"] == ["truncate"]


def test_a_replay_of_a_ledger_entry_with_no_recorded_scope_is_not_verified(
    tmp_path: Path,
) -> None:
    # An entry written before the scope check (or by an adoption) says nothing
    # about what its transaction could reach.
    db = _db(tmp_path / "crm.sqlite")
    ctx = _ctx(_spec(tmp_path, db=db))
    plan = _go(ctx, "T-old", [_sql_update(3, balance=99.5)])
    assert engine.apply_plan(ctx, plan["digest"])["outcome"] == "VERIFIED"
    w = plan["writes"][0]
    ctx.state.record(
        w["key"],
        "applied",
        plan=plan["digest"],
        payload=w["payload_sha256"],
        result={"id": "3"},
    )
    again = engine.apply_plan(
        ctx, _go(ctx, "T-old", [_sql_update(3, balance=99.5)])["digest"]
    )
    assert again["tx"]["replays"] == 1 and again["tx"]["sent"] == 0
    assert again["outcome"] == "COMPLETED_UNVERIFIED", again
    assert again["tx"]["not_covered"] == ["scope_unrecorded"]


def test_a_replay_keeps_the_scope_gaps_of_its_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path / "crm.sqlite")
    _open_scope(monkeypatch, "rule")
    ctx = _ctx(_spec(tmp_path, db=db))
    first = engine.apply_plan(
        ctx, _go(ctx, "T-rr", [_sql_update(3, balance=99.5)])["digest"]
    )
    assert first["outcome"] == "COMPLETED_UNVERIFIED"
    _open_scope(monkeypatch)  # closed now: the replay keeps what the send saw
    again = engine.apply_plan(
        ctx, _go(ctx, "T-rr", [_sql_update(3, balance=99.5)])["digest"]
    )
    assert again["tx"]["replays"] == 1
    assert again["outcome"] == "COMPLETED_UNVERIFIED", again
    assert again["tx"]["not_covered"] == ["rule"]


def test_a_database_undo_with_an_open_scope_is_not_a_verified_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path / "crm.sqlite")
    before = _rows(db)
    ctx = _ctx(_spec(tmp_path, db=db))
    plan = _go(ctx, "T-su", [_sql_update(3, balance=99.5)])
    assert engine.apply_plan(ctx, plan["digest"])["outcome"] == "VERIFIED"
    _open_scope(monkeypatch, "trigger_not_local")
    undo = engine.undo_plan(ctx, plan["digest"])
    assert _rows(db) == before  # the undo ran
    assert undo["outcome"] == "COMPLETED_UNVERIFIED", undo
    assert undo["tx"]["not_covered"] == ["trigger_not_local"]
