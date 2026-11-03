"""The fault gate: a record check is admitted only after it catches its faults.

See ``docs/VERIFICATION_COVERAGE.md`` section 2. Records are made up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openadapt_flow.fault_gate import (
    AdmissionPolicy,
    MemoryRestCopy,
    SqliteCopy,
    check_digest,
    load_coverage,
    run_gate,
    write_coverage,
)
from openadapt_flow.runtime.effects.surfaces import surface_probe
from openadapt_flow.verify_run import parse_verify_config

PARAMS = {"patient_id": "p1", "note": "Follow-up in two weeks", "type": "Triage"}
SEED = [{"id": 1, "patient_id": "p2", "type": "Consult", "note": "Allergy: penicillin"}]
TARGET = {"patient_id": "p1", "type": "Triage", "note": "Follow-up in two weeks"}


def _effects() -> list[dict]:
    match = {"patient_id": {"param": "patient_id"}, "note": {"param": "note"}}
    return [
        {
            "kind": "record_written",
            "match": match,
            "count_new_only": True,
            "timeout_s": 0.3,
        },
        {
            "kind": "field_equals",
            "match": match,
            "field": "type",
            "value": {"param": "type"},
            "timeout_s": 0.3,
        },
    ]


def _rest_cfg(base: str = "http://127.0.0.1:1"):
    return parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {"kind": "rest", "base_url": base, "records_path": "/api/db"},
            "effects": _effects(),
        }
    )


@pytest.fixture
def rest_copy():
    store = MemoryRestCopy(SEED, TARGET)
    try:
        yield store
    finally:
        store.close()


def test_a_full_read_kills_every_fault(rest_copy, tmp_path: Path) -> None:
    record = run_gate(_rest_cfg(), PARAMS, rest_copy, workdir=tmp_path)
    assert record.clean_pass
    survived = [c for c in record.cases if c["result"] == "SURVIVED"]
    assert not survived, survived
    assert record.complete
    assert set(record.classes) == {
        "wrong_value",
        "wrong_record",
        "unrelated_edit",
        "other_table",
        "side_write",
        "partial",
        "duplicate",
        "missing",
        "delayed",
    }
    # The store holds one collection: nothing outside the read to write.
    assert record.classes["other_table"].applicable == 0
    assert record.class_results()["side_write"] == "not_applicable"
    # One wrong-value case per declared field.
    assert record.classes["wrong_value"].applicable == 3
    assert record.killed == record.applicable == 9


def test_a_check_that_claims_no_frame_is_not_tested_on_one(
    rest_copy, tmp_path: Path
) -> None:
    record = run_gate(
        _rest_cfg(), PARAMS, rest_copy, claims=("delta",), workdir=tmp_path
    )
    assert record.classes["unrelated_edit"].applicable == 0
    assert record.applicable == 8


def _sqlite(tmp_path: Path) -> Path:
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE enc (id INTEGER PRIMARY KEY, patient_id TEXT, type TEXT, note TEXT);"
        "INSERT INTO enc VALUES (1, 'p2', 'Consult', 'Allergy: penicillin');"
    )
    con.commit()
    con.close()
    return db


def _sql_cfg(db: Path, *, scoped: bool):
    query = "SELECT id, patient_id, type, note FROM enc"
    verifier = {"kind": "sql", "sqlite_database": str(db), "sql_query": query}
    if scoped:
        verifier["sql_query"] = query + " WHERE patient_id = :p"
        verifier["sql_query_params"] = {"p": {"param": "patient_id"}}
    return parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": verifier,
            "effects": _effects(),
        }
    )


def test_a_read_scoped_to_the_patient_misses_the_unrelated_edit(tmp_path: Path) -> None:
    """The blind spot of #1365: the check can't see another patient's chart."""

    db = _sqlite(tmp_path)
    copy = SqliteCopy(db, "enc", TARGET, workdir=tmp_path / "copy")
    record = run_gate(
        _sql_cfg(db, scoped=True), PARAMS, copy, workdir=tmp_path / "gate"
    )
    assert record.clean_pass
    assert record.classes["unrelated_edit"].killed == 0
    assert not record.complete
    survived = [c["fault"] for c in record.cases if c["result"] == "SURVIVED"]
    assert survived == ["unrelated_edit"]


def test_the_same_table_read_whole_kills_it(tmp_path: Path) -> None:
    db = _sqlite(tmp_path)
    copy = SqliteCopy(db, "enc", TARGET, workdir=tmp_path / "copy")
    record = run_gate(
        _sql_cfg(db, scoped=False), PARAMS, copy, workdir=tmp_path / "gate"
    )
    assert record.complete, [c for c in record.cases if c["result"] != "killed"]


def test_the_check_digest_ignores_where_the_store_is() -> None:
    a = _rest_cfg("http://127.0.0.1:1")
    b = _rest_cfg("http://10.0.0.2:8080")
    assert check_digest(a) == check_digest(b)
    narrower = parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "rest",
                "base_url": "http://127.0.0.1:1",
                "records_path": "/api/db?patient=p1",
            },
            "effects": _effects(),
        }
    )
    assert check_digest(narrower) != check_digest(a)


def test_admission_policy(rest_copy, tmp_path: Path) -> None:
    cfg = _rest_cfg()
    record = run_gate(cfg, PARAMS, rest_copy, workdir=tmp_path / "gate")
    path = write_coverage(record, tmp_path)
    loaded = load_coverage(path)
    assert loaded.digest() == record.digest()
    policy = AdmissionPolicy(coverage=str(path))
    assert policy.problem(loaded, check_digest(cfg)) is None
    assert policy.problem(loaded, "sha256:" + "0" * 64) == "coverage_for_another_check"
    weak = loaded.model_copy(update={"killed": loaded.killed - 1})
    assert policy.problem(weak, check_digest(cfg)) == "coverage_below_policy"
    recorded = loaded.model_copy(update={"test_copy": "recorded_states"})
    assert policy.problem(recorded, check_digest(cfg)) == "coverage_test_copy_refused"
    strict = AdmissionPolicy(coverage=str(path), require_classes=["unrelated_edit"])
    no_frame = run_gate(
        cfg, PARAMS, rest_copy, claims=("delta",), workdir=tmp_path / "g2"
    )
    # Coverage measured without the frame can't admit: verify-run signs the
    # frame into every receipt.
    assert strict.problem(no_frame, check_digest(cfg)) == "coverage_claims_short:frame"
    assert policy.problem(no_frame, check_digest(cfg)) == "coverage_claims_short:frame"
    assert record.store_tables == 1
    # A store known to hold other tables: a one-collection copy is refused.
    emr = AdmissionPolicy(coverage=str(path), refuse_not_applicable=True)
    assert emr.problem(loaded, check_digest(cfg)) == (
        "coverage_class_not_applicable:other_table"
    )
    floor = AdmissionPolicy(coverage=str(path), min_store_tables=2)
    assert floor.problem(loaded, check_digest(cfg)) == "coverage_store_too_small"
    unknown = loaded.model_copy(update={"store_tables": None})
    assert floor.problem(unknown, check_digest(cfg)) == "coverage_store_too_small"


def test_a_missed_claimed_class_refuses_whatever_the_total(tmp_path: Path) -> None:
    """The one-table check of #1506: every in-table fault killed, the faults
    outside its read missed. A lower min_coverage doesn't admit it."""

    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE enc (id INTEGER PRIMARY KEY, patient_id TEXT, type TEXT, note TEXT);"
        "CREATE TABLE allergy (id INTEGER PRIMARY KEY, patient_id TEXT, title TEXT);"
        "INSERT INTO enc VALUES (1, 'p2', 'Consult', 'Allergy: penicillin');"
        "INSERT INTO allergy VALUES (1, 'p2', 'penicillin');"
    )
    con.commit()
    con.close()
    cfg = _sql_cfg(db, scoped=False)
    copy = SqliteCopy(db, "enc", TARGET, workdir=tmp_path / "copy")
    record = run_gate(cfg, PARAMS, copy, workdir=tmp_path / "gate")
    assert record.clean_pass
    results = record.class_results()
    assert results["unrelated_edit"] == "killed"
    assert results["other_table"] == results["side_write"] == "missed"
    assert record.killed == record.applicable - 2
    lenient = AdmissionPolicy(coverage="c.json", min_coverage=0.5)
    assert (
        lenient.problem(record, check_digest(cfg))
        == "coverage_class_missed:other_table"
    )
    detail = {c["fault"]: c["detail"] for c in record.cases}
    assert "allergy." in detail["other_table"]
    assert "allergy.patient_id" in detail["side_write"]

    # Coverage from the earlier suite has no outside-read classes: refused.
    old = record.model_copy(
        update={
            "classes": {
                k: v
                for k, v in record.classes.items()
                if k not in ("other_table", "side_write")
            }
        }
    )
    assert (
        lenient.problem(old, check_digest(cfg)) == "coverage_class_missing:other_table"
    )
    # A class the copy couldn't build is refused, not credited.
    untestable = record.model_copy(deep=True)
    untestable.classes["side_write"].untestable = 1
    assert untestable.class_results()["side_write"] == "untestable"
    # Coverage measured on the delta alone never admits the check.
    delta_only = run_gate(
        cfg, PARAMS, copy, claims=("delta",), workdir=tmp_path / "gate2"
    )
    assert "other_table" not in delta_only.class_results()
    assert lenient.problem(delta_only, check_digest(cfg)) == (
        "coverage_claims_short:frame"
    )
    assert record.store_tables == 2


def test_a_read_of_both_tables_kills_the_outside_faults(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE enc (id INTEGER PRIMARY KEY, patient_id TEXT, type TEXT, note TEXT);"
        "CREATE TABLE allergy (id INTEGER PRIMARY KEY, patient_id TEXT, title TEXT);"
        "INSERT INTO enc VALUES (1, 'p2', 'Consult', 'Allergy: penicillin');"
        "INSERT INTO allergy VALUES (1, 'p2', 'penicillin');"
    )
    con.commit()
    con.close()
    cfg = parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "surfaces",
                "surfaces": {
                    "enc": {
                        "kind": "sql",
                        "sqlite_database": str(db),
                        "sql_query": "SELECT id, patient_id, type, note FROM enc",
                    },
                    "allergy": {
                        "kind": "sql",
                        "sqlite_database": str(db),
                        "sql_query": "SELECT id, patient_id, title FROM allergy",
                    },
                },
            },
            "effects": [dict(e, probe=surface_probe("enc")) for e in _effects()],
        }
    )
    copy = SqliteCopy(db, "enc", TARGET, workdir=tmp_path / "copy")
    record = run_gate(cfg, PARAMS, copy, workdir=tmp_path / "gate")
    assert record.complete, [c for c in record.cases if c["result"] != "killed"]
    assert AdmissionPolicy(coverage="c.json").problem(record, check_digest(cfg)) is None


# -- verify-run under an admission policy ----------------------------------------


def test_verify_run_signs_the_coverage_it_was_admitted_with(
    rest_copy, tmp_path: Path
) -> None:
    import yaml

    from openadapt_flow.verify_run import (
        capture_baseline,
        load_verify_config,
        receipt_problem,
        verify_run,
    )

    record = run_gate(_rest_cfg(), PARAMS, rest_copy, workdir=tmp_path / "gate")
    write_coverage(record, tmp_path)
    doc = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "rest",
            "base_url": rest_copy.base_url,
            "records_path": "/api/db",
        },
        "effects": _effects(),
        "admission": {"coverage": "check_coverage.json"},
    }
    path = tmp_path / "verify.yaml"
    path.write_text(yaml.safe_dump(doc))

    def run(name: str) -> dict:
        cfg = load_verify_config(path)
        rest_copy.reset()
        baseline = tmp_path / f"{name}.json"
        capture_baseline(cfg, PARAMS, baseline)
        rest_copy.save()
        return verify_run(
            cfg,
            params=PARAMS,
            run_record={"executor_kind": "agent", "executor_id": "test-agent"},
            authorization_ref="ticket-0000-admission",
            baseline_path=baseline,
            private_dir=tmp_path / name,
        )["receipt"]

    # Coverage measured with the frame left out of the claims: verify-run
    # would sign frame: held with no frame fault tested, so it refuses.
    short = run_gate(
        _rest_cfg(), PARAMS, rest_copy, claims=("delta",), workdir=tmp_path / "g2"
    )
    write_coverage(short, tmp_path)
    narrowed = run("narrowed")
    assert narrowed["outcome"] == "reconciliation_required"
    assert narrowed["checked"]["coverage"]["policy"] == "coverage_claims_short:frame"
    write_coverage(record, tmp_path)

    admitted = run("admitted")
    assert admitted["outcome"] == "verified"
    assert admitted["checked"]["coverage"] == {
        **record.totals(),
        "policy": "met",
        "classes": record.class_results(),
    }
    # The memory store has one collection: nothing outside the read.
    assert admitted["checked"]["coverage"]["classes"]["other_table"] == (
        "not_applicable"
    )
    assert receipt_problem(admitted) is None

    (tmp_path / "check_coverage.json").unlink()
    missing = run("missing")
    assert missing["outcome"] == "reconciliation_required"
    assert missing["checked"]["coverage"]["policy"] == "coverage_missing"

    other = record.model_copy(update={"check_sha256": "sha256:" + "1" * 64})
    write_coverage(other, tmp_path)
    stale = run("stale")
    assert stale["checked"]["coverage"]["policy"] == "coverage_for_another_check"
    assert stale["outcome"] != "verified"
    forged = {
        **admitted,
        "checked": {
            **admitted["checked"],
            "coverage": {
                **admitted["checked"]["coverage"],
                "policy": "coverage_below_policy",
            },
        },
    }
    assert receipt_problem(forged) == "verified_without_every_part"


# -- scaffold-verifier on the gate ------------------------------------------------


def _scoped_db(tmp_path: Path):
    from tests._record_check_fixtures import NOTE, write_recording

    db = tmp_path / "app.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE encounters (id INTEGER PRIMARY KEY, patient_id TEXT, note TEXT);"
        "CREATE TABLE billing (id INTEGER PRIMARY KEY, patient_id TEXT, amount TEXT);"
    )
    con.execute("INSERT INTO encounters VALUES (1, 'p2', 'an older note')")
    con.execute("INSERT INTO encounters VALUES (2, 'p1', ?)", (NOTE,))
    con.execute("INSERT INTO billing VALUES (1, 'p2', '10.00')")
    con.commit()
    con.close()
    return db, write_recording(tmp_path / "rec", {"patient_id": "p1", "note": NOTE})


def test_the_gate_finds_a_read_scoped_to_the_patient(tmp_path: Path) -> None:
    """A read scoped to the patient can't see another patient's row in the
    same table. The recorded-state test can't show that; the gate on a
    database copy can."""

    from openadapt_flow import record_check_synthesis as rcs

    db, rec = _scoped_db(tmp_path)
    proposal = rcs.synthesize(rec, db_path=db)
    # Only the patient-scoped read, as synthesis built it before it learned
    # to read the target table whole.
    proposal.search.surfaces = [
        s for s in proposal.search.surfaces if "every row" not in s.source
    ]
    proposal.effects = rcs.effects_for(proposal.search.surfaces)
    proposal.verifier = rcs.verifier_section(proposal.search.surfaces)
    cov = rcs.gate_on_database(proposal, db, rcs.gate_config(proposal))
    assert cov is not None and cov.clean_pass
    survived = {c["fault"] for c in cov.cases if c["result"] == "SURVIVED"}
    # Nor an edit in billing: synthesis reads billing only to see that no
    # row was added, which catches the side write but not the edit.
    assert survived == {"unrelated_edit", "other_table"}

    recorded = rcs.coverage_from_recorded(proposal.faults, rcs.gate_config(proposal))
    assert recorded.test_copy == "recorded_states"
    assert not recorded.complete  # wrong_value and delayed untested


def test_scaffold_verifier_on_the_gate_reads_the_target_table_whole(
    tmp_path: Path,
) -> None:
    from openadapt_flow.record_check_synthesis import synthesize

    db, rec = _scoped_db(tmp_path)
    gated = synthesize(rec, db_path=db, fault_gate=True)
    assert any("every row" in s.source for s in gated.search.surfaces)
    cov = gated.coverage
    assert cov is not None and cov.test_copy == "sqlite"
    assert cov.classes["unrelated_edit"].killed == 1
    # The check reads encounters whole and billing only for "no row added":
    # it sees a billing row added for the saved patient, not a billing edit.
    # It claims a frame, so it is a draft, not a working check (#1506).
    results = cov.class_results()
    assert results["side_write"] == "killed"
    assert results["other_table"] == "missed"
    assert not gated.working
    assert any("other_table" in m for m in gated.misses)
    assert gated.proof()["coverage"]["test_copy"] == "sqlite"

    # The same check on a store with no other table is working.
    con = sqlite3.connect(db)
    con.execute("DROP TABLE billing")
    con.commit()
    con.close()
    alone = synthesize(rec, db_path=db, fault_gate=True)
    assert alone.coverage is not None and alone.coverage.complete
    assert alone.coverage.class_results()["other_table"] == "not_applicable"
    assert alone.working, alone.misses
