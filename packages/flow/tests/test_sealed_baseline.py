"""verify-run's baseline holds no record values: the read set is sealed.

The baseline keeps a keyed MAC of each record's identity and fields; one
read at judging time rebuilds the records. A change, a removal and an
addition all still show. Records are made up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openadapt_flow.runtime.effects.sealed import CHANGED, REMOVED, seal, unseal
from openadapt_flow.verify_run import capture_baseline, parse_verify_config, verify_run

PARAMS = {"fname": "Ada", "lname": "Quill", "email": "ada@example.test"}


def _setup(tmp_path: Path) -> tuple[Path, object]:
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE patient (id INTEGER PRIMARY KEY, fname TEXT, lname TEXT, "
        "email TEXT, phone TEXT);"
        "INSERT INTO patient VALUES (1, 'Wendell', 'Prior', 'w@example.test', '617');"
        "INSERT INTO patient VALUES (5, 'Mabel', 'Ortiz', 'm@example.test', '781');"
    )
    con.commit()
    con.close()
    cfg = parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(db),
                "sql_query": "SELECT * FROM patient",
            },
            "record": {
                "name": "patient",
                "match": ["email", "lname"],
                "fields": {"fname": "fname", "lname": "lname", "email": "email"},
            },
            "frame": {"identity_field": "id"},
        }
    )
    capture_baseline(cfg, PARAMS, tmp_path / "baseline.json")
    return db, cfg


def _judge(tmp_path: Path, *statements: str) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db, cfg = _setup(tmp_path)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO patient (fname, lname, email) "
        "VALUES ('Ada', 'Quill', 'ada@example.test')"
    )
    for statement in statements:
        con.execute(statement)
    con.commit()
    con.close()
    return verify_run(
        cfg,
        params=PARAMS,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="test",
        baseline_path=tmp_path / "baseline.json",
    )["receipt"]


def test_the_baseline_file_holds_no_record_values(tmp_path: Path) -> None:
    _setup(tmp_path)
    text = (tmp_path / "baseline.json").read_text()
    for value in ("Wendell", "Prior", "w@example.test", "Mabel", "m@example.test"):
        assert value not in text


def test_a_clean_save_still_verifies(tmp_path: Path) -> None:
    assert _judge(tmp_path)["outcome"] == "verified"


def test_a_change_and_a_removal_still_show(tmp_path: Path) -> None:
    changed = _judge(tmp_path / "a", "UPDATE patient SET phone = '999' WHERE id = 1")
    assert changed["checked"]["frame"] == "refuted"
    removed = _judge(tmp_path / "b", "DELETE FROM patient WHERE id = 5")
    assert removed["checked"]["frame"] == "refuted"


def test_unseal_rebuilds_what_it_can_and_marks_the_rest() -> None:
    key = b"k" * 32
    before = [{"id": 1, "a": "x", "b": "y"}, {"id": 2, "a": "z"}]
    sealed = seal(before, key, "id")
    now = [{"id": 1, "a": "x", "b": "moved"}, {"id": 3, "a": "new"}]
    rebuilt = unseal(sealed, now, key, "id")
    assert rebuilt[0] == {"id": 1, "a": "x", "b": CHANGED}
    assert rebuilt[1]["id"].startswith(REMOVED)
    assert all("new" not in str(r) for r in rebuilt)


# Adversarial review of #1599: each of these ended ``held`` with a sealed
# baseline, where the raw one refuted.

_KEY = b"k" * 32


def _frame(before: list[dict], after: list[dict], spec: dict | None = None) -> str:
    from openadapt_flow.runtime.effects.effect import Effect, EffectKind, EffectState
    from openadapt_flow.runtime.effects.frame import FrameSpec, judge_frame

    rebuilt = unseal(seal(before, _KEY, "id"), after, _KEY, "id")
    return judge_frame(
        FrameSpec(identity_field="id", **(spec or {})),
        [Effect(kind=EffectKind.RECORD_WRITTEN, match={"id": "9"})],
        {"": (EffectState(substrate="t", reachable=True, records=rebuilt), after)},
        params={},
        run_actors=["bot"],
    ).status


def test_a_value_that_keeps_its_text_but_not_its_type_shows_as_changed() -> None:
    new = {"id": 9, "flag": False, "coding": None}
    before = [{"id": 1, "flag": True, "coding": {"code": "E11"}}]
    assert _frame(before, [{**before[0], "flag": "true"}, new]) == "refuted"
    assert (
        _frame(before, [{**before[0], "coding": '{"code": "E11"}'}, new]) == "refuted"
    )
    assert (
        _frame([{"id": 1, "flag": None}], [{"id": 1, "flag": "\x00null"}, new])
        == "refuted"
    )


def test_no_stored_text_can_equal_the_changed_marker() -> None:
    before = [{"id": 1, "name": "Wendell"}]
    for written in ("\x00changed", "\x00removed:"):
        after = [{"id": 1, "name": written}, {"id": 9, "name": "Ada"}]
        assert _frame(before, after) == "refuted"
    assert CHANGED != "\x00changed" and not REMOVED.startswith("\x00removed:\x00")


def test_an_attribution_path_through_a_changed_field_is_unattributed() -> None:
    spec = {
        "attribution": {
            "kind": "record_field",
            "actor_field": "meta.lastUpdatedBy",
            "self": ["bot"],
            "server_set": ["meta.lastUpdatedBy", "meta.lastUpdated"],
        }
    }
    before = [
        {"id": 1, "note": "ok", "meta": {"lastUpdatedBy": "bob", "lastUpdated": "t0"}}
    ]
    after = [
        {
            "id": 1,
            "note": "EDITED",
            "meta": {"lastUpdatedBy": "bob", "lastUpdated": "t1"},
        },
        {"id": 9, "note": "new", "meta": {"lastUpdatedBy": "bot", "lastUpdated": "t1"}},
    ]
    assert _frame(before, after, spec) == "refuted"
