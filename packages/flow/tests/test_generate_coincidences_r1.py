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
GRANT SELECT ON ALL TABLES IN SCHEMA public TO oa_reader;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO oa_reader;
"""


class Shop(App):
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
    for v in TASK:
        demo("create_task", v, lambda v=v: app.task(v))
    argv = ["propose", *common, "--kinds", str(work / "kinds.yaml"), "--demos", str(work / "demos"),
            "--out", str(work / "out"), "--actor-param", f"bot_user_id={BOT}"]  # fmt: skip
    assert gen.main(argv) == 0
    first = json.loads((work / "out" / "report.json").read_text())
    questions = (work / "out" / "QUESTIONS.md").read_text()
    answers = {q["key"]: q["proposal"] for q in first["questions"]}
    (work / "answers.yaml").write_text(yaml.safe_dump(answers))
    assert gen.main([*argv, "--answers", str(work / "answers.yaml")]) == 0
    report = json.loads((work / "out" / "report.json").read_text())
    return {"app": app, "out": work / "out", "report": report, "questions": questions}


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
