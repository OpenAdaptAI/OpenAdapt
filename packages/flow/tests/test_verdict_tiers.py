"""Verdict tiers, entity-scoped frames, the retry wait, shadow mode and the
safety bar (``docs/VERDICT_TIERS.md``), on a real SQLite store.

A whole-database frame stops a correct write when a row moves anywhere: a
task queue, a cache the app recomputes. With ``frame.database.entity_tables``
those rows are logged and the write passes; everything the safety bar names
(a sibling record edited, another user's row, another column of the actor's
row, a removed row, a duplicate) still stops it. Records are made up.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import pytest
import yaml

from openadapt_flow import enforcement, learn, learn_entities
from openadapt_flow.runtime.effects import admission
from openadapt_flow.runtime.effects.admission import FaultResult
from openadapt_flow.verifier import (
    Actor,
    Contract,
    await_effects,
    begin_write,
    verify_write,
)

ACTOR = "2"


@pytest.fixture(autouse=True)
def _homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.setenv("OPENADAPT_GATE_DIR", str(tmp_path / "gates"))
    monkeypatch.setenv("OPENADAPT_VERIFY_RETRY_S", "0")
    monkeypatch.delenv("OPENADAPT_ENFORCEMENT", raising=False)
    monkeypatch.delenv("OPENADAPT_GATE_CLEAN_RUNS", raising=False)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        for sql in (
            "CREATE TABLE app_user (id INTEGER PRIMARY KEY, name TEXT, email TEXT)",
            "CREATE TABLE project (id INTEGER PRIMARY KEY, name TEXT)",
            "CREATE TABLE entry (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "project_id INTEGER, note TEXT, "
            "FOREIGN KEY (user_id) REFERENCES app_user(id), "
            "FOREIGN KEY (project_id) REFERENCES project(id))",
            "CREATE TABLE job_queue (id INTEGER PRIMARY KEY, state TEXT, beat TEXT)",
            "CREATE TABLE project_total (id INTEGER PRIMARY KEY, project_id INTEGER, "
            "hours INTEGER)",
            "INSERT INTO app_user VALUES (1, 'admin', 'a@example.test'), "
            "(2, 'agentbot', 'b@example.test'), (3, 'colleague', 'c@example.test')",
            "INSERT INTO project VALUES (1, 'Website'), (2, 'Audit')",
            "INSERT INTO entry VALUES (1, 3, 1, 'Earlier entry')",
            "INSERT INTO job_queue VALUES (1, 'idle', 't0'), (2, 'done', 't0')",
            "INSERT INTO project_total VALUES (1, 1, 2)",
        ):
            self.run(sql)

    def run(self, sql: str, *args: Any) -> None:
        con = sqlite3.connect(self.path)
        con.execute(sql, args)
        con.commit()
        con.close()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "app.sqlite")


ENTITIES = ["entry", "project", "app_user"]


def contract(
    s: Store, entity_tables: Optional[list[str]] = None, query: str = ""
) -> dict:
    db: dict[str, Any] = {
        "dialect": "sqlite",
        "read": {"sqlite_database": str(s.path)},
        "views": {"entry": ""},
        "new_row_fields": {"entry": ["id"]},
        "settle_s": 0,
    }
    if entity_tables is not None:
        db["entity_tables"] = entity_tables
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "sql",
            "sqlite_database": str(s.path),
            "sql_query": query or "SELECT * FROM entry",
        },
        "effects": [
            {
                "kind": "record_written",
                "match": {
                    "note": {"param": "note"},
                    "user_id": {"param": "user"},
                    "project_id": {"literal": "1"},
                },
                "expected_count": 1,
                "count_new_only": True,
            }
        ],
        "frame": {"identity_field": "id", "database": db},
    }


def check(doc: dict, act: Callable[[], None], tmp_path: Path, note: str = "New") -> Any:
    c = Contract.from_dict(doc, name="c")
    pending = begin_write(
        c,
        {"note": note, "user": ACTOR},
        actor=Actor("agent", ACTOR),
        ledger=None,
        workdir=tmp_path / f"w{len(list(tmp_path.glob('w*')))}",
    )
    act()
    return verify_write(pending, seal=False)


def write(s: Store, note: str = "New") -> None:
    s.run("INSERT INTO entry (user_id, project_id, note) VALUES (2, 1, ?)", note)


def background(s: Store) -> None:
    """What the app does on its own: a queue heartbeat and a recomputed total."""

    s.run("UPDATE job_queue SET beat = 't1', state = 'busy' WHERE id = 1")
    s.run("INSERT INTO job_queue (state, beat) VALUES ('new', 't1')")
    s.run("UPDATE project_total SET hours = 3 WHERE project_id = 1")


# -- tiers --------------------------------------------------------------------------


def test_without_a_scope_background_rows_still_refute(
    store: Store, tmp_path: Path
) -> None:
    r = check(contract(store), lambda: (write(store), background(store)), tmp_path)
    assert r.outcome.value == "REFUTED"
    assert r.tier == "hard"
    assert r.side_effects == []


def test_with_a_scope_background_rows_are_logged_and_the_write_passes(
    store: Store, tmp_path: Path
) -> None:
    r = check(
        contract(store, ENTITIES), lambda: (write(store), background(store)), tmp_path
    )
    assert r.outcome.value == "CONFIRMED", r.reasons
    assert r.tier == "logged"
    by = {(x["table"], x["change"]): x for x in r.side_effects}
    assert by[("job_queue", "changed")]["columns"] == ["beat", "state"]
    assert by[("job_queue", "added")]["rows"] == 1
    assert by[("project_total", "changed")]["columns"] == ["hours"]
    # Names only: no keys and no values reach the evidence.
    assert "t1" not in json.dumps(r.side_effects)
    assert r.to_dict()["tier"] == "logged"


def test_a_clean_write_is_clean(store: Store, tmp_path: Path) -> None:
    r = check(contract(store, ENTITIES), lambda: write(store), tmp_path)
    assert r.outcome.value == "CONFIRMED" and r.tier == "clean"


@pytest.mark.parametrize(
    "harm",
    [
        # A sibling business record edited.
        "UPDATE entry SET note = 'Changed' WHERE id = 1",
        # Another user's row of the acting user's table.
        "UPDATE app_user SET email = 'x@example.test' WHERE id = 3",
        # Another column of the actor's own row.
        "UPDATE app_user SET email = 'x@example.test' WHERE id = 2",
        # A record the write's location reads through.
        "UPDATE project SET name = 'Renamed' WHERE id = 1",
        # A removed row, even outside the entity tables.
        "DELETE FROM job_queue WHERE id = 2",
        # A duplicate of the written record.
        "INSERT INTO entry (user_id, project_id, note) VALUES (2, 1, 'New')",
    ],
)
def test_the_safety_bar_still_stops_under_a_scope(
    store: Store, tmp_path: Path, harm: str
) -> None:
    r = check(
        contract(store, ENTITIES),
        lambda: (write(store), background(store), store.run(harm)),
        tmp_path,
    )
    assert r.outcome.value == "REFUTED", harm
    assert r.blocks is False  # shadow by default: refuted, recorded, not blocking


def test_a_wrong_value_still_refutes_under_a_scope(
    store: Store, tmp_path: Path
) -> None:
    r = check(
        contract(store, ENTITIES),
        lambda: (write(store, "Wrong"), background(store)),
        tmp_path,
        note="New",
    )
    assert r.outcome.value != "CONFIRMED"


def test_the_frames_own_tables_are_always_entities(store: Store) -> None:
    c = Contract.from_dict(contract(store, []), name="c")
    scope = c.config.frame.database.entity_scope(c.config.frame)
    assert scope is not None and "entry" in scope  # views


def test_a_derived_check_column_never_moves_the_frame(
    store: Store, tmp_path: Path
) -> None:
    # pin__queue is a check computed by the read; it flips with a table the
    # scope logs, on every row, with no write to the row.
    q = "SELECT e.*, (SELECT COUNT(*) FROM job_queue) AS pin__queue FROM entry e"
    r = check(
        contract(store, ENTITIES, query=q),
        lambda: (write(store), background(store)),
        tmp_path,
    )
    assert r.outcome.value == "CONFIRMED", r.reasons


# -- retry ----------------------------------------------------------------------------


def test_the_retry_wait_waits_for_a_slow_commit(store: Store) -> None:
    c = Contract.from_dict(contract(store), name="c")
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        if len(slept) == 2:
            write(store)

    reads = await_effects(c, {"note": "New", "user": ACTOR}, budget_s=8, sleep=sleep)
    assert reads == 3
    assert slept == [0.5, 1.0]


def test_the_retry_wait_gives_up_within_its_budget(store: Store) -> None:
    c = Contract.from_dict(contract(store), name="c")
    slept: list[float] = []
    await_effects(c, {"note": "Never", "user": ACTOR}, budget_s=4, sleep=slept.append)
    assert sum(slept) <= 4 and slept == [0.5, 1.0, 2.0]


def test_the_retry_wait_never_reads_through_an_api() -> None:
    doc = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {"kind": "rest", "base_url": "http://127.0.0.1:1", "path": "/x"},
        "effects": [{"kind": "record_written", "match": {"a": {"literal": "1"}}}],
    }
    try:
        c = Contract.from_dict(doc, name="c")
    except Exception:  # noqa: BLE001 - a config this build rejects reads nothing either
        return
    assert await_effects(c, {}, budget_s=8, sleep=lambda s: None) == 0


# -- shadow mode ----------------------------------------------------------------------


def test_a_new_contract_is_shadow_until_n_clean_checks(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENADAPT_GATE_CLEAN_RUNS", "20")
    doc = contract(store, ENTITIES)
    digest = Contract.from_dict(doc, name="c").digest
    bad = check(
        doc,
        lambda: (write(store, "x0"), store.run("DELETE FROM job_queue WHERE id = 2")),
        tmp_path,
        note="x0",
    )
    assert bad.enforcement == "shadow" and not bad.blocks
    for i in range(19):
        check(doc, lambda i=i: write(store, f"n{i}"), tmp_path, note=f"n{i}")
    st = enforcement.status(digest)
    assert st.clean_in_a_row == 19 and not st.offered
    with pytest.raises(enforcement.GateRefused):
        enforcement.enable(digest, "A. Reviewer")
    check(doc, lambda: write(store, "n19"), tmp_path, note="n19")
    assert enforcement.status(digest).offered
    enforcement.enable(digest, "A. Reviewer")
    harm = check(
        doc,
        lambda: (
            write(store, "n20"),
            store.run("UPDATE entry SET note = 'z' WHERE id = 1"),
        ),
        tmp_path,
        note="n20",
    )
    assert harm.enforcement == "gating" and harm.blocks


def test_a_stop_labelled_caught_keeps_the_run(tmp_path: Path) -> None:
    class R:
        def __init__(self, wid: str, outcome: str) -> None:
            self.write_id = wid
            self.outcome = type("O", (), {"value": outcome})()
            self.tier = ""

    d = "sha256:" + "0" * 64
    enforcement.observe(d, R("a", "CONFIRMED"))
    enforcement.observe(d, R("b", "REFUTED"))
    enforcement.observe(d, R("c", "CONFIRMED"))
    assert enforcement.status(d).clean_in_a_row == 1
    enforcement.label(d, "b", "caught")
    assert enforcement.status(d).clean_in_a_row == 3
    enforcement.label(d, "b", "false_stop")
    st = enforcement.status(d)
    assert st.clean_in_a_row == 1 and st.false_stops == 1


def test_n_is_kept_between_20_and_50(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw, want in (("5", 20), ("35", 35), ("500", 50), ("x", 30)):
        monkeypatch.setenv("OPENADAPT_GATE_CLEAN_RUNS", raw)
        assert enforcement.clean_runs_needed() == want


def test_the_environment_can_gate_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_ENFORCEMENT", "gating")
    assert enforcement.status("sha256:" + "1" * 64).mode == "gating"


# -- the learner ----------------------------------------------------------------------


def test_logged_side_effects_are_offered_to_the_learner(
    store: Store, tmp_path: Path
) -> None:
    r = check(
        contract(store, ENTITIES), lambda: (write(store), background(store)), tmp_path
    )
    ep = learn.episode_record(type("E", (), {"kind": "sql"})(), r)
    assert ep.get("logged") is True
    assert {m["table"] for m in ep["moves"]} == {"job_queue", "project_total"}
    assert learn._why_ignored(ep) is None


# -- the safety bar -------------------------------------------------------------------


def _all(caught: bool = True) -> list[FaultResult]:
    return [
        FaultResult(f"{c}-1", c, caught)
        for c in (*admission.SAFETY_BAR, *admission.ACTOR_ATTACKS)
    ]


def test_admitted_only_when_every_class_ran_and_was_caught() -> None:
    assert admission.admit("x", _all()).admitted
    missing = [r for r in _all() if r.fault_class != "lost_update"]
    d = admission.admit("x", missing)
    assert not d.admitted and "lost_update" in d.reasons[0]


@pytest.mark.parametrize("cls", [*admission.SAFETY_BAR, *admission.ACTOR_ATTACKS])
def test_a_relaxation_that_lets_any_planted_fault_through_is_refused(cls: str) -> None:
    results = [
        r if r.fault_class != cls else FaultResult(r.case, cls, False) for r in _all()
    ]
    d = admission.admit(
        "x", results, required=(*admission.SAFETY_BAR, *admission.ACTOR_ATTACKS)
    )
    assert not d.admitted and any(cls in why for why in d.reasons)


def test_a_case_caught_before_must_still_be_run() -> None:
    before = _all() + [FaultResult("extra", "sibling_edit", True)]
    d = admission.admit("x", _all(), before)
    assert not d.admitted and "extra" in d.reasons[0]


def test_fault_gate_names_count_for_the_bar() -> None:
    results = [
        FaultResult("a", "wrong_record", True),
        FaultResult("b", "unrelated_edit", True),
        *[
            FaultResult(c, c, True)
            for c in ("wrong_value", "duplicate", "late_revert", "lost_update")
        ],
    ]
    assert admission.admit("x", results).admitted


def _setup(store: Store, tmp_path: Path) -> Path:
    folder = tmp_path / "setup"
    folder.mkdir()
    (folder / "c.create.yaml").write_text(
        yaml.safe_dump(contract(store)), encoding="utf-8"
    )
    (folder / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "actor_params": ["user"],
                "kinds": {
                    "log": {
                        "main_table": "entry",
                        "variants": [{"create": "c.create.yaml"}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return folder / "manifest.yaml"


def _schema(store: Store) -> Any:
    """The store's schema as ``generate schema`` writes it (SQLite has no
    schema reader; the shape is the same)."""

    from openadapt_flow.generate.sql_store import Column, ForeignKey, Schema, Table

    con = sqlite3.connect(store.path)
    tables = {}
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        cols = [
            Column(r[1], r[2] or "TEXT", not r[3], r[4])
            for r in con.execute(f"PRAGMA table_info({name})")
        ]
        pk = [r[1] for r in con.execute(f"PRAGMA table_info({name})") if r[5]]
        fks = [
            ForeignKey(r[3], r[2], r[4])
            for r in con.execute(f"PRAGMA foreign_key_list({name})")
        ]
        tables[name] = Table(name, cols, pk, fks)
    con.close()
    return Schema("sqlite", "main", tables)


def test_entity_scope_is_derived_staged_and_applied_only_on_evidence(
    store: Store, tmp_path: Path
) -> None:
    manifest = _setup(store, tmp_path)
    proposal = learn_entities.derive(manifest, _schema(store))
    assert proposal["actor_table"] == "app_user"
    assert set(proposal["entity_tables"]) == {"entry", "app_user"}
    assert "job_queue" not in proposal["entity_tables"]
    staged = learn_entities.stage(proposal, tmp_path / "candidate")
    doc = yaml.safe_load((staged.parent / "c.create.yaml").read_text())
    assert doc["frame"]["database"]["entity_tables"] == proposal["entity_tables"]
    faults = tmp_path / "faults.json"

    def results(caught: bool, digest: str) -> None:
        faults.write_text(
            json.dumps(
                {
                    "schema": admission.SCHEMA,
                    "proposal_sha256": digest,
                    "cases": [
                        {"case": r.case, "class": r.fault_class, "caught": caught}
                        for r in _all()
                    ],
                }
            )
        )

    results(True, "sha256:" + "f" * 64)
    with pytest.raises(admission.AdmissionRefused):
        learn_entities.apply(proposal, faults, "A. Reviewer")
    results(False, proposal["proposal_sha256"])
    with pytest.raises(admission.AdmissionRefused):
        learn_entities.apply(proposal, faults, "A. Reviewer")
    original = (manifest.parent / "c.create.yaml").read_text()
    assert "entity_tables" not in original
    results(True, proposal["proposal_sha256"])
    learn_entities.apply(proposal, faults, "A. Reviewer")
    doc = yaml.safe_load((manifest.parent / "c.create.yaml").read_text())
    assert doc["frame"]["database"]["entity_tables"] == proposal["entity_tables"]


def test_no_scope_without_the_acting_users_table(store: Store, tmp_path: Path) -> None:
    manifest = _setup(store, tmp_path)
    data = yaml.safe_load(manifest.read_text())
    data["actor_params"] = []
    manifest.write_text(yaml.safe_dump(data))
    with pytest.raises(learn_entities.EntityScopeError):
        learn_entities.derive(manifest, _schema(store))


def test_tables_in_sql() -> None:
    q = (
        "SELECT CAST(t.`id` AS CHAR), (SELECT a.`name` FROM `projects` a WHERE a.id = "
        't.project_id) FROM `entries` t JOIN "public"."tags" g ON 1=1'
    )
    assert learn_entities.tables_in_sql(q) == {"projects", "entries", "public.tags"}


def test_settling_ignores_motion_outside_the_scope(
    store: Store, tmp_path: Path
) -> None:
    stop = threading.Event()

    def churn() -> None:
        n = 0
        while not stop.is_set():
            n += 1
            store.run("UPDATE job_queue SET beat = ? WHERE id = 1", f"b{n}")
            stop.wait(0.02)

    doc = contract(store, ENTITIES)
    doc["frame"]["database"]["settle_s"] = 0.1
    doc["frame"]["database"]["settle_attempts"] = 2
    t = threading.Thread(target=churn, daemon=True)
    try:
        r = check(doc, lambda: (write(store), t.start()), tmp_path)
    finally:
        stop.set()
        t.join(timeout=5)
    assert r.outcome.value == "CONFIRMED", r.reasons
