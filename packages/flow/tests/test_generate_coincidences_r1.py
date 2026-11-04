# ruff: noqa: F811  (pytest fixtures imported from the generator test, then used as arguments)
"""Review round 1 of #1682: each attack, as a test.

On a throwaway PostgreSQL with no declared foreign keys:

- a line table whose key is (order_id, line_no): the order's key is part of
  the line's key, and links them;
- a one-to-one extension table keyed by its parent's UUID;
- a column equal to "13" while the row's own key is 3 (a substring, not a
  template), seen beside one write;
- a constant equal to the month of the dates every demonstration gave;
- a column that holds the acting user's id and also a copy of the project's
  manager, who is the acting user in every demonstration.

Records are made up.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from typing import Any, Callable

import pytest
import yaml

psycopg2 = pytest.importorskip("psycopg2")

from openadapt_flow.generate import verify_contract as gen  # noqa: E402
from tests.test_generate_verify_contract import BOT, App, pg  # noqa: E402,F401

SCHEMA = """
CREATE TABLE users (id serial PRIMARY KEY, login text);
CREATE TABLE po (id serial PRIMARY KEY, supplier text NOT NULL);
CREATE TABLE po_items (order_id integer NOT NULL, line_no integer NOT NULL,
  part text NOT NULL, qty integer NOT NULL, PRIMARY KEY (order_id, line_no));
CREATE TABLE accounts (id text PRIMARY KEY, name text NOT NULL);
CREATE TABLE accounts_cstm (id_c text PRIMARY KEY, industry_c text);
CREATE TABLE invoices (id serial PRIMARY KEY, customer text NOT NULL, note text,
  tag text, due date NOT NULL, discount integer NOT NULL, priority integer NOT NULL);
CREATE TABLE projects (id serial PRIMARY KEY, name text NOT NULL, manager_id integer);
CREATE TABLE tasks (id serial PRIMARY KEY, project_id integer NOT NULL,
  title text NOT NULL, assignee_id integer);
INSERT INTO users (login) VALUES ('bot'), ('ana'), ('raj');
INSERT INTO projects (name, manager_id) VALUES ('Apollo', 1), ('Gemini', 1), ('Mercury', 3);
SELECT setval('invoices_id_seq', 2);
SELECT setval('po_id_seq', 5);
CREATE TABLE customers (id serial PRIMARY KEY, name text NOT NULL, status integer);
CREATE TABLE bills (id serial PRIMARY KEY, customer_id integer NOT NULL,
  ref text NOT NULL, status integer NOT NULL);
INSERT INTO customers (name, status) VALUES ('Acme', 5), ('Globex', 5),
  ('Initech', 6), ('Umbrella', 6);
CREATE TABLE products (id serial PRIMARY KEY, name text NOT NULL, price integer NOT NULL);
INSERT INTO products (name, price) VALUES ('Widget', 15), ('Gadget', 25), ('Doohickey', 40);
CREATE TABLE sales (id serial PRIMARY KEY, product_id integer NOT NULL,
  ref text NOT NULL, price integer NOT NULL);
CREATE TABLE jobs (id serial PRIMARY KEY, title text NOT NULL);
CREATE TABLE job_audit (id serial PRIMARY KEY, job_id integer NOT NULL, ref text NOT NULL);
SELECT setval('jobs_id_seq', 50);
GRANT SELECT ON ALL TABLES IN SCHEMA public TO oa_reader;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO oa_reader;
"""


class Shop(App):
    def bill(self, v: dict[str, Any], status: int = 5) -> None:
        cid = self.one("SELECT id FROM customers WHERE name=%s", (v["customer"],))
        self.q("INSERT INTO bills (customer_id, ref, status) VALUES (%s,%s,%s)",
               (cid, v["ref"], status))  # fmt: skip

    def sale(self, v: dict[str, Any], price: Any = None) -> None:
        pid, cost = self.q(
            "SELECT id, price FROM products WHERE name=%s", (v["product"],)
        )[0]
        self.q("INSERT INTO sales (product_id, ref, price) VALUES (%s,%s,%s)",
               (pid, v["ref"], cost if price is None else price))  # fmt: skip

    def job(self, v: dict[str, Any]) -> None:
        jid = self.one(
            "INSERT INTO jobs (title) VALUES (%s) RETURNING id", (v["title"],)
        )
        aid = self.one(
            "INSERT INTO job_audit (job_id, ref) VALUES (%s, '') RETURNING id", (jid,)
        )
        self.q("UPDATE job_audit SET ref=%s WHERE id=%s", (f"AUD-{aid}", aid))

    def po(self, v: dict[str, Any]) -> None:
        oid = self.one(
            "INSERT INTO po (supplier) VALUES (%s) RETURNING id", (v["supplier"],)
        )
        for n, item in enumerate(v["items"], 1):
            self.q(
                "INSERT INTO po_items VALUES (%s,%s,%s,%s)",
                (oid, n, item["part"], item["qty"]),
            )

    def account(self, v: dict[str, Any], industry: Any = "same") -> None:
        key = str(uuid.uuid4())
        self.q("INSERT INTO accounts VALUES (%s,%s)", (key, v["name"]))
        ind = v.get("industry") if industry == "same" else industry
        self.q("INSERT INTO accounts_cstm VALUES (%s,%s)", (key, ind))

    def invoice(
        self, v: dict[str, Any], discount: int = 13, priority: int = 11
    ) -> None:
        self.q("INSERT INTO invoices (customer, note, tag, due, discount, priority) "
               "VALUES (%s,%s,%s,%s,%s,%s)",
               (v["customer"], v.get("note"), v.get("tag"), v["due"], discount, priority))  # fmt: skip

    def task(self, v: dict[str, Any]) -> None:
        pid = self.one("SELECT id FROM projects WHERE name=%s", (v["project"],))
        mgr = self.one("SELECT manager_id FROM projects WHERE id=%s", (pid,))
        self.q("INSERT INTO tasks (project_id, title, assignee_id) VALUES (%s,%s,%s)",
               (pid, v["title"], mgr))  # fmt: skip


PO = [
    {"supplier": "Bolt Co", "items": [{"part": "M4 bolt", "qty": 10}]},
    {
        "supplier": "Nut Co",
        "items": [{"part": "M4 nut", "qty": 20}, {"part": "M5 nut", "qty": 5}],
    },
]
ACCOUNT = [
    {"name": "Acme", "industry": "Retail"},
    {"name": "Globex", "industry": "Energy"},
]
INVOICE = [
    {"customer": "Acme", "note": "Net 30", "tag": None, "due": "2026-11-05"},  # id 3
    {"customer": "Globex", "note": None, "tag": "q4", "due": "2026-11-20"},  # id 4
]
BILL = [{"customer": "Acme", "ref": "B-1"}, {"customer": "Globex", "ref": "B-2"}]
SALE = [{"product": "Widget", "ref": "S-1"}, {"product": "Gadget", "ref": "S-2"}]
JOB = [{"title": "Kickoff"}, {"title": "Review"}]
TASK = [
    {"project": "Apollo", "title": "Kickoff"},
    {"project": "Gemini", "title": "Review"},
]
KINDS = {
    "kinds": [
        {"kind": "create_po", "parameters": ["supplier", "items"],
         "examples": [{"values": v} for v in PO]},
        {"kind": "create_account", "parameters": ["name", "industry"],
         "examples": [{"values": v} for v in ACCOUNT]},
        {"kind": "create_invoice", "parameters": ["customer", "note", "tag", "due"],
         "examples": [{"values": v} for v in INVOICE]},
        {"kind": "create_bill", "parameters": ["customer", "ref"],
         "examples": [{"values": v} for v in BILL]},
        {"kind": "create_sale", "parameters": ["product", "ref"],
         "examples": [{"values": v} for v in SALE]},
        {"kind": "create_job", "parameters": ["title"],
         "examples": [{"values": v} for v in JOB]},
        {"kind": "create_task", "parameters": ["project", "title"],
         "examples": [{"values": v} for v in TASK]},
    ]
}  # fmt: skip


@pytest.fixture(scope="module")
def shop(pg: Any) -> dict[str, Any]:
    root, port = pg["root"], pg["port"]
    admin = psycopg2.connect(
        host="127.0.0.1", port=port, user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE shop")
    admin.close()
    app = Shop(port, "shop")
    with app.con() as c, c.cursor() as cur:
        cur.execute(
            "DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='oa_reader') "
            "THEN CREATE ROLE oa_reader LOGIN PASSWORD 'reader-pw'; END IF; END$$"
        )
        cur.execute(SCHEMA)
    os.environ["OA_GEN_TEST_PW"] = "reader-pw"
    work = root / "work-coincidences-r1"
    work.mkdir()
    read = {"sql_driver": "psycopg2",
            "sql_connect_args": {"host": "127.0.0.1", "port": port, "user": "oa_reader", "dbname": "shop"},
            "sql_password_env": "OA_GEN_TEST_PW"}  # fmt: skip
    (work / "read.yaml").write_text(yaml.safe_dump(read))
    (work / "kinds.yaml").write_text(yaml.safe_dump(KINDS))
    common = ["--read", str(work / "read.yaml")]

    def demo(kind: str, values: dict[str, Any], write: Callable[[], Any]) -> None:
        assert gen.main(["snapshot", *common, "--out", str(work / "before.json")]) == 0
        write()
        assert gen.main(["record", *common, "--before", str(work / "before.json"), "--kind", kind,
                         "--values", json.dumps(values), "--demos", str(work / "demos")]) == 0  # fmt: skip

    for v in PO:
        demo("create_po", v, lambda v=v: app.po(v))
    for v in ACCOUNT:
        demo("create_account", v, lambda v=v: app.account(v))
    for v in INVOICE:
        demo("create_invoice", v, lambda v=v: app.invoice(v))
    for v in BILL:
        demo("create_bill", v, lambda v=v: app.bill(v))
    for v in SALE:
        demo("create_sale", v, lambda v=v: app.sale(v))
    for v in JOB:
        demo("create_job", v, lambda v=v: app.job(v))
    for v in TASK:
        demo("create_task", v, lambda v=v: app.task(v))
    argv = ["propose", *common, "--kinds", str(work / "kinds.yaml"), "--demos", str(work / "demos"),
            "--out", str(work / "out"), "--actor-param", f"bot_user_id={BOT}"]  # fmt: skip
    assert gen.main(argv) == 0
    first = json.loads((work / "out" / "report.json").read_text())
    questions = (work / "out" / "QUESTIONS.md").read_text()
    answers = {q["key"]: q["proposal"] for q in first["questions"]}
    # The person names the column that holds a row's own key (an audit row's
    # reference). A sale's price only reads like one: it is a copy.
    bk = answers["bookkeeping"]
    bk = json.loads(json.dumps(bk)) if isinstance(bk, dict) else {"accept": True}
    named = bk.setdefault("unchecked", {})
    for table, col in (("job_audit", "ref"),):
        if col not in named.setdefault(table, []):
            named[table].append(col)
    answers["bookkeeping"] = bk
    (work / "answers.yaml").write_text(yaml.safe_dump(answers))
    assert gen.main([*argv, "--answers", str(work / "answers.yaml")]) == 0
    report = json.loads((work / "out" / "report.json").read_text())
    return {"app": app, "out": work / "out", "report": report, "first": first,
            "questions": questions}  # fmt: skip


def _run(
    shop: dict[str, Any], kind: str, values: dict[str, Any], write: Callable[[], Any]
) -> str:
    from openadapt_flow.verifier import Actor, begin_write, verify_write

    try:
        contract, params = gen.choose(shop["out"] / "manifest.yaml", kind, values,
                                      {"bot_user_id": str(BOT)})  # fmt: skip
    except gen.GenerateError as exc:
        return f"NO CONTRACT {exc}"
    with tempfile.TemporaryDirectory() as tmp:
        pending = begin_write(contract, params, actor=Actor("agent", "ops-bot"), ledger=None,
                              write_key=f"t-{time.time_ns()}", workdir=tmp)  # fmt: skip
        write()
        result = verify_write(pending)
    return (
        str(getattr(result.outcome, "value", result.outcome))
        + " "
        + "; ".join(result.reasons)[:300]
    )


def test_b1_a_composite_key_member_links_line_items(shop: dict[str, Any]) -> None:
    v = {
        "supplier": "Bolt Co",
        "items": [{"part": "M6 bolt", "qty": 3}, {"part": "M8 bolt", "qty": 4}],
    }
    out = _run(shop, "create_po", v, lambda: shop["app"].po(v))
    assert out.startswith("CONFIRMED"), out


def test_b1_a_uuid_extension_row_is_a_link(shop: dict[str, Any]) -> None:
    where = shop["report"]["kinds"]["create_account"]["locations"]["industry"]
    assert "accounts_cstm" in where, where
    v = {"name": "Hooli", "industry": "Media"}
    assert _run(shop, "create_account", v, lambda: shop["app"].account(v)).startswith(
        "CONFIRMED"
    )
    v = {"name": "Initech", "industry": "Media"}
    wrong = _run(shop, "create_account", v, lambda: shop["app"].account(v, "Retail"))
    assert not wrong.startswith("CONFIRMED"), wrong


@pytest.mark.parametrize("discount,priority", [(99, 11), (3, 11), (13, 99)])
def test_b2_a_substring_or_a_month_never_leaves_a_column_open(
    shop: dict[str, Any], discount: int, priority: int
) -> None:
    """``discount`` 13 holds the key 3 only as a substring, and ``priority``
    11 equals the month of both due dates: both are constants, pinned."""

    v = {"customer": "Umbrella", "note": "Net 60", "tag": None, "due": "2026-12-01"}
    out = _run(shop, "create_invoice", v,
               lambda: shop["app"].invoice(v, discount=discount, priority=priority))  # fmt: skip
    assert not out.startswith("CONFIRMED"), out


def test_b2_the_constants_still_pass(shop: dict[str, Any]) -> None:
    v = {"customer": "Umbrella", "note": "Net 60", "tag": None, "due": "2026-12-02"}
    out = _run(shop, "create_invoice", v, lambda: shop["app"].invoice(v))
    assert out.startswith("CONFIRMED"), out


def test_an_actor_id_that_is_also_a_copy_is_asked(shop: dict[str, Any]) -> None:
    """The bot manages both demonstrated projects, so the task's assignee is
    the bot's id and a copy of the manager. A question, never a silent pick."""

    assert "tasks.assignee_id (the acting user's id or a copy of" in shop["questions"]
    refused = shop["report"]["kinds"]["create_task"]["refused"]
    assert any("tasks.assignee_id" in r for r in refused), refused


# -- review round 2 ------------------------------------------------------------


def test_a_constant_equal_to_its_source_in_every_demo_is_asked(
    shop: dict[str, Any],
) -> None:
    """The app stores status 5 on every bill; both demonstrated customers
    happen to have status 5. A constant and a copy both fit: a question,
    and no contract until the person answers (never a silent copy)."""

    assert "bills.status (a constant or a copy of customers.status" in shop["questions"]
    refused = shop["report"]["kinds"]["create_bill"]["refused"]
    assert any("bills.status" in r for r in refused), refused
    # Customer 4's status is 6: under a copy, a wrong 6 would have passed.
    v = {"customer": "Umbrella", "ref": "B-9"}
    out = _run(shop, "create_bill", v, lambda: shop["app"].bill(v, status=6))
    assert not out.startswith("CONFIRMED"), out


# -- review round 3 ------------------------------------------------------------


@pytest.mark.parametrize("price,confirmed", [(None, True), (45, False)])
def test_a_copy_that_reads_as_the_own_key_is_checked_as_a_copy(
    shop: dict[str, Any], price: Any, confirmed: bool
) -> None:
    """Sales 1 and 2 copied prices 15 and 25, which read as ``{key}5``. The
    column is a copy of the product's price, never asked about, so a wrong
    45 on the next sale is refuted and the real price (40) passes."""

    refused_first = shop["first"]["kinds"]["create_sale"]["refused"]
    assert not any("sales.price" in r for r in refused_first), refused_first
    assert "sales.price" not in json.dumps(shop["first"]["questions"])
    v = {"product": "Doohickey", "ref": f"S-{price}"}
    out = _run(shop, "create_sale", v, lambda: shop["app"].sale(v, price=price))
    assert out.startswith("CONFIRMED") is confirmed, out


def test_an_audit_row_holding_its_own_key_can_be_named(shop: dict[str, Any]) -> None:
    """The app adds an audit row ``AUD-{id}`` beside each job. The question
    lists it, the answer names it, and a correct job is CONFIRMED."""

    assert "job_audit" in shop["questions"]
    v = {"title": "Retro"}
    out = _run(shop, "create_job", v, lambda: shop["app"].job(v))
    assert out.startswith("CONFIRMED"), out
