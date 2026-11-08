# ruff: noqa: F811  (pytest fixtures imported from the generator test, then used as arguments)
"""False stops of generated contracts left open by the #1672 reviews (#1675).

The round-2 tracker (one audit row per changed field, no version column) on
a throwaway PostgreSQL, with a kind that changes one or two fields per
demonstration. Correct writes must be CONFIRMED; the wrong writes beside
them must still stop. Records are made up.
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

KINDS_P = {
    "kinds": [
        *KINDS_V["kinds"],
        {"kind": "edit_plan", "parameters": ["project", "subject", "done_ratio", "due_date"],
         "examples": [
             {"values": {"project": "Billing Portal", "subject": "Refund button missing",
                         "done_ratio": 30, "due_date": "2026-11-30"}},
             {"values": {"project": "Help Desk", "subject": "Printer jams", "done_ratio": 50,
                         "due_date": None}},
         ]},
    ]
}  # fmt: skip


@pytest.fixture(scope="module")
def gp(pg: Any) -> dict[str, Any]:
    root, port = pg["root"], pg["port"]
    admin = psycopg2.connect(
        host="127.0.0.1", port=port, user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE trackerp")
    admin.close()
    app = AppV(port, "trackerp")
    with app.con() as c, c.cursor() as cur:
        cur.execute(
            "DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='oa_reader') "
            "THEN CREATE ROLE oa_reader LOGIN PASSWORD 'reader-pw'; END IF; END$$"
        )
        cur.execute(SCHEMA_V)
    os.environ["OA_GEN_TEST_PW"] = "reader-pw"
    work = root / "workp"
    work.mkdir()
    read = {"sql_driver": "psycopg2",
            "sql_connect_args": {"host": "127.0.0.1", "port": port, "user": "oa_reader", "dbname": "trackerp"},
            "sql_password_env": "OA_GEN_TEST_PW"}  # fmt: skip
    (work / "read.yaml").write_text(yaml.safe_dump(read))
    (work / "kinds.yaml").write_text(yaml.safe_dump(KINDS_P, allow_unicode=True))
    common = ["--read", str(work / "read.yaml")]

    def demo(kind: str, values: dict[str, Any], write: Any) -> None:
        assert gen.main(["snapshot", *common, "--out", str(work / "before.json")]) == 0
        write()
        assert gen.main(["record", *common, "--before", str(work / "before.json"), "--kind", kind,
                         "--values", json.dumps(values), "--demos", str(work / "demos")]) == 0  # fmt: skip

    for k in KINDS_P["kinds"]:
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
    assert "audits" in report["kinds"]["edit_plan"]["log_candidates"], report
    # The person's answers: the proposal, plus the audit table as a log (one
    # row per changed field, so the count differs between demonstrations).
    answers = {q["key"]: q["proposal"] for q in report["questions"]}
    bk = answers["bookkeeping"]
    bk = {"accept": True} if bk == "accept" else dict(bk)
    bk["logs_add"] = ["audits"]
    answers["bookkeeping"] = bk
    (work / "answers.yaml").write_text(yaml.safe_dump(answers))
    assert gen.main([*argv, "--answers", str(work / "answers.yaml")]) == 0
    report = json.loads((work / "out" / "report.json").read_text())
    return {"app": app, "out": work / "out", "report": report, "read": read}


def _fresh(app: AppV, subject: str) -> int:
    return app.create_issue(
        {"project": "Billing Portal", "subject": subject, "status": "New"}
    )


# -- item 2: a kind whose demonstrations audited different numbers of fields ---


def test_a_kind_auditing_one_or_two_fields_is_an_update(gp: dict[str, Any]) -> None:
    """One demonstration changed two fields (two audit rows), the other one.
    The issue is still the main record: a project id equal to an issue id
    by chance doesn't hide the values the save put in the issue."""
    k = gp["report"]["kinds"]["edit_plan"]
    assert k["mode"] == "update" and not k["refused"], k
    assert k["emitted"] > 0, k


def test_correct_two_field_plan_edit_confirmed(gp: dict[str, Any]) -> None:
    app = gp["app"]
    iid = _fresh(app, "P1 two")
    v = {"project": "Billing Portal", "subject": "P1 two", "done_ratio": 70,
         "due_date": "2026-12-15"}  # fmt: skip
    out = _run(
        gp,
        "edit_plan",
        v,
        lambda: app.save(iid, {"done_ratio": 70, "due_date": "2026-12-15"}),
    )
    assert out.startswith("CONFIRMED"), out


def test_correct_one_field_plan_edit_confirmed(gp: dict[str, Any]) -> None:
    app = gp["app"]
    iid = _fresh(app, "P2 one")
    v = {"project": "Billing Portal", "subject": "P2 one", "done_ratio": 40,
         "due_date": None}  # fmt: skip
    out = _run(gp, "edit_plan", v, lambda: app.save(iid, {"done_ratio": 40}))
    assert out.startswith("CONFIRMED"), out


def test_plan_edit_with_the_wrong_ratio_stops(gp: dict[str, Any]) -> None:
    app = gp["app"]
    iid = _fresh(app, "P3 wrong")
    v = {"project": "Billing Portal", "subject": "P3 wrong", "done_ratio": 40,
         "due_date": None}  # fmt: skip
    out = _run(gp, "edit_plan", v, lambda: app.save(iid, {"done_ratio": 60}))
    assert not out.startswith("CONFIRMED"), out


def test_plan_edit_that_also_changes_the_status_stops(gp: dict[str, Any]) -> None:
    app = gp["app"]
    iid = _fresh(app, "P4 extra")
    v = {"project": "Billing Portal", "subject": "P4 extra", "done_ratio": 40,
         "due_date": None}  # fmt: skip
    out = _run(
        gp,
        "edit_plan",
        v,
        lambda: app.save(iid, {"done_ratio": 40, "status": "Resolved"}),
    )
    assert not out.startswith("CONFIRMED"), out


def test_plan_edit_that_loses_another_users_save_stops(gp: dict[str, Any]) -> None:
    app = gp["app"]
    iid = _fresh(app, "P5 lost")
    v = {"project": "Billing Portal", "subject": "P5 lost", "done_ratio": 40,
         "due_date": None}  # fmt: skip

    def write() -> None:
        app.save(iid, {"status": "In Progress"}, user=2)
        app.save(iid, {"done_ratio": 40, "status": "New"})

    out = _run(gp, "edit_plan", v, write)
    assert not out.startswith("CONFIRMED"), out
