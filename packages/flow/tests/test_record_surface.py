"""``record.surface``: a record contract read from one of several surfaces.

The record shorthand (what ``qualify`` and ``verify-run`` read) can now name
the surface its record comes from, so a contract can also read other tables
for its frame: a stray write there refutes. Records are made up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openadapt_flow.verify_run import capture_baseline, parse_verify_config, verify_run


def _cfg(db: Path, surface: str | None = "patient") -> dict:
    record = {
        "name": "patient",
        "match": ["email"],
        "fields": {"email": "email", "lname": "lname"},
    }
    if surface:
        record["surface"] = surface
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "surfaces",
            "surfaces": {
                "patient": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": "SELECT * FROM patient",
                },
                "notes": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": "SELECT * FROM notes",
                },
            },
        },
        "record": record,
    }


def _run(tmp_path: Path, stray_note: bool) -> str:
    db = tmp_path / "s.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE patient (id INTEGER PRIMARY KEY, lname TEXT, email TEXT)")
    con.execute(
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, patient_id INTEGER, body TEXT)"
    )
    con.execute("INSERT INTO patient VALUES (1, 'Old', 'old@example.test')")
    con.commit()
    cfg = parse_verify_config(_cfg(db))
    params = {"email": "new@example.test", "lname": "New"}
    capture_baseline(cfg, params, tmp_path / "b.json")
    con.execute("INSERT INTO patient VALUES (2, 'New', 'new@example.test')")
    if stray_note:
        con.execute("INSERT INTO notes VALUES (1, 1, 'stray')")
    con.commit()
    con.close()
    return verify_run(
        cfg,
        params=params,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="t",
        baseline_path=tmp_path / "b.json",
    )["receipt"]["outcome"]


def test_the_record_is_read_from_its_surface(tmp_path: Path) -> None:
    assert _run(tmp_path, stray_note=False) == "verified"


def test_a_stray_write_in_another_surface_refutes(tmp_path: Path) -> None:
    assert _run(tmp_path, stray_note=True) == "reconciliation_required"


def test_an_unset_surface_keeps_the_contract_digest() -> None:
    from openadapt_flow.verify_run import (
        parse_verify_config as parse,
    )
    from openadapt_flow.verify_run import (
        verifier_identity,
        verify_config_sha256,
    )

    without_field = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {"kind": "sql", "sqlite_database": "x.db", "sql_query": "SELECT 1"},
        "record": {"name": "p", "match": ["email"], "fields": {"email": "email"}},
    }
    with_none = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {"kind": "sql", "sqlite_database": "x.db", "sql_query": "SELECT 1"},
        "record": {
            "name": "p",
            "match": ["email"],
            "fields": {"email": "email"},
            "surface": None,
        },
    }

    parsed_without = parse(without_field)
    parsed_none = parse(with_none)

    assert all("surface:" not in (e.probe or "") for e in parsed_without.effects)
    assert all("surface:" not in (e.probe or "") for e in parsed_none.effects)

    digest_without = verify_config_sha256(
        verifier_identity(parsed_without.verifier), list(parsed_without.effects)
    )
    digest_none = verify_config_sha256(
        verifier_identity(parsed_none.verifier), list(parsed_none.effects)
    )
    assert digest_without == digest_none


# -- a composite duplicate key read through a selected surface ---------------
#
# The record's checks are judged by ``record.surface``'s read, so that read
# must hold every record carrying the key. A non-key filter or a row limit on
# the selected read hides an older same-key row; both parsers must refuse it,
# not only when the read is the verifier's single read.

_HIDING_READS = [
    ("SELECT * FROM main_rows WHERE name = :name", "name"),
    ("SELECT * FROM main_rows LIMIT 1000", "limits its rows"),
]


def _keyed(db: Path, main_query: str, surface: str | None = "main") -> dict:
    record: dict = {
        "name": "row",
        "match": ["name"],
        "duplicate_key": ["group_key"],
        "fields": {"name": "name", "group_key": "group_key"},
    }
    if surface is not None:
        record["surface"] = surface
    return {
        "schema_version": "openadapt.verify/v1",
        "app": {
            "name": "Synthetic",
            "version": "1",
            "url": "http://127.0.0.1:9301/",
            "reset": {"command": "true"},
        },
        "verifier": {
            "kind": "surfaces",
            "surfaces": {
                "main": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": main_query,
                },
                "side": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": "SELECT * FROM side_rows",
                },
            },
        },
        "record": record,
    }


def _verify_run_parse(data: dict):
    from openadapt_flow.verify_run import parse_verify_config as parse

    data = {k: v for k, v in data.items() if k != "app"}
    return parse(data)


@pytest.mark.parametrize(("query", "message"), _HIDING_READS)
def test_verify_run_refuses_a_selected_read_that_may_miss_the_key(
    tmp_path: Path, query: str, message: str
) -> None:
    from openadapt_flow.verify_run import VerifyRunError

    with pytest.raises(VerifyRunError, match=message):
        _verify_run_parse(_keyed(tmp_path / "s.db", query))


@pytest.mark.parametrize(("query", "message"), _HIDING_READS)
def test_qualify_refuses_a_selected_read_that_may_miss_the_key(
    tmp_path: Path, query: str, message: str
) -> None:
    from openadapt_flow.qualify.config import QualifyConfigError
    from openadapt_flow.qualify.config import parse_verify_config as parse

    with pytest.raises(QualifyConfigError, match=message):
        parse(_keyed(tmp_path / "s.db", query), env={})


@pytest.mark.parametrize(
    ("surface", "message"),
    [(None, "record.surface"), ("nowhere", "nowhere")],
)
def test_a_key_read_through_surfaces_needs_a_known_surface(
    tmp_path: Path, surface: str | None, message: str
) -> None:
    from openadapt_flow.qualify.config import QualifyConfigError
    from openadapt_flow.qualify.config import parse_verify_config as parse
    from openadapt_flow.verify_run import VerifyRunError

    data = _keyed(tmp_path / "s.db", "SELECT * FROM main_rows", surface=surface)
    with pytest.raises(VerifyRunError, match=message):
        _verify_run_parse(data)
    with pytest.raises(QualifyConfigError, match=message):
        parse(data, env={})


def test_a_complete_selected_read_finds_the_older_same_key_row(
    tmp_path: Path,
) -> None:
    from openadapt_flow.qualify.config import parse_verify_config as parse

    db = tmp_path / "s.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE main_rows (id INTEGER PRIMARY KEY, name TEXT, group_key TEXT)"
    )
    con.execute("CREATE TABLE side_rows (id INTEGER PRIMARY KEY, body TEXT)")
    con.execute("INSERT INTO main_rows VALUES (1, 'older', 'g1')")
    con.commit()
    data = _keyed(db, "SELECT * FROM main_rows")
    parse(data, env={})
    cfg = _verify_run_parse(data)
    params = {"name": "newer", "group_key": "g1"}
    capture_baseline(cfg, params, tmp_path / "b.json")
    con.execute("INSERT INTO main_rows VALUES (2, 'newer', 'g1')")
    con.commit()
    con.close()
    outcome = verify_run(
        cfg,
        params=params,
        run_record={"executor_kind": "agent", "executor_id": "bot"},
        authorization_ref="t",
        baseline_path=tmp_path / "b.json",
    )["receipt"]["outcome"]
    assert outcome == "reconciliation_required"
