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
        h.update(
            json.dumps(app.q(f"SELECT * FROM {t} ORDER BY 1"), default=str).encode()
        )
    return h.hexdigest()


@pytest.fixture(scope="module")
def onboarded(pg: Any) -> dict[str, Any]:  # noqa: F811
    root, port = pg["root"], pg["port"]
    admin = psycopg2.connect(
        host="127.0.0.1", port=port, user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE tracker_zt")
    admin.close()
    app = App(port, "tracker_zt")
    with app.con() as c, c.cursor() as cur:
        cur.execute(SCHEMA)
        cur.execute("UPDATE issues SET lock_version = 4")
        for seq in (
            "journals_id_seq",
            "journal_details_id_seq",
            "custom_values_id_seq",
        ):
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

    def demo(
        folder: str, kind: str, values: dict[str, Any], write: Callable[[], Any]
    ) -> None:
        assert gen.main(["snapshot", *common, "--out", str(work / "before.json")]) == 0
        write()
        assert gen.main(["record", *common, "--before", str(work / "before.json"), "--kind", kind,
                         "--values", json.dumps(values), "--demos", str(work / folder)]) == 0  # fmt: skip

    for ex in KINDS["kinds"][0]["examples"]:
        demo(
            "demos",
            "create_issue",
            ex["values"],
            lambda v=ex["values"]: app.create_issue(v),
        )
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
        keep_sandbox=True,
    )  # fmt: skip
    # The control: the same answers without the time check, in the same
    # sandbox. The planted time-stamp fault must then pass.
    sb = zt.Sandbox(admin_read, read)
    sb.schema = zt.store.read_schema(zt.store.connector(sb.read_config()), "postgresql")
    try:
        answers = {**result["answers"], "recheck_after_s": zt.SANDBOX_RECHECK_S}
        answers.pop("now_checks")
        gen.propose(read=sb.read_config(), kinds=gen.load_kinds(work / "kinds.yaml"),
                    demo_dir=work / "demos", out=work / "control",
                    actor_params={"bot_user_id": str(BOT)}, answers=answers)  # fmt: skip
        control: list[dict[str, Any]] = []
        zt.validate(sb, work / "control" / "manifest.yaml",
                    [d for d in gen.load_demos(work / "holdout") if d.kind == "create_issue"],
                    "ops-bot", {"bot_user_id": str(BOT)}, record=control)  # fmt: skip
    finally:
        sb.drop()
    if os.environ.get("OA_ZT_TEST_KEEP"):
        import shutil

        shutil.copytree(work, os.environ["OA_ZT_TEST_KEEP"], dirs_exist_ok=True)
        (Path(os.environ["OA_ZT_TEST_KEEP"]) / "log.txt").write_text("\n".join(lines))
    return {"app": app, "result": result, "before": before, "out": work / "out", "log": lines,
            "control": control}  # fmt: skip


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
    schemas = {
        s
        for (s,) in onboarded["app"].q(
            "SELECT schema_name FROM information_schema.schemata"
        )
    }
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


def test_create_issue_is_admitted_with_every_fault_class(
    onboarded: dict[str, Any],
) -> None:
    s = onboarded["result"]["kinds"]["create_issue"]
    assert s["admitted"], (s, "\n".join(onboarded["log"][-60:]))
    for cls in ("missing", "wrong_value", "duplicate", "look_alike", "unrelated_edit",
                "delayed", "other_table", "bookkeeping"):  # fmt: skip
        assert cls in s["by_class"], s["by_class"]


def test_a_kind_not_admitted_has_no_production_contract(
    onboarded: dict[str, Any],
) -> None:
    r = onboarded["result"]
    manifest = yaml.safe_load(
        (onboarded["out"] / "contracts" / "manifest.yaml").read_text()
    )
    assert sorted(manifest["kinds"]) == r["admitted"]
    for name, s in r["kinds"].items():
        if not s["admitted"]:
            with pytest.raises(gen.GenerateError):
                gen.choose(onboarded["out"] / "contracts" / "manifest.yaml", name,
                           HOLDOUT[name][0], {"bot_user_id": str(BOT)})  # fmt: skip


def test_a_planted_time_stamp_fails_the_now_check() -> None:
    from openadapt_flow.generate.sql_store import Column, Schema, Table

    schema = Schema(
        "mysql", "d", {"t": Table("t", [Column("at", "datetime", True, None)], [])}
    )
    b = gen.SqlBuilder(schema)
    sql = b.now("t", "at", 1260)
    assert "NOW() - INTERVAL 1260 SECOND" in sql and "UTC_TIMESTAMP()" in sql
    assert zt.mutate(schema.tables["t"], "at", "2026-11-03 10:00:00") == zt.FAR_TIME


def test_the_time_stamp_fault_is_caught_by_the_time_check(
    onboarded: dict[str, Any],
) -> None:
    rows = [
        json.loads(x)
        for x in (onboarded["out"] / "VALIDATION.jsonl").read_text().splitlines()
    ]
    hit = [r for r in rows if r["case"] == "bookkeeping:issues.created_on"]
    assert hit and all(not r["correct"] and r["ok"] for r in hit), hit
    # The main record no longer matches its contract (the time check is one
    # of its columns); the control below shows the same fault passes
    # without the time check.
    assert any("record_written was refuted" in " ".join(r["reasons"]) for r in hit), hit


def test_without_the_time_check_the_time_stamp_fault_passes(
    onboarded: dict[str, Any],
) -> None:
    hit = [
        r for r in onboarded["control"] if r["case"] == "bookkeeping:issues.created_on"
    ]
    assert hit and not any(r["ok"] for r in hit), hit


# -- the admission gate, without a database (review round 1, B1-B5, S7) -------


class _FakeSandbox:
    """Rows as a sandbox would read them; nothing is written."""

    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self._rows = rows

    def get(self, table: str, key: Any) -> Any:
        return None

    def rows(self, table: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows.get(table, [])[:limit]

    def max_key(self, table: str) -> int:
        return 1

    def distinct(self, table: str, column: str, limit: int = 5) -> list[str]:
        return []


def _wide() -> tuple[Any, Any, Any, list[Any]]:
    from openadapt_flow.generate.sql_store import Column, Schema, Table

    cols = [Column("id", "int", False, None, auto=True)]
    cols += [Column(f"c{i:02d}", "varchar", True, None) for i in range(20)]
    cols += [Column("meta", "json", True, None), Column("blob", "blob", True, None),
             Column("sex", "enum", True, None), Column("note", "varchar", True, None),
             Column("t", "time", True, None), Column("u", "uuid", True, None)]  # fmt: skip
    patient = Table("patient", cols, ["id"])
    other = Table(
        "other",
        [Column("id", "int", False, None), Column("x", "int", True, None)],
        ["id"],
    )
    schema = Schema("mysql", "app", {"patient": patient, "other": other})
    sb = _FakeSandbox({"other": [{"id": "1", "x": "5"}],
                       "patient": [{"id": "1", **{c.name: "old" for c in cols[1:]}}]})  # fmt: skip

    class D:
        values = {"name": "Ann"}

    row = {"id": "2", **{f"c{i:02d}": f"v{i}" for i in range(20)}, "meta": '{"a": 1}',
           "blob": "0xff", "sex": "F", "note": None, "t": "10:00:00",
           "u": "123e4567-e89b-12d3-a456-426614174000"}  # fmt: skip
    return schema, sb, D(), [zt.Op("patient", {"id": "2"}, row)]


def test_b2_b3_b4_every_column_of_a_wide_row_gets_a_planted_fault() -> None:
    schema, sb, demo, write = _wide()
    cases = zt.faults(schema, sb, demo, write)
    faulted = {c.name.split(".")[-1] for c in cases
               if c.cls in ("bookkeeping", "wrong_value") and not c.unplantable}  # fmt: skip
    assert faulted == set(write[0].row) - {"id"}  # note (NULL) included
    note = next(c for c in cases if c.name == "bookkeeping:patient.note")
    assert note.write[0].row["note"] is not None  # B4: a non-NULL plant
    meta = next(c for c in cases if c.name == "bookkeeping:patient.meta")
    assert json.loads(meta.write[0].row["meta"]) != {"a": 1}  # B3: valid JSON
    sex = next(c for c in cases if c.name == "bookkeeping:patient.sex")
    assert len(sex.write[0].row["sex"]) == 1  # text keeps its length


def test_b3_a_type_with_no_generator_blocks_admission() -> None:
    from openadapt_flow.generate.sql_store import Column, Schema, Table

    t = Table(
        "p",
        [Column("id", "int", False, None), Column("geo", "geometry", True, None)],
        ["id"],
    )
    schema = Schema("mysql", "app", {"p": t})
    cases = zt.faults(schema, _FakeSandbox({}), type("D", (), {"values": {}})(),
                      [zt.Op("p", {"id": "2"}, {"id": "2", "geo": "0101"})])  # fmt: skip
    geo = [c for c in cases if c.name.endswith("p.geo")]
    assert geo and geo[0].unplantable
    out = zt.run_case(None, Path("."), None, geo[0], "a", {})  # type: ignore[arg-type]
    assert out.outcome == "NOT_PLANTED" and not out.ok


def test_b1_a_fault_that_errors_is_not_stopped() -> None:
    correct = zt.Outcome("correct", "correct", True, "CONFIRMED", True)
    for verdict in ("ERROR", "NO_CONTRACT", "NOT_PLANTED"):
        bad = zt.Outcome("bookkeeping:p.sex", "bookkeeping", False, verdict, False)
        assert not bad.ok
        s = zt.summarize({"k": [correct, bad]}, ["k"])["k"]
        assert not s["admitted"], verdict
    assert zt.Outcome("x", "missing", False, "REFUTED", False).ok
    assert zt.Outcome("x", "delayed", False, "RECHECK_REFUTED", False).ok


def test_b5_a_kind_without_the_core_fault_classes_is_not_admitted() -> None:
    correct = zt.Outcome("correct", "correct", True, "CONFIRMED", True)
    missing = zt.Outcome("missing", "missing", False, "UNCERTAIN", False)
    s = zt.summarize({"k": [correct, missing]}, ["k"], {"k": {"create"}})["k"]
    assert not s["admitted"]
    assert {
        "unrelated_edit",
        "delayed",
        "other_table",
        "duplicate",
        "look_alike",
    } <= set(s["classes_not_planted"])
    full = [correct] + [zt.Outcome(c, c, False, "REFUTED", False)
                        for c in (*zt.REQUIRED_CLASSES, *zt.CREATE_CLASSES)]  # fmt: skip
    assert zt.summarize({"k": full}, ["k"], {"k": {"create"}})["k"]["admitted"]


def test_s7_a_held_out_write_that_repeats_a_demonstration_is_refused() -> None:
    from openadapt_flow.generate.demo_analysis import Demo
    from openadapt_flow.generate.sql_store import Diff

    a = Demo("k", {"x": 1}, Diff([]), {}, name="a")
    b = Demo("k", {"x": 2}, Diff([]), {}, name="b")
    with pytest.raises(zt.ZeroTouchError):
        zt.check_holdout([a], [Demo("k", {"x": 1}, Diff([]), {}, name="c")], ["k"])
    with pytest.raises(zt.ZeroTouchError):
        zt.check_holdout([a], [b], ["k", "other_kind"])


def test_s1_s2_install_keeps_only_admitted_kinds_and_fingerprints_ignore_connections(
    tmp_path: Path,
) -> None:
    staged = tmp_path / "staged"
    for k in ("a", "b"):
        (staged / "contracts" / k).mkdir(parents=True)
        (staged / "contracts" / k / "x.create.yaml").write_text(yaml.safe_dump(
            {"verifier": {"sql_connect_args": {"port": 1}, "sql_query": "SELECT 1"},
             "frame": {"database": {"recheck_after_s": 3}}}))  # fmt: skip
    manifest = {"kinds": {k: {"mode": "create", "variants": [
        {"fields": ["x"], "create": f"contracts/{k}/x.create.yaml"}]} for k in ("a", "b")},
        "field_sets": {"listed_sha256": {f"contracts/{k}/x.create.yaml": "d" for k in ("a", "b")}}}  # fmt: skip
    (staged / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    other = tmp_path / "validated"
    shutil_copy = __import__("shutil").copytree
    shutil_copy(staged, other)
    doc = yaml.safe_load((other / "contracts/a/x.create.yaml").read_text())
    doc["verifier"]["sql_connect_args"]["port"] = 2
    doc["frame"]["database"]["recheck_after_s"] = 300
    (other / "contracts/a/x.create.yaml").write_text(yaml.safe_dump(doc))
    assert zt.fingerprint(staged, "a") == zt.fingerprint(other, "a")
    doc["verifier"]["sql_query"] = "SELECT 2"
    (other / "contracts/a/x.create.yaml").write_text(yaml.safe_dump(doc))
    assert zt.fingerprint(staged, "a") != zt.fingerprint(other, "a")
    zt.install(staged, tmp_path / "contracts", ["a"])
    got = yaml.safe_load((tmp_path / "contracts" / "manifest.yaml").read_text())
    assert list(got["kinds"]) == ["a"]
    assert "field_sets" not in got  # no contracts built at run time
    assert not (tmp_path / "contracts" / "contracts" / "b").exists()
    assert not staged.exists()


def test_strict_pins_checks_both_readings_and_is_off_by_default() -> None:
    from openadapt_flow.generate.demo_analysis import ColumnRole
    from openadapt_flow.generate.sql_store import Column, ForeignKey, Schema, Table

    item = Table("item", [Column("id", "int", False, None), Column("cur", "varchar", True, None),
                          Column("prod", "int", True, None)], ["id"],
                 [ForeignKey("prod", "product", "id")])  # fmt: skip
    product = Table("product", [Column("id", "int", False, None),
                                Column("cur", "varchar", True, None)], ["id"])  # fmt: skip
    schema = Schema("mysql", "d", {"item": item, "product": product})
    copied = {"table": "product", "column": "cur", "ref_column": "id", "via": "prod"}
    role = ColumnRole("varies", detail={"why": "a constant or a copy: can't tell",
                                        "strict": {"constant": "EUR", "copied": copied}})  # fmt: skip
    assert gen._strict({"cur": role}, False)["cur"].role == "varies"
    strict = gen._strict({"cur": role}, True)["cur"]
    assert strict.role == "strict"
    b = gen.SqlBuilder(schema)
    surf = gen.Surface("record", "item", [])
    match: dict[str, Any] = {}
    gen._pin(b, surf, "item", "cur", strict, match, {})
    exprs = dict(surf.columns)
    assert match == {"pin__cur": {"literal": "1"}, "copy__cur": {"literal": "1"}}
    assert "'EUR'" in exprs["pin__cur"] and "`product`" in exprs["copy__cur"]
    with pytest.raises(gen.Unpinned):
        gen._pin(b, gen.Surface("record", "item", []), "item", "cur", role, {}, {})


# -- a duplicate the store's own unique index refuses (review round 2) -------


class _UniqueSandbox(_FakeSandbox):
    def unique_indexes(self, table: str) -> dict[str, list[str]]:
        return {"uk_res": ["fk_event", "kind", "fk_element"]}


def _dup_case(copy_changes: dict[str, Any]) -> Any:
    row = {"id": "5", "fk_event": "46", "kind": "user", "fk_element": "2", "note": "a"}
    copy = {**row, "id": "1005", **copy_changes}
    given = {"fk_event", "kind", "fk_element"}  # the task gave the index's values
    return zt.Case("duplicate:res", "duplicate", [], copy_of=("res", row, copy, given))


class _Mysql1062(Exception):
    pass


def test_a_duplicate_refused_by_a_declared_unique_index_is_not_applicable() -> None:
    exc = _Mysql1062(1062, "Duplicate entry '46-user-2' for key 'uk_res'")
    got = zt.blocked_by_constraint(_UniqueSandbox({}), _dup_case({}), exc)
    assert got and "uk_res" in got


def test_any_other_refusal_of_a_duplicate_is_still_a_failure() -> None:
    sb = _UniqueSandbox({})
    unique = _Mysql1062(1062, "Duplicate entry '46-user-2' for key 'uk_res'")
    # The copy changed a column of the index: the index doesn't prove it.
    assert zt.blocked_by_constraint(sb, _dup_case({"fk_element": "3"}), unique) is None
    # An index the schema doesn't declare.
    other = _Mysql1062(1062, "Duplicate entry 'x' for key 'uk_other'")
    assert zt.blocked_by_constraint(sb, _dup_case({}), other) is None
    # Not a unique violation (a NOT NULL refusal).
    assert (
        zt.blocked_by_constraint(sb, _dup_case({}), _Mysql1062(1048, "cannot be null"))
        is None
    )
    # Not a duplicate fault.
    case = _dup_case({})
    case.cls = "bookkeeping"
    assert zt.blocked_by_constraint(sb, case, unique) is None


def test_duplicates_impossible_by_constraint_are_recorded_not_required() -> None:
    correct = zt.Outcome("correct", "correct", True, "CONFIRMED", True)
    rest = [zt.Outcome(c, c, False, "REFUTED", False)
            for c in (*zt.REQUIRED_CLASSES, "look_alike")]  # fmt: skip
    na = zt.Outcome("duplicate:res", "duplicate", False, "NOT_APPLICABLE", False,
                    ["impossible by constraint res.uk_res (fk_event, kind, fk_element)"])  # fmt: skip
    s = zt.summarize({"k": [correct, *rest, na]}, ["k"], {"k": {"create"}})["k"]
    assert s["admitted"] and s["duplicates_impossible_by_constraint"]
    # One blocked, one that errored: not admitted.
    err = zt.Outcome("duplicate:main", "duplicate", False, "ERROR", False)
    s = zt.summarize({"k": [correct, *rest, na, err]}, ["k"], {"k": {"create"}})["k"]
    assert not s["admitted"]


def test_unique_indexes_reads_the_catalog_on_postgresql(pg: Any) -> None:  # noqa: F811
    admin = psycopg2.connect(
        host="127.0.0.1", port=pg["port"], user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE uq_probe")
    admin.close()
    c = psycopg2.connect(
        host="127.0.0.1", port=pg["port"], user="admin", dbname="uq_probe"
    )
    c.autocommit = True
    c.cursor().execute(
        "CREATE SCHEMA oa_sandbox; CREATE TABLE oa_sandbox.res (id int PRIMARY KEY, "
        "a int, b text, c int); CREATE UNIQUE INDEX uk_res ON oa_sandbox.res (b, a); "
        "CREATE INDEX plain ON oa_sandbox.res (c)"
    )
    c.close()
    read = {"sql_driver": "psycopg2", "sql_connect_args": {"host": "127.0.0.1", "port": pg["port"],
            "user": "admin", "dbname": "uq_probe"}}  # fmt: skip
    sb = zt.Sandbox(read, read)
    assert sb.unique_indexes("res") == {"uk_res": ["b", "a"]}


# -- review round 2: R2-B1, R2-B2, S8-S10 -------------------------------------

READ_BACK_FAILED = (
    "the system of record could not be read back (OperationalError); "
    "the write may still land"
)


def test_r2b1_a_verdict_from_a_failed_read_is_retried_then_fails(
    monkeypatch: Any,
) -> None:
    calls: list[int] = []

    def fails_to_read(*a: Any, **k: Any) -> Any:
        calls.append(1)
        return zt.Outcome("bookkeeping:p.c15", "bookkeeping", False, "UNCERTAIN", False,
                          [READ_BACK_FAILED])  # fmt: skip

    monkeypatch.setattr(zt, "run_case", fails_to_read)
    case = zt.Case("bookkeeping:p.c15", "bookkeeping", [])
    res = zt.run_verdict(None, Path("m/manifest.yaml"), None, case, "a", {})  # type: ignore[arg-type]
    assert len(calls) == zt.READ_ATTEMPTS
    assert res.outcome == "READ_FAILED" and not res.ok
    correct = zt.Outcome("correct", "correct", True, "CONFIRMED", True)
    assert not zt.summarize({"k": [correct, res]}, ["k"])["k"]["admitted"]


def test_r2b1_a_read_that_recovers_gives_its_real_verdict(monkeypatch: Any) -> None:
    seq = iter([
        zt.Outcome("x", "bookkeeping", False, "UNCERTAIN", False, [READ_BACK_FAILED]),
        zt.Outcome("x", "bookkeeping", False, "REFUTED", False, ["record_written was refuted"]),
    ])  # fmt: skip
    monkeypatch.setattr(zt, "run_case", lambda *a, **k: next(seq))
    res = zt.run_verdict(None, Path("m/manifest.yaml"), None,  # type: ignore[arg-type]
                         zt.Case("x", "bookkeeping", []), "a", {})  # fmt: skip
    assert res.outcome == "REFUTED" and res.ok


def test_r2b2_found_keeps_only_the_constant_half_of_a_strict_role() -> None:
    from openadapt_flow.generate.demo_analysis import ColumnRole

    copied = {"table": "q", "column": "owner", "ref_column": "id", "via": "q_id"}
    assert (
        gen._found_role(ColumnRole("strict", detail={"actor": "bot", "copied": copied}))
        is None
    )
    kept = gen._found_role(
        ColumnRole("strict", detail={"constant": "1", "copied": copied})
    )
    assert kept is not None and kept.detail == {"constant": "1"}
    assert gen._found_role(ColumnRole("actor", detail={"param": "bot"})) is None


TICKETS = """
CREATE TABLE queues (id serial PRIMARY KEY, name text NOT NULL, owner_id integer);
CREATE TABLE tickets (id serial PRIMARY KEY, queue_id integer REFERENCES queues(id),
  subject text NOT NULL, owner_id integer);
INSERT INTO queues (name, owner_id) VALUES ('Ops', 1), ('Billing', 1), ('Desk', 1);
SELECT setval('tickets_id_seq', 100);
CREATE ROLE oa_t LOGIN PASSWORD 'reader-pw';
GRANT SELECT ON ALL TABLES IN SCHEMA public TO oa_t;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO oa_t;
"""


def test_r2b2_a_record_another_user_made_is_found_under_strict_pins(
    pg: Any,  # noqa: F811
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """A ticket's owner is the acting user and a copy of its queue's owner in
    every demonstration, so ``strict_pins`` checks both on a create. A ticket
    a colleague already made (owner 2) must still be found, so the task
    writes nothing and a second create is refuted."""

    admin = psycopg2.connect(
        host="127.0.0.1", port=pg["port"], user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE tickets")
    admin.close()
    app = App(pg["port"], "tickets")
    with app.con() as c, c.cursor() as cur:
        cur.execute(TICKETS)
    os.environ["OA_ZT_T_PW"] = "reader-pw"
    read = {"sql_driver": "psycopg2", "sql_connect_args": {"host": "127.0.0.1",
            "port": pg["port"], "user": "oa_t", "dbname": "tickets"},
            "sql_password_env": "OA_ZT_T_PW"}  # fmt: skip
    (tmp_path / "read.yaml").write_text(yaml.safe_dump(read))
    kinds = {"kinds": [{"kind": "create_ticket", "parameters": ["queue", "subject"],
                        "examples": [{"values": {"queue": "Ops", "subject": "a"}}]}]}  # fmt: skip
    (tmp_path / "kinds.yaml").write_text(yaml.safe_dump(kinds))
    common = ["--read", str(tmp_path / "read.yaml")]
    for queue, subject in (
        ("Ops", "Badge reader"),
        ("Billing", "Refund"),
        ("Desk", "VPN"),
    ):
        assert (
            gen.main(["snapshot", *common, "--out", str(tmp_path / "before.json")]) == 0
        )
        app.q("INSERT INTO tickets (queue_id, subject, owner_id) SELECT id, %s, 1 "
              "FROM queues WHERE name = %s", (subject, queue))  # fmt: skip
        assert gen.main(["record", *common, "--before", str(tmp_path / "before.json"),
                         "--kind", "create_ticket", "--values",
                         json.dumps({"queue": queue, "subject": subject}),
                         "--demos", str(tmp_path / "demos")]) == 0  # fmt: skip
    answers = {"strict_pins": True, "bookkeeping": "accept", "recheck_after_s": 300}
    gen.propose(read=read, kinds=gen.load_kinds(tmp_path / "kinds.yaml"),
                demo_dir=tmp_path / "demos", out=tmp_path / "out",
                actor_params={"bot_user_id": "1"}, answers=answers)  # fmt: skip
    create = next((tmp_path / "out" / "contracts").rglob("*.create.yaml")).read_text()
    assert "copy__owner_id" in create  # strict: the copy is checked on a create
    # A colleague (user 2) already made this ticket.
    app.q("INSERT INTO tickets (queue_id, subject, owner_id) SELECT id, 'Printer', 2 "
          "FROM queues WHERE name = 'Ops'")  # fmt: skip
    loaded: list[Path] = []
    real = gen._load_for

    def record(path: Path, params: Any) -> Any:
        loaded.append(Path(path))
        return real(path, params)

    monkeypatch.setattr(gen, "_load_for", record)
    gen.choose(tmp_path / "out" / "manifest.yaml", "create_ticket",
               {"queue": "Ops", "subject": "Printer"}, {"bot_user_id": "1"})  # fmt: skip
    # The found contract is read first; it must also be the one returned.
    assert loaded and loaded[-1].name.endswith(".found.yaml"), loaded


def test_s9_s10_a_new_rows_own_key_and_each_value_on_another_row_are_faulted() -> None:
    from openadapt_flow.generate.sql_store import Column, Schema, Table

    t = Table("codes", [Column("code", "varchar", False, None), Column("label", "varchar", True, None)],
              ["code"])  # fmt: skip
    schema = Schema("mysql", "app", {"codes": t})
    sb = _FakeSandbox({"codes": [{"code": "AA", "label": "old"}]})
    cases = zt.faults(schema, sb, type("D", (), {"values": {"code": "BB"}})(),
                      [zt.Op("codes", {"code": "BB"}, {"code": "BB", "label": "new"})])  # fmt: skip
    names = {c.name for c in cases}
    assert "wrong_value:codes.code" in names  # S9
    assert "unrelated_edit:codes.label" in names  # S10


# -- review round 3: R3-B1, R3-B2, S11, S12 -----------------------------------


def test_r3b1_an_index_on_a_value_the_app_makes_never_proves_a_duplicate_impossible() -> (
    None
):
    exc = _Mysql1062(1062, "Duplicate entry '46-user-2' for key 'uk_res'")
    case = _dup_case({})
    table, row, copy, _ = case.copy_of
    case.copy_of = (table, row, copy, {"fk_event", "kind"})  # fk_element: the app's
    assert zt.blocked_by_constraint(_UniqueSandbox({}), case, exc) is None
    # N5: another table's index of the same name.
    other = _Mysql1062(1062, "Duplicate entry 'x' for key 'other_table.uk_res'")
    assert zt.blocked_by_constraint(_UniqueSandbox({}), _dup_case({}), other) is None


def test_s11_an_uncertain_without_a_judged_read_is_not_stopped() -> None:
    class R:
        receipt = {"outcome": "reconciliation_required"}
        evidence = {"frame": {"status": "not_checked"}}
        observed = "present"

    assert not zt.write_judged(R())
    R.evidence = {"frame": {"status": "held"}}
    assert zt.write_judged(R())
    R.observed = "unknown"
    assert not zt.write_judged(R())

    class RC:
        read_at = ""
        frame = "not_checked"

    assert not zt.recheck_judged(RC())
    RC.read_at, RC.frame = "2026-11-03T20:00:00Z", "refuted"
    assert zt.recheck_judged(RC())
    out = zt.Outcome(
        "x", "bookkeeping", False, "UNCERTAIN", False, ["a judgment"], False
    )
    assert not out.ok
    for text in ("the database was still changing after the save",
                 "no readable baseline, so what else changed can't be known",
                 "the database could not be judged again"):  # fmt: skip
        assert zt.read_failed([text])


def _uq_sandbox(pg: Any, db: str) -> Any:  # noqa: F811
    admin = psycopg2.connect(
        host="127.0.0.1", port=pg["port"], user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {db}")
    admin.close()
    c = psycopg2.connect(host="127.0.0.1", port=pg["port"], user="admin", dbname=db)
    c.autocommit = True
    c.cursor().execute(
        "CREATE TABLE res (id integer PRIMARY KEY, code text NOT NULL, name text, tag text);"
        "CREATE UNIQUE INDEX uk_code ON res (code);"
        "CREATE UNIQUE INDEX uk_name ON res (name);"
        "CREATE UNIQUE INDEX uk_partial ON res (tag) WHERE name IS NOT NULL;"
        "CREATE UNIQUE INDEX uk_expr ON res (lower(tag));"
        "INSERT INTO res VALUES (1, 'INV-1', 'Ann', 'a');"
    )
    c.close()
    read = {"sql_driver": "psycopg2", "sql_connect_args": {"host": "127.0.0.1",
            "port": pg["port"], "user": "admin", "dbname": db}}  # fmt: skip
    sb = zt.Sandbox(read, read)
    sb.create(zt.store.read_schema(zt.store.connector(read), "postgresql"), "admin")
    return sb


def test_r3b1_s12_on_postgresql(pg: Any) -> None:  # noqa: F811
    sb = _uq_sandbox(pg, "uq_r3")
    try:
        # S12: partial and expression indexes are not taken as proof.
        # (The sandbox's copies of the indexes get names of their own.)
        assert sorted(sb.unique_indexes("res").values()) == [["code"], ["name"]]
        # tag NULL: the expression index can't refuse the copy (a refusal by
        # it would not count as proof, so this checks uk_name alone).
        new = {"id": "2", "code": "INV-2", "name": "Bob", "tag": None}
        write = [zt.Op("res", {"id": "2"}, new)]

        def dup_of(values: dict[str, Any]) -> Any:
            bound = {("res", k) for k in values}  # the generator's bindings
            cases = zt.faults(sb.schema, sb, type("D", (), {"values": values})(),
                              write, bound)  # fmt: skip
            return next(c for c in cases if c.cls == "duplicate")

        # The task gives the name; the app makes the code: the copy gets a
        # fresh code, keeps the name, and only uk_name refuses it.
        case = dup_of({"name": "Bob"})
        assert case.write[-1].row["code"] != "INV-2"
        assert case.write[-1].row["name"] == "Bob"
        with pytest.raises(Exception) as err:
            sb.put_many(case.write)
        blocked = zt.blocked_by_constraint(sb, case, err.value)
        assert blocked and blocked.startswith("res.") and blocked.endswith("(name)")
        # Neither is the task's: both are made fresh, nothing proves the
        # duplicate impossible, and the store takes it.
        case = dup_of({})
        assert case.write[-1].row["name"] != "Bob"
        sb.put_many(case.write)  # the store takes it: a normal duplicate
    finally:
        sb.drop()


def test_r3b2_a_restore_that_fails_midway_changes_nothing(pg: Any) -> None:  # noqa: F811
    sb = _uq_sandbox(pg, "uq_tx")
    try:
        before = sb.rows("res")
        bad = [zt.Op("res", {"id": "1"}, {"id": "1", "code": "INV-9", "name": "Ann", "tag": "a"}),
               zt.Op("res", {"id": "3"}, {"id": "3", "code": None, "name": "X", "tag": "x"})]  # fmt: skip
        with pytest.raises(Exception):
            sb.put_many(bad)  # the second row breaks NOT NULL
        assert sb.rows("res") == before
    finally:
        sb.drop()


def test_r3b2_a_broken_sandbox_stops_the_setup(
    onboarded: dict[str, Any],
    pg: Any,  # noqa: F811
    monkeypatch: Any,  # noqa: F811
) -> None:
    def broken(*a: Any, **k: Any) -> Any:
        raise zt.SandboxBroken("the sandbox could not be restored: injected")

    monkeypatch.setattr(zt, "run_case", broken)
    work = onboarded["out"].parent
    read = yaml.safe_load((work / "read.yaml").read_text())
    admin_read = {"sql_driver": "psycopg2", "sql_connect_args": {"host": "127.0.0.1",
                  "port": pg["port"], "user": "admin", "dbname": "tracker_zt"}}  # fmt: skip
    out = work / "out_broken"
    (out / "contracts").mkdir(parents=True)  # a stale install must not survive
    result = zt.onboard(read=read, admin=admin_read, reader="oa_reader",
                        kinds_path=work / "kinds.yaml", demo_dir=work / "demos",
                        holdout_dir=work / "holdout", out=out,
                        actor_params={"bot_user_id": str(BOT)}, actor_id="ops-bot",
                        log=lambda s: None)  # fmt: skip
    assert result["admitted"] == [] and "injected" in result["stopped"]
    assert not (out / "contracts").exists()
    assert list(out.glob("contracts.stopped-*"))  # kept aside, never served
    assert "setup stopped" in (out / "EXCEPTIONS.md").read_text()


# -- review round 4: R4-B1 ------------------------------------------------------


def test_r4b1_an_app_value_equal_to_a_task_value_is_still_made_fresh(pg: Any) -> None:  # noqa: F811
    """The app's number 7 equals the task's quantity 7, and the app's
    reference starts with the task's due date: neither is the task's, by the
    generator's bindings, so both are made fresh and the store takes the
    duplicate (it is never waived as impossible)."""

    admin = psycopg2.connect(
        host="127.0.0.1", port=pg["port"], user="admin", dbname="postgres"
    )
    admin.autocommit = True
    admin.cursor().execute("CREATE DATABASE uq_r4")
    admin.close()
    c = psycopg2.connect(
        host="127.0.0.1", port=pg["port"], user="admin", dbname="uq_r4"
    )
    c.autocommit = True
    c.cursor().execute(
        "CREATE TABLE r4 (id integer PRIMARY KEY, num integer NOT NULL, qty integer,"
        " ref text NOT NULL, due date);"
        "CREATE UNIQUE INDEX uk_num ON r4 (num); CREATE UNIQUE INDEX uk_ref ON r4 (ref);"
        "INSERT INTO r4 VALUES (1, 5, 3, 'X-1', '2026-01-01');"
    )
    c.close()
    read = {"sql_driver": "psycopg2", "sql_connect_args": {"host": "127.0.0.1",
            "port": pg["port"], "user": "admin", "dbname": "uq_r4"}}  # fmt: skip
    sb = zt.Sandbox(read, read)
    sb.create(zt.store.read_schema(zt.store.connector(read), "postgresql"), "admin")
    try:
        new = {
            "id": "2",
            "num": "7",
            "qty": "7",
            "ref": "2026-12-01-0001",
            "due": "2026-12-01",
        }
        demo = type("D", (), {"values": {"qty": 7, "due": "2026-12-01"}})()
        bound = {("r4", "qty"), ("r4", "due")}
        cases = zt.faults(sb.schema, sb, demo, [zt.Op("r4", {"id": "2"}, new)], bound)
        dup = next(c for c in cases if c.cls == "duplicate")
        copy = dup.write[-1].row
        assert copy["num"] != "7" and copy["ref"] != "2026-12-01-0001"
        assert copy["qty"] == "7" and copy["due"] == "2026-12-01"
        sb.put_many(dup.write)  # accepted: a duplicate the contract must stop
    finally:
        sb.drop()


def test_r4b1_bindings_come_from_the_generators_report() -> None:
    report = {"kinds": {"k": {"locations": {
        "qty": "r4.qty (number)",
        "project": "issues.project_id -> projects.name (exact)",
        "ref": "custom_values (whose customized_id is the issues key).value (exact)",
        "odd": "something else",
    }}}}  # fmt: skip
    assert zt.bound_columns(report, "k") == {
        ("r4", "qty"),
        ("issues", "project_id"),
        ("custom_values", "value"),
    }
    assert zt.bound_columns({}, "k") == set()
