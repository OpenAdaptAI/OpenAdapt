# ruff: noqa: F811  (pytest fixtures imported from the generator test, then used as arguments)
"""Joins and pins two demonstrations can't tell apart (#1677, item 4).

A small time tracker and order book on a throwaway PostgreSQL, with no
declared foreign keys, seeded so that ids coincide in every demonstration:

- a new time entry's own id equals its customer's id;
- a new ticket's ``ext_id`` equals its customer's id, while the real path is
  the ticket's project and that project's customer;
- an order line's own id equals its order's id;
- a value one demonstration alone stored (an order's currency).

The generator must never take a row's own key for a join, must ask when a
join only equal ids support has a supported rival, and must not pin a value
seen beside one write only. Correct writes are then CONFIRMED and wrong ones
stopped. Records are made up.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Callable

import pytest
import yaml

psycopg2 = pytest.importorskip("psycopg2")

from openadapt_flow.generate import verify_contract as gen  # noqa: E402
from tests.test_generate_verify_contract import BOT, App, pg  # noqa: E402,F401

SCHEMA = """
CREATE TABLE users (id serial PRIMARY KEY, login text);
CREATE TABLE customers (id serial PRIMARY KEY, name text NOT NULL);
CREATE TABLE projects (id serial PRIMARY KEY, name text NOT NULL, customer_id integer);
CREATE TABLE timesheets (id serial PRIMARY KEY, project_id integer NOT NULL,
  hours integer NOT NULL, user_id integer NOT NULL, created_at timestamp);
CREATE TABLE tickets (id serial PRIMARY KEY, project_id integer NOT NULL,
  title text NOT NULL, ext_id integer, user_id integer NOT NULL, created_at timestamp);
CREATE TABLE orders (id serial PRIMARY KEY, supplier text NOT NULL, note text,
  currency text NOT NULL, user_id integer NOT NULL, created_at timestamp);
CREATE TABLE order_lines (id serial PRIMARY KEY, order_id integer NOT NULL,
  part text NOT NULL, qty integer NOT NULL);
INSERT INTO users (login) VALUES ('bot');
INSERT INTO customers (name) VALUES ('Acme'), ('Globex'), ('Initech'), ('Umbrella'),
  ('Hooli'), ('Vandelay'), ('Stark'), ('Wayne');
INSERT INTO projects (name, customer_id) VALUES ('Apollo', 3), ('Gemini', 4),
  ('Mercury', 6), ('Skylab', 3), ('Ranger', 7);
SELECT setval('timesheets_id_seq', 2);
SELECT setval('tickets_id_seq', 40);
SELECT setval('orders_id_seq', 2);
SELECT setval('order_lines_id_seq', 2);
GRANT SELECT ON ALL TABLES IN SCHEMA public TO oa_reader;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO oa_reader;
"""


class Book(App):
    def project(self, name: str) -> int:
        return self.one("SELECT id FROM projects WHERE name=%s", (name,))

    def log_time(self, v: dict[str, Any]) -> None:
        self.q("INSERT INTO timesheets (project_id, hours, user_id, created_at) "
               "VALUES (%s,%s,%s,now())", (self.project(v["project"]), v["hours"], BOT))  # fmt: skip

    def open_ticket(self, v: dict[str, Any], ext: Any) -> None:
        self.q("INSERT INTO tickets (project_id, title, ext_id, user_id, created_at) "
               "VALUES (%s,%s,%s,%s,now())", (self.project(v["project"]), v["title"], ext, BOT))  # fmt: skip

    def order(self, v: dict[str, Any], currency: str) -> None:
        oid = self.one("INSERT INTO orders (supplier, note, currency, user_id, created_at) "
                       "VALUES (%s,%s,%s,%s,now()) RETURNING id",
                       (v["supplier"], v.get("note"), currency, BOT))  # fmt: skip
        for line in v["lines"]:
            self.q("INSERT INTO order_lines (order_id, part, qty) VALUES (%s,%s,%s)",
                   (oid, line["part"], line["qty"]))  # fmt: skip


LOG = [
    {"project": "Apollo", "customer": "Initech", "hours": 3},  # entry 3, customer 3
    {"project": "Gemini", "customer": "Umbrella", "hours": 5},  # entry 4, customer 4
]
TICKET = [
    {"project": "Apollo", "customer": "Initech", "title": "Printer"},  # ext 3
    {"project": "Gemini", "customer": "Umbrella", "title": "VPN"},  # ext 4
]
ORDER = [
    {"supplier": "Bolt Co", "note": "Rush", "lines": [{"part": "M4 bolt", "qty": 10}]},
    {"supplier": "Nut Co", "note": None, "lines": [{"part": "M4 nut", "qty": 20}]},
]
KINDS = {
    "kinds": [
        {"kind": "log_time", "parameters": ["project", "customer", "hours"],
         "examples": [{"values": v} for v in LOG]},
        {"kind": "open_ticket", "parameters": ["project", "customer", "title"],
         "examples": [{"values": v} for v in TICKET]},
        {"kind": "create_order", "parameters": ["supplier", "note", "lines"],
         "examples": [{"values": v} for v in ORDER]},
    ]
}  # fmt: skip


@pytest.fixture(scope="module")
def book(pg: Any) -> dict[str, Any]:
    root, port = pg["root"], pg["port"]
    admin = psycopg2.connect(
        host="127.0.0.1", port=port, user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE book")
    admin.close()
    app = Book(port, "book")
    with app.con() as c, c.cursor() as cur:
        cur.execute(
            "DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='oa_reader') "
            "THEN CREATE ROLE oa_reader LOGIN PASSWORD 'reader-pw'; END IF; END$$"
        )
        cur.execute(SCHEMA)
    os.environ["OA_GEN_TEST_PW"] = "reader-pw"
    work = root / "work-coincidences"
    work.mkdir()
    read = {"sql_driver": "psycopg2",
            "sql_connect_args": {"host": "127.0.0.1", "port": port, "user": "oa_reader", "dbname": "book"},
            "sql_password_env": "OA_GEN_TEST_PW"}  # fmt: skip
    (work / "read.yaml").write_text(yaml.safe_dump(read))
    (work / "kinds.yaml").write_text(yaml.safe_dump(KINDS))
    common = ["--read", str(work / "read.yaml")]

    def demo(kind: str, values: dict[str, Any], write: Callable[[], Any]) -> None:
        assert gen.main(["snapshot", *common, "--out", str(work / "before.json")]) == 0
        write()
        assert gen.main(["record", *common, "--before", str(work / "before.json"), "--kind", kind,
                         "--values", json.dumps(values), "--demos", str(work / "demos")]) == 0  # fmt: skip

    for v in LOG:
        demo("log_time", v, lambda v=v: app.log_time(v))
    for v, ext in zip(TICKET, (3, 4)):
        demo("open_ticket", v, lambda v=v, ext=ext: app.open_ticket(v, ext))
    for v, cur in zip(ORDER, ("SEK", "USD")):
        demo("create_order", v, lambda v=v, cur=cur: app.order(v, cur))
    argv = ["propose", *common, "--kinds", str(work / "kinds.yaml"), "--demos", str(work / "demos"),
            "--out", str(work / "out"), "--actor-param", f"bot_user_id={BOT}"]  # fmt: skip
    assert gen.main(argv) == 0
    first = json.loads((work / "out" / "report.json").read_text())
    questions = (work / "out" / "QUESTIONS.md").read_text()
    answers = {q["key"]: q["proposal"] for q in first["questions"]}
    # The person picks the ticket's customer behind its project.
    tied = first["kinds"]["open_ticket"]["unresolved"].get("customer") or []
    if tied:
        answers["locations.open_ticket"] = {
            "customer": next(
                i for i, t in enumerate(tied) if "projects.customer_id" in t
            )
        }
    # The order's currency was seen beside one write only: the person
    # names it as unchecked (else the kind has no contract).
    bk = answers["bookkeeping"]
    bk = json.loads(json.dumps(bk)) if isinstance(bk, dict) else {"accept": True}
    bk.setdefault("unchecked", {}).setdefault("orders", []).append("currency")
    answers["bookkeeping"] = bk
    (work / "answers.yaml").write_text(yaml.safe_dump(answers))
    assert gen.main([*argv, "--answers", str(work / "answers.yaml")]) == 0
    report = json.loads((work / "out" / "report.json").read_text())
    return {"app": app, "out": work / "out", "first": first, "report": report,
            "questions": questions}  # fmt: skip


def _run(
    book: dict[str, Any], kind: str, values: dict[str, Any], write: Callable[[], Any]
) -> str:
    from openadapt_flow.verifier import Actor, begin_write, verify_write

    contract, params = gen.choose(book["out"] / "manifest.yaml", kind, values,
                                  {"bot_user_id": str(BOT)})  # fmt: skip
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


def test_a_rows_own_key_is_never_a_join(book: dict[str, Any]) -> None:
    """Each new time entry's id equalled its customer's id. The customer is
    placed behind the project, never at the entry's own key."""

    where = book["report"]["kinds"]["log_time"]["locations"]["customer"]
    assert where.startswith(
        "timesheets.project_id -> projects.customer_id -> customers"
    ), where
    # A correct entry on a project whose customer's id is not the entry's.
    v = {"project": "Mercury", "customer": "Vandelay", "hours": 2}
    assert _run(book, "log_time", v, lambda: book["app"].log_time(v)).startswith(
        "CONFIRMED"
    )
    wrong = {
        "project": "Ranger",
        "customer": "Vandelay",
        "hours": 2,
    }  # Ranger is Stark's
    assert not _run(
        book, "log_time", v, lambda: book["app"].log_time(wrong)
    ).startswith("CONFIRMED")


def test_a_join_only_equal_ids_support_is_asked(book: dict[str, Any]) -> None:
    """The ticket's ext_id equalled its customer's id twice. That join and
    the project's customer both fit; the generator asks."""

    first = book["first"]["kinds"]["open_ticket"]
    tied = first["unresolved"]["customer"]
    assert any("tickets.ext_id -> customers" in t for t in tied), tied
    assert any("projects.customer_id -> customers" in t for t in tied), tied
    assert "locations.open_ticket" in book["questions"]
    v = {"project": "Mercury", "customer": "Vandelay", "title": "Fax"}
    assert _run(
        book, "open_ticket", v, lambda: book["app"].open_ticket(v, 6)
    ).startswith("CONFIRMED")


def test_a_list_items_own_key_is_never_its_link(book: dict[str, Any]) -> None:
    """Each order's one line had the order's id as its own id. The line is
    linked by order_id, so a correct order with two lines is confirmed."""

    v = {"supplier": "Bolt Co", "note": None,
         "lines": [{"part": "M5 bolt", "qty": 5}, {"part": "M6 bolt", "qty": 6}]}  # fmt: skip
    app = book["app"]
    out = _run(book, "create_order", v, lambda: app.order(v, "USD"))
    assert out.startswith("CONFIRMED"), out


def test_a_value_seen_beside_one_write_is_not_pinned(book: dict[str, Any]) -> None:
    """Only the order with a note stored SEK. The generator doesn't pin SEK
    to orders with a note: it asks, and leaves it out of the proposal."""

    assert "orders.currency (seen in one demonstration only)" in book["questions"]
    proposal = book["first"]["questions"][0]["proposal"]
    assert "currency" not in ((proposal or {}).get("unchecked") or {}).get("orders", [])
    assert any(
        "orders.currency" in r
        for r in book["first"]["kinds"]["create_order"]["refused"]
    )
    # Named as unchecked, an order with a note in another currency passes.
    v = {"supplier": "Nut Co", "note": "Rush", "lines": [{"part": "M8 nut", "qty": 3}]}
    out = _run(book, "create_order", v, lambda: book["app"].order(v, "EUR"))
    assert out.startswith("CONFIRMED"), out
