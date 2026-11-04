# ruff: noqa: F811  (pytest fixtures imported from the generator test, then used as arguments)
"""The adversarial review's round-3 attacks on generated contracts (#1672).

The round-2 tracker with no version column, so updates read their versions
from the audit trail, on a throwaway PostgreSQL. The person's answers are
the proposal plus ``logs_add: [audits]``. Every wrong write must stop; the
correct writes beside them must be CONFIRMED. Also: the PostgreSQL read-only
check counts roles reachable with SET ROLE and column grants. Records are
made up.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
import yaml

psycopg2 = pytest.importorskip("psycopg2")

from openadapt_flow.generate import verify_contract as gen  # noqa: E402
from tests.test_generate_verify_contract import BOT, _run, pg  # noqa: E402,F401
from tests.test_generate_verify_contract_attacks_r2 import (  # noqa: E402
    KINDS_V,
    SCHEMA_V,
    AppV,
)


class AppA(AppV):
    """Logs each save under the given event code (a bulk edit, say)."""

    def save(
        self, iid: int, sets: dict[str, Any], *, user: int = BOT, action: str = "update"
    ) -> None:
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
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (iid, user, action, col, old, None if val is None else f"v{val}"),
            )
        self.q("UPDATE issues SET updated_on=now() WHERE id=%s", (iid,))
        self.q("UPDATE projects SET updated_on=now() WHERE id=(SELECT project_id FROM issues WHERE id=%s)",
               (iid,))  # fmt: skip


@pytest.fixture(scope="module")
def ga(pg: Any) -> dict[str, Any]:
    root, port = pg["root"], pg["port"]
    admin = psycopg2.connect(
        host="127.0.0.1", port=port, user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE tracker3")
    admin.close()
    app = AppA(port, "tracker3")
    with app.con() as c, c.cursor() as cur:
        cur.execute(
            "DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='oa_reader') "
            "THEN CREATE ROLE oa_reader LOGIN PASSWORD 'reader-pw'; END IF; END$$"
        )
        cur.execute(SCHEMA_V)
    os.environ["OA_GEN_TEST_PW"] = "reader-pw"
    work = root / "work3"
    work.mkdir()
    read = {"sql_driver": "psycopg2",
            "sql_connect_args": {"host": "127.0.0.1", "port": port, "user": "oa_reader", "dbname": "tracker3"},
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
    bk = answers["bookkeeping"]
    bk = {"accept": True} if bk == "accept" else dict(bk)
    bk["logs_add"] = ["audits"]
    answers["bookkeeping"] = bk
    (work / "answers.yaml").write_text(yaml.safe_dump(answers))
    assert gen.main([*argv, "--answers", str(work / "answers.yaml")]) == 0
    report = json.loads((work / "out" / "report.json").read_text())
    return {"app": app, "out": work / "out", "report": report, "read": read,
            "answers": answers}  # fmt: skip


def _fresh(app: AppA, subject: str) -> int:
    return app.create_issue(
        {"project": "Help Desk", "subject": subject, "status": "New"}
    )


# -- SF1: free text every demonstration left empty stays empty -------------------


def test_proposal_leaves_free_text_out(ga: dict[str, Any]) -> None:
    unchecked = ga["answers"]["bookkeeping"].get("unchecked", {})
    assert "description" not in unchecked.get("issues", []), unchecked


def test_correct_create_confirmed(ga: dict[str, Any]) -> None:
    app = ga["app"]
    v = {
        "project": "Help Desk",
        "subject": "P0 child",
        "status": "New",
        "parent": "VPN drops",
    }
    out = _run(ga, "create_issue", v, lambda: app.create_issue(v))
    assert out.startswith("CONFIRMED"), out


def test_create_that_also_fills_an_empty_description_stops(ga: dict[str, Any]) -> None:
    app = ga["app"]
    v = {
        "project": "Help Desk",
        "subject": "A1 child",
        "status": "New",
        "parent": "VPN drops",
    }

    def write() -> None:
        iid = app.create_issue(v)
        app.q(
            "UPDATE issues SET description='Refund approved, wire to acct 99' WHERE id=%s",
            (iid,),
        )

    out = _run(ga, "create_issue", v, write)
    assert not out.startswith("CONFIRMED"), out


# -- SF6.1: the audit table's own key never names the record ---------------------


def test_versions_come_from_the_audit_trail_for_every_update_kind(
    ga: dict[str, Any],
) -> None:
    for kind in ("set_status", "edit_description"):
        v = ga["report"]["kinds"][kind]["versions"]
        assert v and v["table"] == "audits" and v["record_column"] == "issue_id", (
            kind,
            v,
        )


def test_lost_update_of_another_field_stops(ga: dict[str, Any]) -> None:
    app = ga["app"]
    iid = _fresh(app, "AS1")
    v = {"project": "Help Desk", "subject": "AS1", "status": "Resolved"}

    def write() -> None:
        app.save(iid, {"due_date": "2026-12-31"}, user=2)
        app.save(iid, {"status": "Resolved", "due_date": None})

    out = _run(ga, "set_status", v, write)
    assert not out.startswith("CONFIRMED"), out


def test_lost_update_of_the_same_field_stops(ga: dict[str, Any]) -> None:
    app = ga["app"]
    iid = _fresh(app, "AS2")
    v = {"project": "Help Desk", "subject": "AS2", "status": "Resolved"}

    def write() -> None:
        app.save(iid, {"status": "In Progress"}, user=2)
        app.save(iid, {"status": "Resolved"})

    out = _run(ga, "set_status", v, write)
    assert not out.startswith("CONFIRMED"), out


# -- SF3: an event code no demonstration showed counts as a save -----------------


def test_no_code_is_shown_to_be_a_read_so_nothing_is_filtered(
    ga: dict[str, Any],
) -> None:
    v = ga["report"]["kinds"]["edit_description"]["versions"]
    assert v["where"] == {}, v


def test_correct_description_edit_confirmed(ga: dict[str, Any]) -> None:
    app = ga["app"]
    iid = _fresh(app, "A2 ctrl")
    v = {"project": "Help Desk", "subject": "A2 ctrl", "description": "Fixed"}
    out = _run(
        ga, "edit_description", v, lambda: app.save(iid, {"description": "Fixed"})
    )
    assert out.startswith("CONFIRMED"), out


def test_lost_update_under_an_unseen_code_stops(ga: dict[str, Any]) -> None:
    app = ga["app"]
    iid = _fresh(app, "A2C1")
    v = {"project": "Help Desk", "subject": "A2C1", "description": "Fixed"}

    def write() -> None:
        app.save(iid, {"due_date": "2026-12-31"}, user=2, action="bulk_update")
        app.save(iid, {"description": "Fixed", "due_date": None})

    out = _run(ga, "edit_description", v, write)
    assert not out.startswith("CONFIRMED"), out


def test_same_field_lost_update_under_an_unseen_code_stops(ga: dict[str, Any]) -> None:
    app = ga["app"]
    iid = _fresh(app, "A2C2")
    v = {"project": "Help Desk", "subject": "A2C2", "description": "Fixed"}

    def write() -> None:
        app.save(iid, {"description": "Theirs"}, user=2, action="bulk_update")
        app.save(iid, {"description": "Fixed"})

    out = _run(ga, "edit_description", v, write)
    assert not out.startswith("CONFIRMED"), out


def test_versions_filter_drops_only_a_code_shown_to_be_a_read(
    ga: dict[str, Any],
) -> None:
    """A read code is one the demonstrations logged on a record they didn't
    change; the read keeps NULL codes and every other code."""
    frame = gen._versions_frame(
        gen.SqlBuilder(_schema(ga)),
        _plan_with_where({"action": {"not_in": ["read"]}}),
        _settings(ga),
    )
    assert frame is not None
    sql = frame["read"]["sql_query"]
    assert "IS NULL OR" in sql and "NOT IN ('read')" in sql, sql


def _schema(ga: dict[str, Any]) -> Any:
    from openadapt_flow.generate import sql_store as store

    return store.read_schema(store.connector(ga["read"]), "postgresql")


def _plan_with_where(where: dict[str, Any]) -> Any:
    plan = gen.KindPlan("edit_description")
    plan.anchor = "issues"
    plan.versions = {"kind": "audit", "table": "audits", "record_column": "issue_id",
                     "author_column": "user_id", "actor_param": "bot_user_id", "where": where}  # fmt: skip
    return plan


def _settings(ga: dict[str, Any]) -> Any:
    return gen.Settings(
        dict(ga["read"]), {"bot_user_id": str(BOT)}, 300, "test-environment"
    )


# -- SF4: PostgreSQL roles reachable with SET ROLE, and column grants -------------


def test_pg_read_only_check_counts_set_role_and_column_grants(pg: Any) -> None:
    port = pg["port"]
    admin = psycopg2.connect(
        host="127.0.0.1", port=port, user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE rotest3")
    admin.close()
    a = psycopg2.connect(host="127.0.0.1", port=port, user="admin", dbname="rotest3")
    a.autocommit = True
    a.cursor().execute(
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC; CREATE TABLE t (id serial primary key, v text); "
        "CREATE ROLE w3; GRANT INSERT, UPDATE ON t TO w3; "
        "CREATE ROLE ro3 LOGIN NOINHERIT; GRANT SELECT ON t TO ro3; GRANT w3 TO ro3; "
        "CREATE ROLE ro4 LOGIN; GRANT SELECT ON t TO ro4; GRANT UPDATE (v) ON t TO ro4; "
        "CREATE ROLE ro6 LOGIN; GRANT SELECT ON t TO ro6; "
        "REVOKE CREATE ON DATABASE rotest3 FROM PUBLIC; REVOKE TEMP ON DATABASE rotest3 FROM PUBLIC"
    )  # fmt: skip
    a.close()

    def check(role: str) -> list[str]:
        return gen.check_read_only(
            lambda: psycopg2.connect(
                host="127.0.0.1", port=port, user=role, dbname="rotest3"
            ),
            "postgresql",
        )

    ro3 = check("ro3")
    assert any("(as w3) has INSERT on table public.t" in p for p in ro3), ro3
    ro4 = check("ro4")
    assert any("has UPDATE on table public.t" in p for p in ro4), ro4
    assert check("ro6") == []
