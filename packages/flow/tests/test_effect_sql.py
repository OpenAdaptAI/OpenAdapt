"""Read-only SQL EffectVerifier + table-delta audit (kit).

Contract-proven against LOCAL sqlite fixtures (stdlib driver) -- the verdict
logic, the read-only whitelist, and the promoted exact table-delta contract.
No production database is involved; per-substrate claims stay at that level.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    SqlRecordVerifier,
    Verdict,
    assert_read_only_sql,
    audit_table_deltas,
    capture_table_counts,
)

TARGET = {"patient_id": "p1", "type": "Triage"}
QUERY = (
    "SELECT id, patient_id, type, note FROM encounters WHERE patient_id = :patient_id"
)


@pytest.fixture
def db(tmp_path: Path):
    """A file-backed sqlite system of record (fresh connection per read)."""
    path = tmp_path / "sor.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE encounters ("
        "id INTEGER PRIMARY KEY, patient_id TEXT, type TEXT, note TEXT)"
    )
    conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, entry TEXT)")
    conn.commit()
    yield path, conn
    conn.close()


def _verifier(path: Path, *, query: str = QUERY) -> SqlRecordVerifier:
    return SqlRecordVerifier(
        lambda: sqlite3.connect(path),
        query,
        query_params={"patient_id": "p1"},
        poll_interval_s=0.01,
    )


def _insert(conn: sqlite3.Connection, note: str = "Follow-up") -> None:
    conn.execute(
        "INSERT INTO encounters (patient_id, type, note) VALUES ('p1', 'Triage', ?)",
        (note,),
    )
    conn.commit()


def _effect(**kwargs) -> Effect:
    defaults = dict(
        kind=EffectKind.RECORD_WRITTEN,
        match=TARGET,
        expected_count=1,
        timeout_s=0.05,
    )
    defaults.update(kwargs)
    return Effect(**defaults)


# -- read-only whitelist ------------------------------------------------------


class TestReadOnlyWhitelist:
    def test_select_and_with_pass(self):
        assert_read_only_sql("SELECT * FROM t")
        assert_read_only_sql("  with x as (select 1) select * from x")
        assert_read_only_sql("SELECT count(*) FROM t;")

    @pytest.mark.parametrize(
        "query",
        [
            "",
            "DELETE FROM t",
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET a=1",
            "DROP TABLE t",
            "SELECT * FROM t; DELETE FROM t",  # stacking
            "SELECT * INTO backup FROM t",  # SELECT INTO
            "WITH x AS (INSERT INTO t VALUES (1) RETURNING id) SELECT * FROM x",
            "SELECT * FROM t -- DELETE FROM t",  # comment smuggling
            "SELECT /* hidden */ * FROM t",
            "PRAGMA writable_schema=1",
            "select * from t where a = (select 1); attach database 'x' as y",
            # Side-effecting functions reachable from a bare SELECT (the DB
            # role is the real gate; these are the cheap lexical catches).
            "SELECT nextval('loan_seq')",
            "SELECT setval('loan_seq', 99)",
            "SELECT lo_import('/etc/passwd')",
            "SELECT dblink_exec('conn', 'x')",
            "SELECT load_extension('/tmp/evil.so')",
            "WITH slow AS (SELECT pg_sleep(30)) SELECT * FROM slow",
            "SELECT sleep(10)",
            "SELECT benchmark(100000000, sha1('x'))",
        ],
    )
    def test_mutating_or_smuggled_queries_refuse(self, query: str):
        with pytest.raises(ValueError):
            assert_read_only_sql(query)

    def test_filter_is_defense_in_depth_not_proof(self):
        """A lexically-clean SELECT calling an arbitrary UDF passes the
        filter — documented honestly: the read-only DB ROLE is the real
        enforcement (docs/EFFECT_KIT.md)."""
        assert_read_only_sql("SELECT some_custom_function(1) FROM t")

    def test_verifier_construction_enforces_whitelist(self, tmp_path: Path):
        with pytest.raises(ValueError):
            SqlRecordVerifier(lambda: None, "DELETE FROM encounters")


# -- verdict contract ---------------------------------------------------------


class TestSqlVerdicts:
    def test_confirmed_exactly_one_write(self, db):
        path, conn = db
        v = _verifier(path)
        before = v.capture_pre_state()
        assert before.reachable
        _insert(conn)
        verdict = v.verify(_effect(), before)
        assert verdict.verdict is Verdict.CONFIRMED
        assert verdict.observed_count == 1

    def test_refuted_missing_write(self, db):
        path, _conn = db
        v = _verifier(path)
        before = v.capture_pre_state()
        verdict = v.verify(_effect(), before)
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.should_halt

    def test_refuted_duplicate_write(self, db):
        path, conn = db
        v = _verifier(path)
        before = v.capture_pre_state()
        _insert(conn)
        _insert(conn)
        verdict = v.verify(_effect(), before)
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.observed_count == 2

    def test_field_equals_partial_save_caught(self, db):
        path, conn = db
        v = _verifier(path)
        before = v.capture_pre_state()
        _insert(conn, note="")  # row persisted, field dropped
        verdict = v.verify(
            _effect(kind=EffectKind.FIELD_EQUALS, field="note", value="Follow-up"),
            before,
        )
        assert verdict.verdict is Verdict.REFUTED
        assert "note" in verdict.reason

    def test_indeterminate_unreachable_database(self, tmp_path: Path):
        v = SqlRecordVerifier(
            lambda: sqlite3.connect(f"file:{tmp_path}/absent.db?mode=ro", uri=True),
            QUERY,
            query_params={"patient_id": "p1"},
        )
        before = v.capture_pre_state()
        assert not before.reachable
        verdict = v.verify(_effect(), before)
        assert verdict.verdict is Verdict.INDETERMINATE
        assert verdict.should_halt

    def test_count_new_only_ignores_preexisting_rows(self, db):
        """Duplicate-write guard: exactly one NEW row, baseline row ignored."""
        path, conn = db
        _insert(conn, note="historical")  # pre-existing matching row
        v = _verifier(path)
        before = v.capture_pre_state()
        effect = _effect(count_new_only=True)
        # Absolute counting would now see 1 (pre-existing) and CONFIRM with no
        # write at all; the delta guard refutes.
        assert v.verify(effect, before).verdict is Verdict.REFUTED
        _insert(conn, note="this run")
        assert v.verify(effect, before).verdict is Verdict.CONFIRMED
        _insert(conn, note="double submit")
        verdict = v.verify(effect, before)
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.observed_count == 2


# -- promoted exact table-delta audit ----------------------------------------


class TestTableDeltaAudit:
    def test_capture_and_exact_contract(self, db):
        path, conn = db
        connect = lambda: sqlite3.connect(path)  # noqa: E731
        tables = ["encounters", "audit_log"]
        before = capture_table_counts(connect, tables)
        assert before == {"encounters": 0, "audit_log": 0}
        _insert(conn)
        after = capture_table_counts(connect, tables)
        violations, deltas = audit_table_deltas(
            before, after, expected={"encounters": 1}
        )
        assert violations == []
        assert deltas == {"encounters": 1, "audit_log": 0}

    def test_unexpected_table_movement_is_a_violation(self, db):
        path, conn = db
        connect = lambda: sqlite3.connect(path)  # noqa: E731
        tables = ["encounters", "audit_log"]
        before = capture_table_counts(connect, tables)
        _insert(conn)
        conn.execute("INSERT INTO audit_log (entry) VALUES ('stray')")
        conn.commit()
        after = capture_table_counts(connect, tables)
        violations, _ = audit_table_deltas(before, after, expected={"encounters": 1})
        assert violations == ["audit_log:+1 (expected +0)"]

    def test_missing_expected_delta_is_a_violation(self, db):
        path, conn = db
        connect = lambda: sqlite3.connect(path)  # noqa: E731
        before = capture_table_counts(connect, ["encounters"])
        after = capture_table_counts(connect, ["encounters"])
        violations, _ = audit_table_deltas(before, after, expected={"encounters": 1})
        assert violations == ["encounters:+0 (expected +1)"]

    def test_identifier_smuggling_refused(self, db):
        path, _conn = db
        with pytest.raises(ValueError):
            capture_table_counts(
                lambda: sqlite3.connect(path), ['encounters"; DROP TABLE x; --']
            )

    def test_unreadable_audit_is_none_not_zero(self, tmp_path: Path):
        def connect():
            raise sqlite3.OperationalError("db down")

        assert capture_table_counts(connect, ["encounters"]) is None
