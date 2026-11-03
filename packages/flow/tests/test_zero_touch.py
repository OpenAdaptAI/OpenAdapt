"""Zero-touch setup on a real PostgreSQL: no person answers, a sandbox decides.

The issue tracker from ``test_generate_verify_contract`` (names behind keys,
a journal, custom fields, look-alike projects). Demonstrations train the
generator; other correct writes are held out. ``zero_touch.onboard`` answers
every question by its rules, plants faults in a sandbox schema, and admits a
kind only when every held-out write is confirmed and every fault stopped.
The app's own tables must not change. Skipped without PostgreSQL. Records
are made up.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

psycopg2 = pytest.importorskip("psycopg2")

from openadapt_flow.generate import verify_contract as gen  # noqa: E402
from openadapt_flow.generate import zero_touch as zt  # noqa: E402
from tests.test_generate_verify_contract import BOT, KINDS, SCHEMA, App, pg  # noqa: E402,F401

HOLDOUT = {
    "create_issue": [
        {"project": "Help Desk", "subject": "Badge reader offline", "status": "In Progress",
         "assignee": "Jon Park", "due_date": "2026-12-01", "customer_ref": None},
    ],
    "update_issue": [
        {"project": "Help Desk", "subject": "VPN drops", "status": "Resolved",
         "assignee": None, "due_date": None, "done_ratio": None, "notes": None},
    ],
}  # fmt: skip


def _digest(app: App) -> str:
    h = hashlib.sha256()
    for (t,) in app.q(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' ORDER BY table_name"
    ):
        h.update(json.dumps(app.q(f"SELECT * FROM {t} ORDER BY 1"), default=str).encode())
    return h.hexdigest()


@pytest.fixture(scope="module")
def onboarded(pg: Any) -> dict[str, Any]:  # noqa: F811
    root, port = pg["root"], pg["port"]
    admin = psycopg2.connect(host="127.0.0.1", port=port, user="admin", dbname="postgres")
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE tracker_zt")
    admin.close()
    app = App(port, "tracker_zt")
    with app.con() as c, c.cursor() as cur:
        cur.execute(SCHEMA)
        cur.execute("UPDATE issues SET lock_version = 4")
        for seq in ("journals_id_seq", "journal_details_id_seq", "custom_values_id_seq"):
            cur.execute(f"SELECT setval('{seq}', 100)")
    os.environ["OA_ZT_TEST_PW"] = "reader-pw"
    work = root / "zt"
    work.mkdir()
    read = {"sql_driver": "psycopg2",
            "sql_connect_args": {"host": "127.0.0.1", "port": port, "user": "oa_reader",
                                 "dbname": "tracker_zt"},
            "sql_password_env": "OA_ZT_TEST_PW"}  # fmt: skip
    admin_read = {"sql_driver": "psycopg2",
                  "sql_connect_args": {"host": "127.0.0.1", "port": port, "user": "admin",
                                       "dbname": "tracker_zt"}}  # fmt: skip
    (work / "read.yaml").write_text(yaml.safe_dump(read))
    (work / "kinds.yaml").write_text(yaml.safe_dump(KINDS, allow_unicode=True))
    common = ["--read", str(work / "read.yaml")]

    def demo(folder: str, kind: str, values: dict[str, Any], write: Callable[[], Any]) -> None:
        assert gen.main(["snapshot", *common, "--out", str(work / "before.json")]) == 0
        write()
        assert gen.main(["record", *common, "--before", str(work / "before.json"), "--kind", kind,
                         "--values", json.dumps(values), "--demos", str(work / folder)]) == 0  # fmt: skip

    for ex in KINDS["kinds"][0]["examples"]:
        demo("demos", "create_issue", ex["values"], lambda v=ex["values"]: app.create_issue(v))
    for ex in KINDS["kinds"][1]["examples"]:
        v = ex["values"]
        demo("demos", "update_issue", v,
             lambda v=v: app.update_issue(app.find(v["project"], v["subject"]), v))  # fmt: skip
    for v in HOLDOUT["create_issue"]:
        demo("holdout", "create_issue", v, lambda v=v: app.create_issue(v))
    for v in HOLDOUT["update_issue"]:
        demo("holdout", "update_issue", v,
             lambda v=v: app.update_issue(app.find(v["project"], v["subject"]), v))  # fmt: skip
    before = _digest(app)
    lines: list[str] = []
    result = zt.onboard(
        read=read, admin=admin_read, reader="oa_reader", kinds_path=work / "kinds.yaml",
        demo_dir=work / "demos", holdout_dir=work / "holdout", out=work / "out",
        actor_params={"bot_user_id": str(BOT)}, actor_id="ops-bot", log=lines.append,
    )  # fmt: skip
    return {"app": app, "result": result, "before": before, "out": work / "out", "log": lines}


def test_no_person_decision_and_a_ledger(onboarded: dict[str, Any]) -> None:
    r = onboarded["result"]
    assert r["person_decisions"] == 0
    keys = {e["key"] for e in r["ledger"]}
    assert {"now_checks", "bookkeeping", "recheck_after_s"} <= keys
    # Nothing beyond the generator's proposal: no log tables, no added columns.
    book = r["answers"]["bookkeeping"]
    assert "logs_add" not in book and "columns_add" not in book
    assert (onboarded["out"] / "EXCEPTIONS.md").exists()


def test_the_apps_own_tables_did_not_change(onboarded: dict[str, Any]) -> None:
    assert _digest(onboarded["app"]) == onboarded["before"]
    schemas = {s for (s,) in onboarded["app"].q("SELECT schema_name FROM information_schema.schemata")}
    assert zt.PG_SANDBOX_SCHEMA not in schemas


def test_an_admitted_kind_confirmed_every_held_out_write_and_stopped_every_fault(
    onboarded: dict[str, Any],
) -> None:
    kinds = onboarded["result"]["kinds"]
    for name, s in kinds.items():
        if s["admitted"]:
            assert s["correct_writes"] >= 1 and not s["false_stops"]
            assert s["faults"] > 0 and s["faults_stopped"] == s["faults"], (name, s)
        else:
            assert s["false_stops"] or s["faults_passed"] or s.get("why"), (name, s)


def test_create_issue_is_admitted_with_every_fault_class(onboarded: dict[str, Any]) -> None:
    s = onboarded["result"]["kinds"]["create_issue"]
    assert s["admitted"], (s, "\n".join(onboarded["log"][-60:]))
    for cls in ("lost_write", "wrong_value", "duplicate", "look_alike", "collateral_row",
                "late_write", "extra_row", "bookkeeping"):  # fmt: skip
        assert cls in s["by_class"], s["by_class"]


def test_a_kind_not_admitted_has_no_production_contract(onboarded: dict[str, Any]) -> None:
    r = onboarded["result"]
    manifest = yaml.safe_load((onboarded["out"] / "contracts" / "manifest.yaml").read_text())
    assert sorted(manifest["kinds"]) == r["admitted"]
    for name, s in r["kinds"].items():
        if not s["admitted"]:
            with pytest.raises(gen.GenerateError):
                gen.choose(onboarded["out"] / "contracts" / "manifest.yaml", name,
                           HOLDOUT[name][0], {"bot_user_id": str(BOT)})  # fmt: skip


def test_a_planted_time_stamp_fails_the_now_check() -> None:
    from openadapt_flow.generate.sql_store import Column, Schema, Table

    schema = Schema("mysql", "d", {"t": Table("t", [Column("at", "datetime", True, None)], [])})
    b = gen.SqlBuilder(schema)
    sql = b.now("t", "at", 1260)
    assert "NOW() - INTERVAL 1260 SECOND" in sql and "UTC_TIMESTAMP()" in sql
    assert zt.mutate(schema.tables["t"], "at", "2026-11-03 10:00:00") == zt.FAR_TIME
