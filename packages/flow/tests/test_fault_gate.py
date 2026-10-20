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
        "partial",
        "duplicate",
        "missing",
        "delayed",
    }
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
    assert strict.problem(no_frame, check_digest(cfg)) == "coverage_class_missing"


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

    admitted = run("admitted")
    assert admitted["outcome"] == "verified"
    assert admitted["checked"]["coverage"] == {**record.totals(), "policy": "met"}
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
    assert survived == {"unrelated_edit"}

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
    assert cov.complete, [c for c in cov.cases if c["result"] != "killed"]
    assert gated.working, gated.misses
    assert gated.proof()["coverage"]["test_copy"] == "sqlite"
