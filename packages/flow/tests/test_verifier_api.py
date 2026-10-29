"""The stable verifier API: capture, write, verify_write.

MCP skills, the RL reward and the registry call ``openadapt_flow.verifier``.
These tests pin its public signatures and check it end to end on a SQLite
store. Records are made up.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier
from openadapt_flow.verifier import (
    Actor,
    BaselineUnreadable,
    Contract,
    RecheckResult,
    RecheckState,
    Snapshot,
    VerifierError,
    WriteResult,
    capture,
    capture_database,
    diff_database,
    recheck,
    verify_write,
)

BOT = Actor(kind="agent", id="bot")


@pytest.fixture(autouse=True)
def _used_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test records used snapshots in its own directory."""

    used = tmp_path / "used"
    monkeypatch.setattr(verifier, "USED_DIR", used)
    return used


PARAMS = {"fname": "Ada", "lname": "Quill", "email": "ada@example.test"}


def _store(tmp_path: Path) -> Path:
    path = tmp_path / "app.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE patient (id INTEGER PRIMARY KEY, fname TEXT, lname TEXT, "
        "email TEXT, phone TEXT);"
        "INSERT INTO patient VALUES (1, 'Wendell', 'Prior', 'w@example.test', '1');"
    )
    con.commit()
    con.close()
    return path


def _doc(db: Path) -> dict[str, Any]:
    return {
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


def _contract(db: Path) -> Contract:
    return Contract.from_dict(_doc(db))


def _write(db: Path, *statements: str) -> None:
    con = sqlite3.connect(db)
    for statement in statements:
        con.execute(statement)
    con.commit()
    con.close()


SAVE = (
    "INSERT INTO patient (fname, lname, email) "
    "VALUES ('Ada', 'Quill', 'ada@example.test')"
)


def _check(tmp_path: Path, *statements: str) -> WriteResult:
    db = _store(tmp_path)
    contract = _contract(db)
    before = capture(contract, PARAMS)
    _write(db, *statements)
    return verify_write(contract, before, params=PARAMS, actor=BOT)


def test_a_correct_write_is_verified(tmp_path: Path) -> None:
    result = _check(tmp_path, SAVE)
    assert result.verified and result.outcome == "verified"
    assert result.status == "verified"
    assert len(result.receipt_sha256) == 64
    assert result.checked["delta"] == "held" and result.checked["frame"] == "held"
    assert result.reasons == []
    assert result.receipt["outcome"] == "verified"


def test_a_wrong_value_is_not_verified(tmp_path: Path) -> None:
    wrong = SAVE.replace("'Ada', 'Quill'", "'Eda', 'Quill'")
    result = _check(tmp_path, wrong)
    assert not result.verified
    assert result.checked["delta"] != "held"
    assert result.reasons


def test_a_stray_write_is_not_verified(tmp_path: Path) -> None:
    result = _check(tmp_path, SAVE, "UPDATE patient SET phone = '2' WHERE id = 1")
    assert not result.verified and result.status == "refuted"
    assert result.checked["frame"] == "refuted"
    assert any("outside the declared write" in r for r in result.reasons)


def test_no_write_is_not_verified(tmp_path: Path) -> None:
    result = _check(tmp_path)
    assert not result.verified
    # The store doesn't show the write: it may still land, so reconcile.
    assert result.status == "unconfirmed"


def test_an_unreadable_store_stops_before_the_write(tmp_path: Path) -> None:
    contract = _contract(tmp_path / "missing" / "app.sqlite")
    with pytest.raises(BaselineUnreadable):
        capture(contract, PARAMS)


def test_an_unknown_actor_kind_is_refused(tmp_path: Path) -> None:
    db = _store(tmp_path)
    contract = _contract(db)
    before = capture(contract, PARAMS)
    with pytest.raises(VerifierError):
        verify_write(contract, before, params=PARAMS, actor=Actor("robot", "x"))


def test_the_database_diff_names_what_moved_without_values(tmp_path: Path) -> None:
    db = _store(tmp_path)
    read = {"sqlite_database": str(db)}
    before = capture_database(read, "sqlite")
    _write(db, SAVE, "UPDATE patient SET phone = '2' WHERE id = 1")
    moved = diff_database(before, capture_database(read, "sqlite"))
    assert set(moved) == {"patient"}
    assert (moved["patient"].added, moved["patient"].changed) == (1, 1)
    assert moved["patient"].changed_columns == ["phone"]
    # Undone: back where it was.
    _write(db, "DELETE FROM patient WHERE id = 2", "UPDATE patient SET phone = '1'")
    assert diff_database(before, capture_database(read, "sqlite")) == {}


def test_a_snapshot_verifies_one_write(tmp_path: Path) -> None:
    db = _store(tmp_path)
    contract = _contract(db)
    before = capture(contract, PARAMS)
    _write(db, SAVE)
    verify_write(contract, before, params=PARAMS, actor=BOT)
    with pytest.raises(VerifierError):
        verify_write(contract, before, params=PARAMS, actor=BOT)


def test_a_snapshot_crosses_a_process_boundary(tmp_path: Path) -> None:
    db = _store(tmp_path)
    contract = _contract(db)
    path = capture(contract, PARAMS).save(tmp_path / "before.json")
    assert path.stat().st_mode & 0o777 == 0o600
    _write(db, SAVE)
    result = verify_write(contract, Snapshot.load(path), params=PARAMS, actor=BOT)
    assert result.verified


def test_a_bad_contract_raises(tmp_path: Path) -> None:
    with pytest.raises(VerifierError):
        Contract.from_dict({"schema_version": "openadapt.verify/v1"})


def test_the_public_api_is_stable() -> None:
    assert sorted(verifier.__all__) == [
        "Actor",
        "BaselineUnreadable",
        "Contract",
        "DatabaseSnapshot",
        "RecheckNotDue",
        "RecheckResult",
        "RecheckState",
        "Snapshot",
        "Source",
        "TableDiff",
        "VerifierError",
        "WriteResult",
        "capture",
        "capture_database",
        "diff_database",
        "recheck",
        "verify_write",
    ]

    def params(fn: Any) -> list[tuple[str, str]]:
        return [
            (name, p.kind.name) for name, p in inspect.signature(fn).parameters.items()
        ]

    assert params(capture) == [
        ("contract", "POSITIONAL_OR_KEYWORD"),
        ("params", "POSITIONAL_OR_KEYWORD"),
    ]
    assert params(verify_write) == [
        ("contract", "POSITIONAL_OR_KEYWORD"),
        ("before", "POSITIONAL_OR_KEYWORD"),
        ("params", "KEYWORD_ONLY"),
        ("actor", "KEYWORD_ONLY"),
        ("source", "KEYWORD_ONLY"),
        ("authorization_ref", "KEYWORD_ONLY"),
        ("write_attempted", "KEYWORD_ONLY"),
    ]
    assert [n for n in inspect.signature(capture_database).parameters] == [
        "read",
        "dialect",
        "allow_tables",
        "max_rows",
        "key",
        "profile",
        "before",
        "actor_names",
    ]
    assert [f for f in WriteResult.__dataclass_fields__] == [
        "verified",
        "status",
        "outcome",
        "checked",
        "reasons",
        "receipt",
        "receipt_sha256",
        "evidence",
        "recheck",
        "recheck_due_at",
    ]
    assert params(recheck) == [
        ("contract", "POSITIONAL_OR_KEYWORD"),
        ("write", "POSITIONAL_OR_KEYWORD"),
        ("params", "KEYWORD_ONLY"),
        ("actor", "KEYWORD_ONLY"),
        ("excused_by", "KEYWORD_ONLY"),
        ("wait", "KEYWORD_ONLY"),
    ]
    assert [f for f in RecheckResult.__dataclass_fields__] == [
        "outcome",
        "frame",
        "reasons",
        "receipt_sha256",
        "post_save_read_at",
        "due_at",
        "read_at",
        "excused",
        "evidence",
    ]
    assert list(inspect.signature(RecheckState.load).parameters) == [
        "path",
        "params",
    ]


def test_diff_database_applies_the_profile_and_before(tmp_path: Path) -> None:
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, uuid TEXT,"
        " last_updated TEXT);"
        "CREATE TABLE users_secure (id INTEGER PRIMARY KEY, username TEXT,"
        " password TEXT, login_fail_counter INTEGER, last_update TEXT);"
        "INSERT INTO users VALUES (1, 'clinic2', 'u1', 'a'), (2, 'bot', 'u2', 'a');"
        "INSERT INTO users_secure VALUES (1, 'clinic2', 'h1', 0, 'a'), (2, 'bot', 'h2', 0, 'a');"
    )
    con.commit()
    con.close()
    read = {"sqlite_database": str(db)}
    args = {"profile": "openemr", "actor_names": ["bot"]}
    before = capture_database(read, "sqlite", **args)
    _write(
        db,
        "UPDATE users_secure SET login_fail_counter = 1, last_update = 'b' WHERE id = 2",
    )
    after = capture_database(read, "sqlite", before=before, **args)
    # The acting user's own sign-in bookkeeping: back where it was.
    assert diff_database(before, after, **args) == {}
    _write(db, "UPDATE users_secure SET password = 'x' WHERE id = 1")
    after = capture_database(read, "sqlite", before=before, **args)
    assert set(diff_database(before, after, **args)) == {"users_secure"}


def test_a_reloaded_or_copied_snapshot_is_still_single_use(tmp_path: Path) -> None:
    """Adversarial review of #1588: one write must not earn two rewards."""

    import copy

    db = _store(tmp_path)
    contract = _contract(db)
    first = capture(contract, PARAMS)
    path = first.save(tmp_path / "before.json")
    _write(db, SAVE)
    assert verify_write(
        contract, Snapshot.load(path), params=PARAMS, actor=BOT
    ).verified
    for reuse in (
        Snapshot.load(path),
        first,
        Snapshot(copy.deepcopy(first.payload)),
    ):
        with pytest.raises(VerifierError):
            verify_write(contract, reuse, params=PARAMS, actor=BOT)


def test_a_snapshot_from_another_contract_or_unbound_params_raise_verifier_error(
    tmp_path: Path,
) -> None:
    db = _store(tmp_path)
    contract = _contract(db)
    (tmp_path / "other").mkdir()
    other_db = _store(tmp_path / "other")
    with pytest.raises(VerifierError):
        verify_write(
            _contract(other_db), capture(contract, PARAMS), params=PARAMS, actor=BOT
        )
    with pytest.raises(VerifierError):
        verify_write(
            contract, capture(contract, PARAMS), params={"fname": "Ada"}, actor=BOT
        )


def test_a_snapshot_repr_shows_no_values_or_salt(tmp_path: Path) -> None:
    snapshot = capture(_contract(_store(tmp_path)), PARAMS)
    assert str(snapshot.payload["salt"]) not in repr(snapshot)
    assert "Wendell" not in repr(snapshot)


def test_a_used_snapshot_is_refused_in_another_process(
    tmp_path: Path, _used_dir: Path
) -> None:
    """Re-review of #1588: a copied file, or a snapshot pickled to a worker,
    must not verify a second write in a new process."""

    import shutil
    import subprocess
    import sys

    db = _store(tmp_path)
    contract = _contract(db)
    first = capture(contract, PARAMS)
    path = first.save(tmp_path / "before.json")
    _write(db, SAVE)
    assert verify_write(
        contract, Snapshot.load(path), params=PARAMS, actor=BOT
    ).verified
    copy = tmp_path / "copy.json"
    shutil.copy(path, copy)
    script = (
        "import sys; from pathlib import Path\n"
        "from openadapt_flow import verifier as v\n"
        "v.USED_DIR = Path(sys.argv[3])\n"
        "c = v.Contract.from_dict(__import__('json').loads(sys.argv[2]))\n"
        "try:\n"
        "    v.verify_write(c, v.Snapshot.load(sys.argv[1]),"
        " params={'fname': 'Ada', 'lname': 'Quill', 'email': 'ada@example.test'},"
        " actor=v.Actor(kind='agent', id='bot'))\n"
        "    print('verified')\n"
        "except v.VerifierError:\n"
        "    print('refused')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script, str(copy), json.dumps(_doc(db)), str(_used_dir)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "refused"


def test_the_snapshot_fields_are_stable() -> None:
    from dataclasses import fields

    assert [f.name for f in fields(Snapshot)] == ["payload", "used", "path"]
    assert list(inspect.signature(Snapshot.load).parameters) == ["path"]
    assert list(inspect.signature(Snapshot.save).parameters) == ["self", "path"]


def test_a_snapshot_verifies_only_where_it_was_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review of #1588: a worker with another HOME (another state
    directory) must refuse, not replay."""

    db = _store(tmp_path)
    contract = _contract(db)
    before = capture(contract, PARAMS)
    _write(db, SAVE)
    monkeypatch.setattr(verifier, "USED_DIR", tmp_path / "elsewhere")
    with pytest.raises(VerifierError, match="captured under this state"):
        verify_write(contract, before, params=PARAMS, actor=BOT)
