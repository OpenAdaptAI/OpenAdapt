# ruff: noqa: F811  (pytest fixtures imported from the generator test, then used as arguments)
"""The adversarial review's round-2 attacks on generated contracts (#1672).

An issue tracker that audits one row per changed field and touches the
issue's project on every save, on a throwaway PostgreSQL. Every wrong write
must stop; the correct writes beside them must be CONFIRMED. Also: the
bookkeeping answer can name only what its question offered, and the
PostgreSQL read-only check counts inherited and non-DML privileges. Records
are made up.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
import yaml

psycopg2 = pytest.importorskip("psycopg2")

from openadapt_flow.generate import verify_contract as gen  # noqa: E402
from tests.test_generate_verify_contract import (  # noqa: E402,F401
    BOT,
    SCHEMA,
    App,
    _run,
    pg,
)

SCHEMA_V = (
    SCHEMA.replace(
        "CREATE TABLE projects (id serial PRIMARY KEY, name text NOT NULL);",
        "CREATE TABLE projects (id serial PRIMARY KEY, name text NOT NULL, "
        "description text, updated_on timestamp);",
    )
    .replace(
        # No version column: the versions come from the audit trail.
        "lock_version integer NOT NULL DEFAULT 0);",
        "description text, parent_id integer);",
    )
    .replace(
        "CREATE TABLE settings",
        "CREATE TABLE audits (id serial PRIMARY KEY, issue_id integer, user_id integer, "
        "action text, field_name text, old_value text, new_value text);\nCREATE TABLE settings",
    )
    .replace("CREATE ROLE oa_reader LOGIN PASSWORD 'reader-pw';", "")
)


class AppV(App):
    """Audits one row per changed field; touches the project on every save
    (a parent ``touch``); writes no journals and has no version column."""

    def create_issue(self, v: dict[str, Any], *, project_id: Any = None) -> int:
        pid = project_id or self.one(
            "SELECT id FROM projects WHERE name=%s", (v["project"],)
        )
        sid = self.one("SELECT id FROM statuses WHERE name=%s", (v["status"],))
        parent = None
        if v.get("parent"):
            parent = v.get("parent_id") or self.one(
                "SELECT i.id FROM issues i WHERE i.subject=%s ORDER BY id LIMIT 1",
                (v["parent"],),
            )
        iid = self.one(
            "INSERT INTO issues (project_id, subject, status_id, author_id, created_on, updated_on, parent_id) "
            "VALUES (%s,%s,%s,%s,now(),now(),%s) RETURNING id",
            (pid, v["subject"], sid, BOT, parent),
        )
        self.q(
            "INSERT INTO audits (issue_id, user_id, action) VALUES (%s,%s,'create')",
            (iid, BOT),
        )
        self.q("UPDATE projects SET updated_on=now() WHERE id=%s", (pid,))
        return iid  # fmt: skip

    def save(self, iid: int, sets: dict[str, Any], *, user: int = BOT) -> None:
        for col, val in sets.items():
            if col == "status":
                col, val = (
                    "status_id",
                    self.one("SELECT id FROM statuses WHERE name=%s", (val,)),
                )
            old = self.one(f"SELECT {col}::text FROM issues WHERE id=%s", (iid,))
            self.q(f"UPDATE issues SET {col}=%s WHERE id=%s", (val, iid))
            self.q(
                "INSERT INTO audits (issue_id, user_id, action, field_name, old_value, new_value) "
                "VALUES (%s,%s,'update',%s,%s,%s)",
                (iid, user, col, old, None if val is None else f"v{val}"),
            )
        self.q("UPDATE issues SET updated_on=now() WHERE id=%s", (iid,))
        self.q("UPDATE projects SET updated_on=now() WHERE id=(SELECT project_id FROM issues WHERE id=%s)",
               (iid,))  # fmt: skip


KINDS_V = {
    "kinds": [
        {"kind": "create_issue", "parameters": ["project", "subject", "status", "parent"],
         "examples": [
             {"values": {"project": "Help Desk", "subject": "Toner low", "status": "New", "parent": "Printer jams"}},
             {"values": {"project": "Billing Portal", "subject": "VPN cert", "status": "New", "parent": "VPN drops"}},
         ]},
        {"kind": "set_status", "parameters": ["project", "subject", "status"],
         "examples": [
             {"values": {"project": "Help Desk", "subject": "Printer jams", "status": "In Progress"}},
             {"values": {"project": "Help Desk", "subject": "VPN drops", "status": "Resolved"}},
         ]},
        {"kind": "edit_description", "parameters": ["project", "subject", "description"],
         "examples": [
             {"values": {"project": "Help Desk", "subject": "VPN drops", "description": "Drops every hour"}},
             {"values": {"project": "Billing Portal", "subject": "Refund button missing",
                         "description": "Only on Safari"}},
         ]},
    ]
}  # fmt: skip


def _propose(work: Any, argv: list[str], answers: Any) -> int:
    (work / "answers.yaml").write_text(yaml.safe_dump(answers))
    return gen.main([*argv, "--answers", str(work / "answers.yaml")])


@pytest.fixture(scope="module")
def gv(pg: Any) -> dict[str, Any]:
    root, port = pg["root"], pg["port"]
    admin = psycopg2.connect(
        host="127.0.0.1", port=port, user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE trackerv")
    admin.close()
    app = AppV(port, "trackerv")
    with app.con() as c, c.cursor() as cur:
        cur.execute(
            "DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='oa_reader') "
            "THEN CREATE ROLE oa_reader LOGIN PASSWORD 'reader-pw'; END IF; END$$"
        )
        cur.execute(SCHEMA_V)
    os.environ["OA_GEN_TEST_PW"] = "reader-pw"
    work = root / "workv"
    work.mkdir()
    read = {"sql_driver": "psycopg2",
            "sql_connect_args": {"host": "127.0.0.1", "port": port, "user": "oa_reader", "dbname": "trackerv"},
            "sql_password_env": "OA_GEN_TEST_PW"}  # fmt: skip
    (work / "read.yaml").write_text(yaml.safe_dump(read))
    (work / "kinds.yaml").write_text(yaml.safe_dump(KINDS_V, allow_unicode=True))
    common = ["--read", str(work / "read.yaml")]

    def demo(kind: str, values: dict[str, Any], write: Any) -> None:
        assert gen.main(["snapshot", *common, "--out", str(work / "before.json")]) == 0
        write()
        assert gen.main(["record", *common, "--before", str(work / "before.json"), "--kind", kind,
                         "--values", json.dumps(values), "--demos", str(work / "demos")]) == 0  # fmt: skip

    for k in KINDS_V["kinds"]:
        for ex in k["examples"]:
            v = ex["values"]
            if k["kind"] == "create_issue":
                demo("create_issue", v, lambda v=v: app.create_issue(v))
            else:
                sets = {p: v[p] for p in k["parameters"][2:] if v.get(p) is not None}
                demo(
                    k["kind"],
                    v,
                    lambda v=v, s=sets: app.save(
                        app.find(v["project"], v["subject"]), s
                    ),
                )
    argv = ["propose", *common, "--kinds", str(work / "kinds.yaml"), "--demos", str(work / "demos"),
            "--out", str(work / "out"), "--actor-param", f"bot_user_id={BOT}"]  # fmt: skip
    assert gen.main(argv) == 0
    report = json.loads((work / "out" / "report.json").read_text())
    answers = {q["key"]: q["proposal"] for q in report["questions"]}
    assert _propose(work, argv, answers) == 0
    report = json.loads((work / "out" / "report.json").read_text())
    return {"app": app, "out": work / "out", "report": report, "read": read,
            "work": work, "argv": argv, "answers": answers}  # fmt: skip


def _fresh(app: AppV, subject: str) -> int:
    return app.create_issue(
        {"project": "Help Desk", "subject": subject, "status": "New"}
    )


def test_versions_read_write_events_of_creates_and_updates(gv: dict[str, Any]) -> None:
    v = gv["report"]["kinds"]["edit_description"]["versions"]
    assert v["kind"] == "audit" and v["table"] == "audits", v
    # No demonstration logged a code on a record it didn't change, so no
    # code is shown to be a read: every entry counts as a save. A column
    # that varies with the fields saved (field_name) is never a filter.
    assert v["where"] == {}, v


def test_correct_status_update_confirmed(gv: dict[str, Any]) -> None:
    app = gv["app"]
    iid = _fresh(app, "V1 control")
    v = {"project": "Help Desk", "subject": "V1 control", "status": "Resolved"}
    out = _run(gv, "set_status", v, lambda: app.save(iid, {"status": "Resolved"}))
    assert out.startswith("CONFIRMED"), out


def test_correct_description_edit_confirmed(gv: dict[str, Any]) -> None:
    app = gv["app"]
    iid = _fresh(app, "T1 control")
    v = {"project": "Help Desk", "subject": "T1 control", "description": "Fixed"}
    out = _run(
        gv, "edit_description", v, lambda: app.save(iid, {"description": "Fixed"})
    )
    assert out.startswith("CONFIRMED"), out


def test_lost_update_of_another_field_stops(gv: dict[str, Any]) -> None:
    """Another user sets the due date; the agent's stale save puts it back."""
    app = gv["app"]
    iid = _fresh(app, "V4 lost update")
    v = {"project": "Help Desk", "subject": "V4 lost update", "description": "Fixed"}

    def write() -> None:
        app.save(iid, {"due_date": "2026-12-31"}, user=2)
        app.save(iid, {"description": "Fixed", "due_date": None})

    out = _run(gv, "edit_description", v, write)
    assert not out.startswith("CONFIRMED"), out


def test_lost_update_of_the_same_field_stops(gv: dict[str, Any]) -> None:
    app = gv["app"]
    iid = _fresh(app, "V5")
    v = {"project": "Help Desk", "subject": "V5", "description": "Fixed"}

    def write() -> None:
        app.save(iid, {"description": "Theirs"}, user=2)
        app.save(iid, {"description": "Fixed"})

    out = _run(gv, "edit_description", v, write)
    assert not out.startswith("CONFIRMED"), out


def test_target_without_a_baseline_version_stops(gv: dict[str, Any]) -> None:
    """An issue with no audit row at all (written outside the app) can't be
    shown to be new: with no version before the write it stops as missing."""
    app = gv["app"]
    iid = _fresh(app, "No history")
    app.q("DELETE FROM audits WHERE issue_id=%s", (iid,))
    v = {"project": "Help Desk", "subject": "No history", "description": "Fixed"}
    out = _run(
        gv, "edit_description", v, lambda: app.save(iid, {"description": "Fixed"})
    )
    assert not out.startswith("CONFIRMED"), out


def test_touched_parent_row_cant_take_the_targets_field(gv: dict[str, Any]) -> None:
    app = gv["app"]
    iid = _fresh(app, "T2 leak")
    v = {"project": "Help Desk", "subject": "T2 leak", "description": "Fixed"}

    def write() -> None:
        app.save(iid, {"description": "Fixed"})
        app.q(
            "UPDATE projects SET description='Overwritten by the agent' WHERE name='Help Desk'"
        )

    try:
        out = _run(gv, "edit_description", v, write)
    finally:
        app.q("UPDATE projects SET description=NULL WHERE name='Help Desk'")
    assert not out.startswith("CONFIRMED"), out


def test_target_cant_take_the_touched_rows_field(gv: dict[str, Any]) -> None:
    """A column nothing excuses on the target (is_private) stops, beside a
    correct save."""
    app = gv["app"]
    iid = _fresh(app, "T4")
    v = {"project": "Help Desk", "subject": "T4", "description": "Fixed"}

    def write() -> None:
        app.save(iid, {"description": "Fixed"})
        app.q("UPDATE issues SET is_private=true WHERE id=%s", (iid,))

    out = _run(gv, "edit_description", v, write)
    assert not out.startswith("CONFIRMED"), out


def test_correct_create_with_parent_confirmed(gv: dict[str, Any]) -> None:
    app = gv["app"]
    v = {
        "project": "Help Desk",
        "subject": "P0 child",
        "status": "New",
        "parent": "VPN drops",
    }
    out = _run(gv, "create_issue", v, lambda: app.create_issue(v))
    assert out.startswith("CONFIRMED"), out


def test_parent_named_by_a_subject_two_issues_share_stops(gv: dict[str, Any]) -> None:
    """'Refund button missing' names two issues: a key back to the main table
    must name exactly one row too."""
    app = gv["app"]
    legacy = app.find("Billing Portal Legacy", "Refund button missing")
    v = {
        "project": "Help Desk",
        "subject": "P1 child",
        "status": "New",
        "parent": "Refund button missing",
    }
    out = _run(
        gv, "create_issue", v, lambda: app.create_issue({**v, "parent_id": legacy})
    )
    assert not out.startswith("CONFIRMED"), out


# -- answers name only what the question offered --------------------------------


def _offered(gv: dict[str, Any]) -> Any:
    return dict(gv["answers"])


def test_answer_with_an_unknown_key_is_refused(gv: dict[str, Any], capsys: Any) -> None:
    answers = _offered(gv)
    answers["bookkeeping"] = {"accept": True, "also_excuse": {"issues": ["subject"]}}
    assert _propose(gv["work"], gv["argv"], answers) != 0
    assert "unknown answer keys" in capsys.readouterr().err


def test_logs_add_of_an_unlisted_table_is_refused(
    gv: dict[str, Any], capsys: Any
) -> None:
    answers = _offered(gv)
    answers["bookkeeping"] = {"accept": True, "logs_add": ["issues"]}
    assert _propose(gv["work"], gv["argv"], answers) != 0
    assert "logs_add names tables" in capsys.readouterr().err


def test_columns_add_of_an_unlisted_column_is_refused(
    gv: dict[str, Any], capsys: Any
) -> None:
    answers = _offered(gv)
    answers["bookkeeping"] = {"accept": True, "columns_add": {"issues": ["subject"]}}
    assert _propose(gv["work"], gv["argv"], answers) != 0
    assert "columns_add names" in capsys.readouterr().err


def test_a_mapping_without_accept_takes_no_proposed_column(gv: dict[str, Any]) -> None:
    """``{logs_add: [...]}`` alone doesn't accept the proposed bookkeeping:
    the save's updated_on then isn't excused and a correct update stops."""
    answers = _offered(gv)
    answers["bookkeeping"] = {"logs_add": ["audits"]}
    assert _propose(gv["work"], gv["argv"], answers) == 0
    try:
        app = gv["app"]
        iid = _fresh(app, "No accept")
        v = {"project": "Help Desk", "subject": "No accept", "status": "Resolved"}
        out = _run(gv, "set_status", v, lambda: app.save(iid, {"status": "Resolved"}))
        assert not out.startswith("CONFIRMED"), out
    finally:
        assert _propose(gv["work"], gv["argv"], gv["answers"]) == 0


def test_unnamed_unchecked_columns_refuse_the_kind(gv: dict[str, Any]) -> None:
    """A new row's time stamp named nowhere: no contract, so the write stops."""
    answers = _offered(gv)
    proposal = answers["bookkeeping"]
    assert isinstance(proposal, dict) and proposal.get("unchecked"), proposal
    answers["bookkeeping"] = {"accept": True}
    try:
        assert _propose(gv["work"], gv["argv"], answers) == 0
        report = json.loads((gv["out"] / "report.json").read_text())
        refused = " ".join(report["kinds"]["create_issue"]["refused"])
        assert "bookkeeping.unchecked" in refused, report["kinds"]["create_issue"]
        app = gv["app"]
        v = {
            "project": "Help Desk",
            "subject": "No contract",
            "status": "New",
            "parent": "VPN drops",
        }
        with pytest.raises(gen.GenerateError):
            _run(gv, "create_issue", v, lambda: app.create_issue(v))
    finally:
        assert _propose(gv["work"], gv["argv"], gv["answers"]) == 0


# -- PostgreSQL read-only check ----------------------------------------------------


def test_pg_read_only_check_counts_inherited_and_non_dml_privileges(pg: Any) -> None:
    port = pg["port"]
    admin = psycopg2.connect(
        host="127.0.0.1", port=port, user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE rotest")
    admin.close()
    a = psycopg2.connect(host="127.0.0.1", port=port, user="admin", dbname="rotest")
    a.autocommit = True
    a.cursor().execute(
        "CREATE TABLE t (id serial primary key, v text); CREATE ROLE writers; "
        "GRANT INSERT, UPDATE, DELETE ON t TO writers; "
        "CREATE ROLE ro1 LOGIN; GRANT SELECT ON t TO ro1; GRANT writers TO ro1; "
        "CREATE ROLE ro2 LOGIN; GRANT SELECT, TRIGGER ON t TO ro2; GRANT CREATE ON SCHEMA public TO ro2; "
        "CREATE ROLE ro3 LOGIN; GRANT SELECT ON t TO ro3; GRANT SELECT ON t_id_seq TO ro3;"
    )  # fmt: skip
    a.close()

    def check(role: str) -> list[str]:
        return gen.check_read_only(
            lambda: psycopg2.connect(
                host="127.0.0.1", port=port, user=role, dbname="rotest"
            ),
            "postgresql",
        )

    ro1 = check("ro1")
    assert any("INSERT on table public.t" in p for p in ro1), ro1
    ro2 = check("ro2")
    assert any("TRIGGER on table public.t" in p for p in ro2), ro2
    assert any("CREATE on schema public" in p for p in ro2), ro2
    assert check("ro3") == []
