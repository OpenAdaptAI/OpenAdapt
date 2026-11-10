"""The one verifier API: begin_write, the write, verify_write.

These tests pin its public signatures and outcome vocabulary, and check it
end to end on a SQLite store. Records are made up.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow import verifier as verifier_api
from openadapt_flow.verifier import (
    Actor,
    BaselineUnreadable,
    Contract,
    MemoryLedger,
    Outcome,
    RecheckResult,
    RecheckState,
    VerifierError,
    WriteResult,
    begin_write,
    capture_database,
    classify,
    diff_database,
    recheck,
    verify_write,
)

BOT = Actor(kind="agent", id="bot")


@pytest.fixture(autouse=True)
def _ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


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
    pending = begin_write(_contract(db), PARAMS, actor=BOT, ledger=MemoryLedger())
    _write(db, *statements)
    return verify_write(pending)


def test_a_correct_write_is_confirmed(tmp_path: Path) -> None:
    result = _check(tmp_path, SAVE)
    assert result.outcome is Outcome.CONFIRMED and result.verified
    assert result.receipt_outcome == "verified" and result.verdict == "VERIFIED"
    assert len(result.receipt_sha256) == 64
    assert result.checked["delta"] == "held" and result.checked["frame"] == "held"
    assert result.reasons == [] and result.observed == "present"


def test_a_result_says_what_it_establishes(tmp_path: Path) -> None:
    scope = _check(tmp_path, SAVE).scope
    assert scope.read_paths == ("database",) and scope.stores == ("sql",)
    assert scope.unread == ()
    assert scope.strength == "independent_system_of_record"
    assert scope.window.baseline_read_at and scope.window.read_at
    assert scope.window.max_execution_window_s == 3600
    joined = " ".join(scope.not_covered)
    for gap in ("after read_at", "email", "other services", "authorized"):
        assert gap in joined
    # No frame.database: what's outside the read set isn't covered either.
    assert "outside the read set" in joined


def test_a_wrong_value_is_not_confirmed(tmp_path: Path) -> None:
    wrong = SAVE.replace("'Ada', 'Quill'", "'Eda', 'Quill'")
    result = _check(tmp_path, wrong)
    assert result.outcome is not Outcome.CONFIRMED
    assert result.checked["delta"] != "held" and result.reasons


def test_a_stray_write_is_refuted(tmp_path: Path) -> None:
    result = _check(tmp_path, SAVE, "UPDATE patient SET phone = '2' WHERE id = 1")
    assert result.outcome is Outcome.REFUTED and not result.retry_safe
    assert result.checked["frame"] == "refuted"
    assert any("outside the declared write" in r for r in result.reasons)


def test_a_duplicate_write_is_refuted(tmp_path: Path) -> None:
    assert _check(tmp_path, SAVE, SAVE).outcome is Outcome.REFUTED


def test_no_write_is_uncertain(tmp_path: Path) -> None:
    result = _check(tmp_path)
    # The store doesn't show the write: it may still land, so reconcile.
    assert result.outcome is Outcome.UNCERTAIN and not result.retry_safe
    assert result.verdict == "RECONCILIATION_REQUIRED"


@pytest.mark.parametrize("file_backend", [False, True])
@pytest.mark.parametrize("write_attempted", [False, True])
def test_absence_without_dispatch_proof_stays_held(
    tmp_path: Path, file_backend: bool, write_attempted: bool
) -> None:
    db = _store(tmp_path)
    backend = (
        verifier_api.FileLedger(tmp_path / "journal")
        if file_backend
        else MemoryLedger()
    )
    ledger = verifier_api.Ledger(backend, settle_s=0)
    contract = _contract(db)
    pending = begin_write(contract, PARAMS, actor=BOT, ledger=ledger)
    result = verify_write(pending, write_attempted=write_attempted)
    expected = Outcome.UNCERTAIN if write_attempted else Outcome.REFUTED
    assert result.outcome is expected and not result.retry_safe
    assert result.receipt_outcome == (
        "reconciliation_required" if write_attempted else "halted_before_effect"
    )
    assert result.to_dict()["retry_safe"] is False
    assert not result.verified
    retained = ledger.latest(pending.write_key)
    assert retained is not None and retained.resolved is not None
    assert retained.resolved["retry_safe"] is False
    assert retained.resolved["result"]["retry_safe"] is False
    if file_backend:
        ledger = verifier_api.Ledger(
            verifier_api.FileLedger(tmp_path / "journal"), settle_s=0
        )
    with pytest.raises(verifier_api.WriteHeld):
        begin_write(contract, PARAMS, actor=BOT, ledger=ledger)
    assert len(ledger.attempts()) == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM patient").fetchone() == (1,)


def test_an_unreadable_store_stops_before_the_write(tmp_path: Path) -> None:
    contract = _contract(tmp_path / "missing" / "app.sqlite")
    with pytest.raises(BaselineUnreadable):
        begin_write(contract, PARAMS, actor=BOT, ledger=MemoryLedger())


def test_an_unknown_actor_kind_is_refused_before_reading(tmp_path: Path) -> None:
    with pytest.raises(VerifierError):
        begin_write(
            _contract(_store(tmp_path)), PARAMS, actor=Actor("robot", "x"), ledger=None
        )


def test_a_missing_value_is_refused(tmp_path: Path) -> None:
    with pytest.raises(VerifierError, match="values missing"):
        begin_write(_contract(_store(tmp_path)), {"fname": "Ada"}, actor=BOT)


def test_a_pending_write_is_checked_once(tmp_path: Path) -> None:
    db = _store(tmp_path)
    pending = begin_write(_contract(db), PARAMS, actor=BOT, ledger=MemoryLedger())
    _write(db, SAVE)
    assert verify_write(pending).verified
    with pytest.raises(VerifierError):
        verify_write(pending)


def test_without_a_ledger_the_folder_is_the_workdir(tmp_path: Path) -> None:
    db = _store(tmp_path)
    pending = begin_write(
        _contract(db), PARAMS, actor=BOT, ledger=None, workdir=tmp_path / "w"
    )
    assert pending.workdir.parent == tmp_path / "w"
    assert pending.workdir.stat().st_mode & 0o777 == 0o700
    _write(db, SAVE)
    result = verify_write(pending)
    assert result.verified and result.write_key == ""
    assert Path(str(result.receipt_path)).parent == pending.workdir


def test_the_database_diff_names_what_moved_without_values(tmp_path: Path) -> None:
    db = _store(tmp_path)
    read = {"sqlite_database": str(db)}
    before = capture_database(read, "sqlite")
    _write(db, SAVE, "UPDATE patient SET phone = '2' WHERE id = 1")
    moved = diff_database(before, capture_database(read, "sqlite"))
    assert set(moved) == {"patient"}
    assert (moved["patient"].added, moved["patient"].changed) == (1, 1)
    assert moved["patient"].changed_columns == ["phone"]
    _write(db, "DELETE FROM patient WHERE id = 2", "UPDATE patient SET phone = '1'")
    assert diff_database(before, capture_database(read, "sqlite")) == {}


def test_a_bad_contract_raises(tmp_path: Path) -> None:
    with pytest.raises(VerifierError):
        Contract.from_dict({"schema_version": "openadapt.verify/v1"})


def test_the_contract_names_the_values_it_binds(tmp_path: Path) -> None:
    assert _contract(_store(tmp_path)).params == ["email", "fname", "lname"]


@pytest.mark.parametrize(
    "receipt_outcome, checked, rows, expected",
    [
        ("verified", {}, [], (Outcome.CONFIRMED, False)),
        ("halted_before_effect", {}, [], (Outcome.REFUTED, False)),
        ("rejected_policy", {}, [], (Outcome.REFUTED, False)),
        ("failed_platform", {}, [], (Outcome.UNCERTAIN, False)),
        ("reconciliation_required", {"frame": "refuted"}, [], (Outcome.REFUTED, False)),
        (
            "reconciliation_required",
            {"source": "refuted"},
            [],
            (Outcome.REFUTED, False),
        ),
        (
            "reconciliation_required",
            {"delta": "not_checked"},
            [],
            (Outcome.UNCERTAIN, False),
        ),
        (
            "reconciliation_required",
            {"delta": "refuted"},
            [{"verdict": "REFUTED", "observed_effect": "absent"}],
            (Outcome.UNCERTAIN, False),
        ),
        (
            "reconciliation_required",
            {"delta": "refuted"},
            [{"verdict": "REFUTED", "observed_effect": "present"}],
            (Outcome.REFUTED, False),
        ),
    ],
)
def test_every_receipt_outcome_maps_to_one_of_three(
    receipt_outcome: str, checked: dict, rows: list, expected: tuple
) -> None:
    evidence = {"readback": {"effects": rows}}
    assert classify(receipt_outcome, checked, evidence) == expected


def test_the_public_api_is_stable() -> None:
    assert sorted(verifier_api.__all__) == [
        "Actor",
        "AlreadyWritten",
        "BaselineUnreadable",
        "Contract",
        "DatabaseSnapshot",
        "FileLedger",
        "Ledger",
        "LedgerBackend",
        "LedgerError",
        "MemoryLedger",
        "Outcome",
        "PendingWrite",
        "RecheckNotDue",
        "RecheckResult",
        "RecheckState",
        "Scope",
        "Snapshot",
        "Source",
        "TableDiff",
        "VerifierError",
        "Window",
        "WriteBlocked",
        "WriteHeld",
        "WriteResult",
        "begin_write",
        "capture",
        "capture_database",
        "diff_database",
        "issue_recheck_receipt",
        "issue_write_receipt",
        "recheck",
        "reconcile",
        "verified_write",
        "verify_write",
    ]
    assert [o.value for o in Outcome] == ["CONFIRMED", "REFUTED", "UNCERTAIN"]

    def params(fn: Any) -> list[tuple[str, str]]:
        return [
            (name, p.kind.name) for name, p in inspect.signature(fn).parameters.items()
        ]

    assert params(begin_write) == [
        ("contract", "POSITIONAL_OR_KEYWORD"),
        ("params", "POSITIONAL_OR_KEYWORD"),
        ("actor", "KEYWORD_ONLY"),
        ("ledger", "KEYWORD_ONLY"),
        ("write_key", "KEYWORD_ONLY"),
        ("authorization_ref", "KEYWORD_ONLY"),
        ("workdir", "KEYWORD_ONLY"),
        ("declared_by", "KEYWORD_ONLY"),
        ("read_provider", "KEYWORD_ONLY"),
    ]
    assert params(verify_write) == [
        ("pending", "POSITIONAL_OR_KEYWORD"),
        # before, params, actor and authorization_ref: the deprecated
        # (contract, before) form; test_verifier_snapshot_api.py pins it.
        ("before", "POSITIONAL_OR_KEYWORD"),
        ("write_attempted", "KEYWORD_ONLY"),
        ("source", "KEYWORD_ONLY"),
        ("seal", "KEYWORD_ONLY"),
        ("delivered", "KEYWORD_ONLY"),
        ("params", "KEYWORD_ONLY"),
        ("actor", "KEYWORD_ONLY"),
        ("authorization_ref", "KEYWORD_ONLY"),
        ("write_receipt", "KEYWORD_ONLY"),
    ]
    assert [n for n in inspect.signature(capture_database).parameters] == [
        "read",
        "dialect",
        "allow_tables",
        "max_rows",
        "read_row_budget",
        "key",
        "profile",
        "before",
        "actor_names",
    ]
    budget = inspect.signature(capture_database).parameters["read_row_budget"]
    assert budget.kind is inspect.Parameter.KEYWORD_ONLY
    assert budget.default == 1_000_000
    assert list(WriteResult.__dataclass_fields__)[:5] == [
        "outcome",
        "scope",
        "checked",
        "reasons",
        "retry_safe",
    ]
    assert list(WriteResult.__dataclass_fields__)[-4:] == [
        "recheck",
        "recheck_due_at",
        "write_receipt",
        "write_receipt_path",
    ]
    assert params(recheck) == [
        ("contract", "POSITIONAL_OR_KEYWORD"),
        ("write", "POSITIONAL_OR_KEYWORD"),
        ("params", "KEYWORD_ONLY"),
        ("actor", "KEYWORD_ONLY"),
        ("excused_by", "KEYWORD_ONLY"),
        ("wait", "KEYWORD_ONLY"),
        ("ledger", "KEYWORD_ONLY"),
        ("read_provider", "KEYWORD_ONLY"),
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
        "recheck_claim_sha256",
        "excuse_receipt_sha256s",
        "recheck_owed",
    ]
    assert RecheckResult.__dataclass_fields__["recheck_owed"].default is False
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
    assert diff_database(before, after, **args) == {}
    _write(db, "UPDATE users_secure SET password = 'x' WHERE id = 1")
    after = capture_database(read, "sqlite", before=before, **args)
    assert set(diff_database(before, after, **args)) == {"users_secure"}


def test_a_pending_write_repr_shows_no_values(tmp_path: Path) -> None:
    pending = begin_write(
        _contract(_store(tmp_path)), PARAMS, actor=BOT, ledger=MemoryLedger()
    )
    text = repr(pending)
    assert "Ada" not in text and "example.test" not in text
    pending.release()


#: Contract hashes as openadapt-flow 2.5.0 computed them. A field that lost
#: its default (records_path, resource_type) must not move them: the write
#: ledger and reward certificates are keyed on these.
_PINNED_CONTRACT_HASHES = {
    "fhir": {
        "digest": "91c4a3ebe007fb29ff109ab420e004c69d18344776d91b833275cafa0764fd20",
        "identity": "5035aab8eb04079931ced609318a886d463b270031edb967650e3966206919e3",
        "store": "20d025f2007789ef2271b5cef933e38fd47d4114d559fc820aca9283d6d827fd",
    },
    "rest": {
        "digest": "5ab1d6ebc5bae6c5fba13a3f2ce60e2b65292c2126e6f858ef95190ee4b1104f",
        "identity": "f287cf9ea6af74fd36dba0e42ae64a3024518c4c45ac7e063a1b4fee7dd2e678",
        "store": "32521d4b64cce55ed24443f4d35ec1e73f472cd88ebc4d57e6a92d06161611a7",
    },
    "sql": {
        "digest": "45ea6035db9bf63b39e7bf31db07dab820e42a9a218ba39906ea54e283cb5b22",
        "identity": "60cf99eaa0beed0335bff2e85e23ac1a29b90882075d728eb8952a71305b52a2",
        "store": "f333364923dd35c0e086046de0494ee84df7c766bc5c9a53209e58cad4594a9c",
    },
}


def test_contract_hashes_do_not_change() -> None:
    record = {
        "name": "patient",
        "match": ["email", "lname"],
        "fields": {"fname": "fname", "lname": "lname", "email": "email"},
    }
    verifiers = {
        "sql": {
            "kind": "sql",
            "sqlite_database": "/data/app.db",
            "sql_query": "SELECT * FROM patient",
        },
        "rest": {
            "kind": "rest",
            "base_url": "http://app.local",
            "records_path": "/api/db",
            "records_key": "records",
        },
        "fhir": {
            "kind": "fhir",
            "base_url": "https://fhir.local/r4",
            "resource_type": "Observation",
            "field_paths": {
                "id": "id",
                "email": "email",
                "lname": "lname",
                "fname": "fname",
            },
        },
    }
    for kind, verifier_config in verifiers.items():
        contract = Contract.from_dict(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": verifier_config,
                "record": record,
                "frame": {"identity_field": "id"},
            }
        )
        got = {
            "digest": contract.digest,
            "store": contract.store,
            "identity": contract.identity,
        }
        assert got == _PINNED_CONTRACT_HASHES[kind], kind


def test_legacy_handed_back_result_does_not_advertise_retry(tmp_path: Path) -> None:
    db = _store(tmp_path)
    ledger = verifier_api.Ledger(verifier_api.FileLedger(tmp_path / "journal"))
    contract = _contract(db)
    pending = begin_write(contract, PARAMS, actor=BOT, ledger=ledger, write_key="one")
    _write(db, SAVE)
    result = verify_write(pending)
    assert result.verified
    retained = ledger.latest(pending.write_key)
    assert retained is not None and retained.resolved is not None
    fields = dict(retained.resolved)
    for key in ("event", "attempt", "at", "t", "revision", "based_on", "applied"):
        fields.pop(key, None)
    fields["result"] = {**fields["result"], "retry_safe": True}
    ledger.resolved(
        pending.write_key,
        pending.write_id,
        expected_revision=retained.revision,
        **fields,
    )
    reopened = verifier_api.Ledger(verifier_api.FileLedger(tmp_path / "journal"))
    with pytest.raises(verifier_api.AlreadyWritten) as caught:
        begin_write(contract, PARAMS, actor=BOT, ledger=reopened, write_key="one")
    assert caught.value.result["retry_safe"] is False
    assert caught.value.result["verified"] is True
    assert caught.value.result["not_sent_again"] is True
    assert reopened.latest(pending.write_key).resolved["result"]["retry_safe"] is True
