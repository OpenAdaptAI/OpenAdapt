# ruff: noqa: F811  (pytest fixtures imported from the generator test, then used as arguments)
"""The adversarial review's attacks on generated contracts (#1672 round 1).

Each test is one way a wrong write could have passed, from the review, run
on the same throwaway PostgreSQL as ``test_generate_verify_contract``. Every
wrong write must now stop, and a correct update of an issue that already has
history must be CONFIRMED. Records are made up.
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
    CREATE,
    KINDS,
    SCHEMA,
    App,
    _run,
    generated,
    pg,
)


def _fresh_issue(app: App, subject: str) -> int:
    return app.create_issue(
        {"project": "Help Desk", "subject": subject, "status": "New",
         "assignee": None, "due_date": None, "customer_ref": None}
    )  # fmt: skip


def test_a1_a_number_written_with_the_same_digits_is_stopped(
    generated: dict[str, Any],
) -> None:
    """The custom field is stored as given, so it is compared exactly; the
    phone rule applies only to a value a person confirmed as a phone."""

    app = generated["app"]
    for asked, written in (("1500.75", "15007.5"), ("2026-1104", "202-611-04")):
        v = {**CREATE, "subject": f"A1 {asked}", "customer_ref": asked}
        out = _run(generated, "create_issue", v,
                   lambda v=v, w=written: app.create_issue({**v, "customer_ref": w}))  # fmt: skip
        assert not out.startswith("CONFIRMED"), out


def test_a2_a_display_name_two_users_share_is_stopped(
    generated: dict[str, Any],
) -> None:
    app = generated["app"]
    other = app.one(
        "INSERT INTO users (login, firstname, lastname) VALUES ('jpark2','Jon','Park') RETURNING id"
    )
    try:
        v = {**CREATE, "subject": "A2 shared name", "assignee": "Jon Park"}

        def write() -> None:
            iid = app.create_issue({**v, "assignee": None})
            app.q("UPDATE issues SET assigned_to_id=%s WHERE id=%s", (other, iid))

        out = _run(generated, "create_issue", v, write)
        assert not out.startswith("CONFIRMED"), out
    finally:
        app.q("UPDATE issues SET assigned_to_id=NULL WHERE assigned_to_id=%s", (other,))
        app.q("DELETE FROM users WHERE id=%s", (other,))


def test_a3_a_first_and_last_name_split_two_ways_is_stopped(
    generated: dict[str, Any],
) -> None:
    app = generated["app"]
    a = app.one(
        "INSERT INTO users (login, firstname, lastname) VALUES ('mal','Mary Ann','Lee') RETURNING id"
    )
    b = app.one(
        "INSERT INTO users (login, firstname, lastname) VALUES ('mal2','Mary','Ann Lee') RETURNING id"
    )
    try:
        v = {**CREATE, "subject": "A3 split", "assignee": "Mary Ann Lee"}

        def write() -> None:
            iid = app.create_issue({**v, "assignee": None})
            app.q("UPDATE issues SET assigned_to_id=%s WHERE id=%s", (b, iid))

        out = _run(generated, "create_issue", v, write)
        assert not out.startswith("CONFIRMED"), out
    finally:
        app.q(
            "UPDATE issues SET assigned_to_id=NULL WHERE assigned_to_id IN (%s,%s)",
            (a, b),
        )
        app.q("DELETE FROM users WHERE id IN (%s,%s)", (a, b))


@pytest.mark.xfail(
    strict=True,
    reason="PostgreSQL has no whole-database frame: a table a person names as a "
    "log is not read, so a rewrite of its existing rows is not seen (documented "
    "in GENERATE_CONTRACTS.md, Limits)",
)
def test_a4_a_rewrite_of_a_log_table_on_postgresql_is_stopped(
    generated: dict[str, Any],
) -> None:
    app = generated["app"]
    iid = _fresh_issue(app, "A4 fresh")
    v = {"project": "Help Desk", "subject": "A4 fresh", "status": "Resolved", "assignee": None,
         "due_date": None, "done_ratio": None, "notes": None}  # fmt: skip
    app.q(
        "INSERT INTO journal_details (journal_id, prop_key, old_value, value) VALUES (999,'status_id','1','2')"
    )

    def write() -> None:
        app.update_issue(iid, v)
        app.q("UPDATE journal_details SET value='999' WHERE journal_id=999")

    out = _run(generated, "update_issue", v, write)
    assert not out.startswith("CONFIRMED"), out


def test_a5_a_correct_update_of_an_issue_with_history_is_confirmed(
    generated: dict[str, Any],
) -> None:
    app = generated["app"]
    iid = _fresh_issue(app, "A5 history")
    first = {"project": "Help Desk", "subject": "A5 history", "status": "In Progress", "assignee": None,
             "due_date": None, "done_ratio": None, "notes": None}  # fmt: skip
    app.update_issue(
        iid, first, user=2
    )  # earlier history, by a colleague, before the window
    v = {**first, "status": "Resolved"}
    out = _run(generated, "update_issue", v, lambda: app.update_issue(iid, v))
    assert out.startswith("CONFIRMED"), out


# -- an app that touches a counter row on each create, and soft-deletes -------------

SCHEMA_B = (
    SCHEMA.replace(
        "lock_version integer NOT NULL DEFAULT 0);",
        "lock_version integer NOT NULL DEFAULT 0, deleted smallint NOT NULL DEFAULT 0);",
    )
    .replace(
        "INSERT INTO settings (name, value) VALUES ('ui_theme', 'classic');",
        "INSERT INTO settings (name, value) VALUES ('ui_theme', 'classic'), ('last_issue', '0');",
    )
    .replace("CREATE ROLE oa_reader LOGIN PASSWORD 'reader-pw';", "")
)


class AppB(App):
    def create_issue(self, v: dict[str, Any], *, project_id: Any = None) -> int:
        iid = super().create_issue(v, project_id=project_id)
        self.q("UPDATE settings SET value=%s WHERE name='last_issue'", (str(iid),))
        return iid


@pytest.fixture(scope="module")
def gen_b(pg: Any) -> dict[str, Any]:
    root, port = pg["root"], pg["port"]
    admin = psycopg2.connect(
        host="127.0.0.1", port=port, user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE trackerb")
    admin.close()
    app = AppB(port, "trackerb")
    with app.con() as c, c.cursor() as cur:
        cur.execute(
            "DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='oa_reader') "
            "THEN CREATE ROLE oa_reader LOGIN PASSWORD 'reader-pw'; END IF; END$$"
        )
        cur.execute(SCHEMA_B)
    os.environ["OA_GEN_TEST_PW"] = "reader-pw"
    work = root / "workb"
    work.mkdir()
    read = {"sql_driver": "psycopg2",
            "sql_connect_args": {"host": "127.0.0.1", "port": port, "user": "oa_reader", "dbname": "trackerb"},
            "sql_password_env": "OA_GEN_TEST_PW"}  # fmt: skip
    (work / "read.yaml").write_text(yaml.safe_dump(read))
    kinds = {"kinds": [KINDS["kinds"][0]]}
    (work / "kinds.yaml").write_text(yaml.safe_dump(kinds, allow_unicode=True))
    common = ["--read", str(work / "read.yaml")]
    for ex in kinds["kinds"][0]["examples"]:
        assert gen.main(["snapshot", *common, "--out", str(work / "before.json")]) == 0
        app.create_issue(ex["values"])
        assert gen.main(["record", *common, "--before", str(work / "before.json"), "--kind", "create_issue",
                         "--values", json.dumps(ex["values"]), "--demos", str(work / "demos")]) == 0  # fmt: skip
    argv = ["propose", *common, "--kinds", str(work / "kinds.yaml"), "--demos", str(work / "demos"),
            "--out", str(work / "out"), "--actor-param", f"bot_user_id={BOT}"]  # fmt: skip
    assert gen.main(argv) == 0
    report = json.loads((work / "out" / "report.json").read_text())
    answers = {q["key"]: q["proposal"] for q in report["questions"]}
    (work / "answers.yaml").write_text(yaml.safe_dump(answers))
    assert gen.main([*argv, "--answers", str(work / "answers.yaml")]) == 0
    report = json.loads((work / "out" / "report.json").read_text())
    return {"app": app, "out": work / "out", "report": report, "read": read}


def test_b0_the_counter_row_is_excused_by_key_and_a_correct_create_passes(
    gen_b: dict[str, Any],
) -> None:
    rows = gen_b["report"]["kinds"]["create_issue"]["touched_rows"]
    assert [(r["table"], r["columns"]) for r in rows] == [
        ("settings", {"value": "counter"})
    ]
    app = gen_b["app"]
    v = {**CREATE, "subject": "B0 correct"}
    out = _run(gen_b, "create_issue", v, lambda: app.create_issue(v))
    assert out.startswith("CONFIRMED"), out


def test_b1_another_setting_changed_is_stopped(gen_b: dict[str, Any]) -> None:
    app = gen_b["app"]
    v = {**CREATE, "subject": "B1"}
    try:
        out = _run(gen_b, "create_issue", v, lambda: (
            app.create_issue(v), app.q("UPDATE settings SET value='dark' WHERE name='ui_theme'")))  # fmt: skip
    finally:
        app.q("UPDATE settings SET value='classic' WHERE name='ui_theme'")
    assert not out.startswith("CONFIRMED"), out


def test_b2_another_records_custom_field_changed_is_stopped(
    gen_b: dict[str, Any],
) -> None:
    app = gen_b["app"]
    victim = app.create_issue({**CREATE, "subject": "victim", "customer_ref": "777"})
    v = {**CREATE, "subject": "B2"}
    out = _run(gen_b, "create_issue", v, lambda: (
        app.create_issue(v),
        app.q("UPDATE custom_values SET value='HIJACKED' WHERE customized_id=%s", (victim,))))  # fmt: skip
    assert not out.startswith("CONFIRMED"), out


def test_b3_a_trashed_record_with_the_values_is_not_found(
    gen_b: dict[str, Any],
) -> None:
    app = gen_b["app"]
    v = {**CREATE, "subject": "B3 trashed"}
    iid = app.create_issue(v)
    app.q("UPDATE issues SET deleted=1 WHERE id=%s", (iid,))
    out = _run(gen_b, "create_issue", v, lambda: None)
    assert not out.startswith("CONFIRMED"), out


def test_b4_a_create_that_writes_deleted_is_stopped(gen_b: dict[str, Any]) -> None:
    app = gen_b["app"]
    v = {**CREATE, "subject": "B4"}

    def write() -> None:
        iid = app.create_issue(v)
        app.q("UPDATE issues SET deleted=1 WHERE id=%s", (iid,))

    out = _run(gen_b, "create_issue", v, write)
    assert not out.startswith("CONFIRMED"), out
