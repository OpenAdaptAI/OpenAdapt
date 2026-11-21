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


def _spec(
    tmp_path: Path,
    *,
    db: Path | None = None,
    rest_url: str | None = None,
    mail: MailSink | None = None,
    holdback: float = 60,
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
                        },
                        "delete": {"method": "DELETE", "path": "/api/items/{id}"},
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
