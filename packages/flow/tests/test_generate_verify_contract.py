"""The contract generator on a real PostgreSQL: demonstrations in, contracts out.

A throwaway PostgreSQL (``initdb``) holds a small issue tracker shaped like
Redmine: names behind keys with no declared foreign keys, a person's name in
two columns, notes in a journal table, custom fields in an
entity-attribute-value table, look-alike projects with the same issue
subject. The test records two demonstration writes per kind, lets the
generator propose contracts, then runs correct writes and planted faults
through ``begin_write`` and ``verify_write``. Every correct write must be
CONFIRMED and every fault stopped. Skipped when PostgreSQL isn't installed.
Records are made up.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

psycopg2 = pytest.importorskip("psycopg2")

from openadapt_flow.generate import verify_contract as gen  # noqa: E402

INITDB = shutil.which("initdb") or "/opt/homebrew/bin/initdb"
PG_CTL = shutil.which("pg_ctl") or "/opt/homebrew/bin/pg_ctl"

SCHEMA = """
CREATE TABLE projects (id serial PRIMARY KEY, name text NOT NULL);
CREATE TABLE users (id serial PRIMARY KEY, login text, firstname text, lastname text);
CREATE TABLE statuses (id serial PRIMARY KEY, name text);
CREATE TABLE issues (
  id serial PRIMARY KEY, project_id integer NOT NULL, subject text NOT NULL,
  status_id integer NOT NULL, assigned_to_id integer, due_date date,
  done_ratio integer NOT NULL DEFAULT 0, is_private boolean NOT NULL DEFAULT false,
  author_id integer NOT NULL, created_on timestamp, updated_on timestamp,
  lock_version integer NOT NULL DEFAULT 0);
CREATE TABLE journals (id serial PRIMARY KEY, journalized_id integer,
  journalized_type text, user_id integer, notes text DEFAULT '', created_on timestamp);
CREATE TABLE journal_details (id serial PRIMARY KEY, journal_id integer,
  prop_key text, old_value text, value text);
CREATE TABLE custom_values (id serial PRIMARY KEY, customized_type text,
  customized_id integer, custom_field_id integer, value text);
CREATE TABLE settings (id serial PRIMARY KEY, name text, value text);
INSERT INTO projects (name) VALUES ('Billing Portal'), ('Billing Portal Legacy'), ('Help Desk');
INSERT INTO users (login, firstname, lastname) VALUES ('bot', 'Ops', 'Bot'),
  ('jpark', 'Jon', 'Park'), ('roconnor', 'Renée', 'O''Connor'), ('roconner', 'Renee', 'O''Conner');
INSERT INTO statuses (name) VALUES ('New'), ('In Progress'), ('Resolved');
INSERT INTO issues (project_id, subject, status_id, author_id, created_on, updated_on) VALUES
  (1, 'Refund button missing', 1, 2, '2026-01-01', '2026-01-01'),
  (2, 'Refund button missing', 1, 2, '2026-01-01', '2026-01-01'),
  (3, 'Printer jams', 1, 2, '2026-01-01', '2026-01-01'),
  (3, 'VPN drops', 2, 2, '2026-01-01', '2026-01-01');
INSERT INTO settings (name, value) VALUES ('ui_theme', 'classic');
CREATE ROLE oa_reader LOGIN PASSWORD 'reader-pw';
GRANT SELECT ON ALL TABLES IN SCHEMA public TO oa_reader;
"""

BOT = 1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def pg() -> Any:
    if not (os.path.exists(INITDB) and os.path.exists(PG_CTL)):
        pytest.skip("PostgreSQL (initdb, pg_ctl) is not installed")
    root = Path(tempfile.mkdtemp(prefix="oa-gen-pg-"))
    port = _free_port()
    env = {**os.environ, "LC_ALL": "C"}
    subprocess.run([INITDB, "-D", str(root / "data"), "-U", "admin", "--auth=trust", "-E", "UTF8", "--locale=C"],
                   check=True, capture_output=True, env=env)  # fmt: skip
    subprocess.run(
        [PG_CTL, "-D", str(root / "data"), "-l", str(root / "log"), "-w", "start", "-o",
         f"-p {port} -c unix_socket_directories='' -c listen_addresses=127.0.0.1"],
        check=True, capture_output=True, env=env,
    )  # fmt: skip
    try:
        yield {"root": root, "port": port}
    finally:
        subprocess.run([PG_CTL, "-D", str(root / "data"), "-m", "immediate", "stop"],
                       capture_output=True, env=env)  # fmt: skip
        shutil.rmtree(root, ignore_errors=True)


class App:
    """The tracker's own writes, as its API would make them."""

    def __init__(self, port: int, db: str) -> None:
        self.port, self.db = port, db

    def con(self) -> Any:
        c = psycopg2.connect(host="127.0.0.1", port=self.port, user="admin", dbname=self.db)
        c.autocommit = True
        return c

    def q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self.con() as c, c.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall() if cur.description else []

    def one(self, sql: str, args: tuple = ()) -> Any:
        rows = self.q(sql, args)
        assert len(rows) == 1, (sql, rows)
        return rows[0][0]

    def user(self, name: str) -> int:
        first, last = name.split(" ", 1)
        return self.one("SELECT id FROM users WHERE firstname=%s AND lastname=%s", (first, last))

    def create_issue(self, v: dict[str, Any], *, project_id: Any = None) -> int:
        pid = project_id or self.one("SELECT id FROM projects WHERE name=%s", (v["project"],))
        sid = self.one("SELECT id FROM statuses WHERE name=%s", (v["status"],))
        uid = self.user(v["assignee"]) if v.get("assignee") else None
        iid = self.one(
            "INSERT INTO issues (project_id, subject, status_id, assigned_to_id, due_date, "
            "author_id, created_on, updated_on) VALUES (%s,%s,%s,%s,%s,%s,now(),now()) RETURNING id",
            (pid, v["subject"], sid, uid, v.get("due_date"), BOT),
        )
        if v.get("customer_ref") is not None:
            self.q("INSERT INTO custom_values (customized_type, customized_id, custom_field_id, value) "
                   "VALUES ('Issue', %s, 1, %s)", (iid, v["customer_ref"]))  # fmt: skip
        return iid

    def find(self, project: str, subject: str) -> int:
        return self.one(
            "SELECT i.id FROM issues i JOIN projects p ON p.id=i.project_id "
            "WHERE p.name=%s AND i.subject=%s", (project, subject))  # fmt: skip

    def update_issue(self, iid: int, v: dict[str, Any], *, user: int = BOT) -> None:
        sets, details = [], []
        if v.get("status") is not None:
            sets.append(("status_id", self.one("SELECT id FROM statuses WHERE name=%s", (v["status"],))))
        if v.get("assignee") is not None:
            sets.append(("assigned_to_id", self.user(v["assignee"])))
        if v.get("due_date") is not None:
            sets.append(("due_date", v["due_date"]))
        if v.get("done_ratio") is not None:
            sets.append(("done_ratio", v["done_ratio"]))
        for col, val in sets:
            old = self.one(f"SELECT {col}::text FROM issues WHERE id=%s", (iid,))
            self.q(f"UPDATE issues SET {col}=%s WHERE id=%s", (val, iid))
            details.append((col, old, str(val)))
        self.q("UPDATE issues SET updated_on=now(), lock_version=lock_version+1 WHERE id=%s", (iid,))
        jid = self.one(
            "INSERT INTO journals (journalized_id, journalized_type, user_id, notes, created_on) "
            "VALUES (%s,'Issue',%s,%s,now()) RETURNING id", (iid, user, v.get("notes") or ""))  # fmt: skip
        for col, old, new in details:
            self.q("INSERT INTO journal_details (journal_id, prop_key, old_value, value) "
                   "VALUES (%s,%s,%s,%s)", (jid, col, old, new))  # fmt: skip


KINDS = {
    "kinds": [
        {
            "kind": "create_issue",
            "parameters": ["project", "subject", "status", "assignee", "due_date", "customer_ref"],
            "examples": [
                {"values": {"project": "Billing Portal", "subject": "Tax rounding", "status": "New",
                            "assignee": "Jon Park", "due_date": "2026-11-20", "customer_ref": None}},
                {"values": {"project": "Help Desk", "subject": "Onboard Zoë", "status": "New",
                            "assignee": "Renée O'Connor", "due_date": None, "customer_ref": "007341"}},
            ],
        },
        {
            "kind": "update_issue",
            "parameters": ["project", "subject", "status", "assignee", "due_date", "done_ratio", "notes"],
            "examples": [
                {"values": {"project": "Help Desk", "subject": "Printer jams", "status": "In Progress",
                            "assignee": "Jon Park", "due_date": None, "done_ratio": None, "notes": None}},
                {"values": {"project": "Billing Portal", "subject": "Refund button missing",
                            "status": None, "assignee": None, "due_date": None, "done_ratio": 30,
                            "notes": "Customer confirmed the refund."}},
            ],
        },
    ]
}


@pytest.fixture(scope="module")
def generated(pg: Any) -> dict[str, Any]:
    root, port = pg["root"], pg["port"]
    admin = psycopg2.connect(host="127.0.0.1", port=port, user="admin", dbname="postgres")
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE tracker")
    admin.close()
    app = App(port, "tracker")
    with app.con() as c, c.cursor() as cur:
        cur.execute(SCHEMA)
    os.environ["OA_GEN_TEST_PW"] = "reader-pw"
    work = root / "work"
    work.mkdir()
    read = {"sql_driver": "psycopg2",
            "sql_connect_args": {"host": "127.0.0.1", "port": port, "user": "oa_reader", "dbname": "tracker"},
            "sql_password_env": "OA_GEN_TEST_PW"}  # fmt: skip
    (work / "read.yaml").write_text(yaml.safe_dump(read))
    (work / "kinds.yaml").write_text(yaml.safe_dump(KINDS, allow_unicode=True))
    common = ["--read", str(work / "read.yaml")]
    assert gen.main(["schema", *common]) == 0

    def demo(kind: str, values: dict[str, Any], write: Callable[[], Any]) -> None:
        assert gen.main(["snapshot", *common, "--out", str(work / "before.json")]) == 0
        write()
        assert gen.main(["record", *common, "--before", str(work / "before.json"), "--kind", kind,
                         "--values", json.dumps(values), "--demos", str(work / "demos")]) == 0  # fmt: skip

    for ex in KINDS["kinds"][0]["examples"]:
        demo("create_issue", ex["values"], lambda v=ex["values"]: app.create_issue(v))
    for ex in KINDS["kinds"][1]["examples"]:
        v = ex["values"]
        demo("update_issue", v, lambda v=v: app.update_issue(app.find(v["project"], v["subject"]), v))
    argv = ["propose", *common, "--kinds", str(work / "kinds.yaml"), "--demos", str(work / "demos"),
            "--out", str(work / "out"), "--actor-param", f"bot_user_id={BOT}"]  # fmt: skip
    assert gen.main(argv) == 0
    report = json.loads((work / "out" / "report.json").read_text())
    # The person accepts the proposals.
    answers = {q["key"]: q["proposal"] for q in report["questions"]}
    (work / "answers.yaml").write_text(yaml.safe_dump(answers))
    assert gen.main([*argv, "--answers", str(work / "answers.yaml")]) == 0
    if os.environ.get("OA_GEN_TEST_KEEP"):
        shutil.copytree(work, os.environ["OA_GEN_TEST_KEEP"], dirs_exist_ok=True)
    return {"app": app, "out": work / "out", "report": report, "read": read}


def _run(generated: dict[str, Any], kind: str, values: dict[str, Any], write: Callable[[], Any]) -> str:
    from openadapt_flow.verifier import Actor, begin_write, verify_write

    contract, params = gen.choose(generated["out"] / "manifest.yaml", kind, values, {"bot_user_id": str(BOT)})
    with tempfile.TemporaryDirectory() as tmp:
        pending = begin_write(contract, params, actor=Actor("agent", "ops-bot"), ledger=None,
                              write_key=f"t-{time.time_ns()}", workdir=tmp)  # fmt: skip
        write()
        result = verify_write(pending)
    return getattr(result.outcome, "value", str(result.outcome)) + " " + "; ".join(result.reasons)[:400]


def test_questions_and_contracts(generated: dict[str, Any]) -> None:
    report = generated["report"]
    keys = [q["key"] for q in report["questions"]]
    assert len(keys) <= 3, keys
    kinds = report["kinds"]
    assert kinds["create_issue"]["mode"] == "create"
    assert kinds["update_issue"]["mode"] == "update"
    assert kinds["create_issue"]["emitted"] > 0, kinds["create_issue"]
    # Four change fields the examples give (status, assignee, done ratio,
    # notes): every non-empty subset gets a contract. No example gives a due
    # date, so no contract changes one.
    assert kinds["update_issue"]["emitted"] == 15, json.dumps(kinds["update_issue"], indent=1)
    assert "journal_details" in kinds["update_issue"]["logs"]


def test_a_correct_create_is_confirmed(generated: dict[str, Any]) -> None:
    app = generated["app"]
    v = {"project": "Billing Portal", "subject": "Dark mode", "status": "New",
         "assignee": "Renée O'Connor", "due_date": "2026-12-01", "customer_ref": "0099"}  # fmt: skip
    assert _run(generated, "create_issue", v, lambda: app.create_issue(v)).startswith("CONFIRMED")


def test_found_existing_with_no_write_is_confirmed(generated: dict[str, Any]) -> None:
    app = generated["app"]
    v = {"project": "Help Desk", "subject": "Make sure", "status": "New",
         "assignee": "Jon Park", "due_date": None, "customer_ref": None}  # fmt: skip
    app.create_issue(v)
    assert _run(generated, "create_issue", v, lambda: None).startswith("CONFIRMED")


def test_an_undemonstrated_update_combination_is_confirmed(generated: dict[str, Any]) -> None:
    app = generated["app"]
    v = {"project": "Help Desk", "subject": "VPN drops", "status": "Resolved", "assignee": None,
         "due_date": None, "done_ratio": 80, "notes": "Fixed the tunnel."}  # fmt: skip
    iid = app.find("Help Desk", "VPN drops")
    assert _run(generated, "update_issue", v, lambda: app.update_issue(iid, v)).startswith("CONFIRMED")


CREATE = {"project": "Billing Portal", "subject": "Export CSV", "status": "New",
          "assignee": "Jon Park", "due_date": "2026-12-05", "customer_ref": "0042"}  # fmt: skip
UPDATE = {"project": "Billing Portal", "subject": "Refund button missing", "status": "Resolved",
          "assignee": None, "due_date": None, "done_ratio": None, "notes": "Done."}  # fmt: skip


def _faults(app: App) -> dict[str, tuple[str, dict[str, Any], Callable[[], Any]]]:
    def legacy() -> int:
        return app.find("Billing Portal Legacy", "Refund button missing")

    def target() -> int:
        return app.find("Billing Portal", "Refund button missing")

    return {
        "wrong value": ("create_issue", CREATE, lambda: app.create_issue({**CREATE, "assignee": "Renee O'Conner"})),
        "wrong project": ("create_issue", CREATE, lambda: app.create_issue(CREATE, project_id=2)),
        "duplicate": ("create_issue", CREATE, lambda: (app.create_issue(CREATE), app.create_issue(CREATE))),
        "nothing written": ("create_issue", CREATE, lambda: None),
        "missing custom field": ("create_issue", CREATE, lambda: app.create_issue({**CREATE, "customer_ref": None})),
        "extra issue": ("create_issue", CREATE, lambda: (app.create_issue(CREATE), app.create_issue({**CREATE, "subject": "Follow-up"}))),
        "setting changed": ("create_issue", CREATE, lambda: (app.create_issue(CREATE), app.q("UPDATE settings SET value='dark'"))),
        "look-alike target": ("update_issue", UPDATE, lambda: app.update_issue(legacy(), UPDATE)),
        "both targets": ("update_issue", UPDATE, lambda: (app.update_issue(target(), UPDATE), app.update_issue(legacy(), {"status": "Resolved"}))),
        "note missing": ("update_issue", UPDATE, lambda: app.update_issue(target(), {**UPDATE, "notes": None})),
        "collateral field": ("update_issue", UPDATE, lambda: (app.update_issue(target(), UPDATE), app.q("UPDATE issues SET is_private=true WHERE id=%s", (target(),)))),
        "collateral delete": ("update_issue", UPDATE, lambda: (app.update_issue(target(), UPDATE), app.q("DELETE FROM issues WHERE subject='Printer jams'"))),
        "note by someone else": ("update_issue", UPDATE, lambda: app.update_issue(target(), UPDATE, user=2)),
    }  # fmt: skip


@pytest.mark.parametrize("name", list(_faults(App(0, "")).keys()))
def test_each_fault_is_stopped(generated: dict[str, Any], name: str) -> None:
    app = generated["app"]
    kind, values, write = _faults(app)[name]
    with app.con() as c, c.cursor() as cur:  # each fault starts from the same rows
        cur.execute("BEGIN")
        snapshot = {t: cur.execute(f"SELECT * FROM {t}") or cur.fetchall()
                    for t in ("issues", "journals", "journal_details", "custom_values", "settings")}  # fmt: skip
        cur.execute("COMMIT")
    try:
        outcome = _run(generated, kind, values, write)
        print(f"FAULT {name}: {outcome}")
        assert not outcome.startswith("CONFIRMED"), (name, outcome)
    finally:
        with app.con() as c, c.cursor() as cur:
            for t, rows in snapshot.items():
                cur.execute(f"DELETE FROM {t}")
                for r in rows:
                    cur.execute(f"INSERT INTO {t} VALUES ({', '.join(['%s'] * len(r))})", r)
