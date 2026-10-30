"""Review round 1 of null values (#1663), adapted from the reviewer's attacks.

- A missing REST/FHIR field is not null unless the contract declares it per
  field (``absent_is_null: true``). The record check and the frame read the
  same rule, so a correct write whose API omits the null field confirms only
  when declared, and a misspelled or nested key never passes for null.
- The ledger keeps ``None`` and ``""`` apart: a null write is not handed the
  result of an earlier ``""`` write.
- A screen read can't show a null, so ``begin_write`` refuses one there.

Real HTTP servers (REST, FHIR) and SQLite. Records are made up.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.verifier import (
    Actor,
    AlreadyWritten,
    Contract,
    MemoryLedger,
    Outcome,
    VerifierError,
    begin_write,
    verify_write,
)

BOT = Actor(kind="agent", id="bot")


@pytest.fixture(autouse=True)
def _dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


STATE: dict[str, Any] = {"issues": []}


class _Issues(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"issues": STATE["issues"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture()
def rest_url() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Issues)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _rest(url: str, match: dict[str, Any], frame: bool) -> Contract:
    doc: dict[str, Any] = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "rest",
            "base_url": url,
            "records_path": "/issues.json",
            "records_key": "issues",
        },
        "effects": [
            {
                "kind": "record_written",
                "match": match,
                "expected_count": 1,
                "count_new_only": True,
            }
        ],
    }
    if frame:
        doc["frame"] = {"identity_field": "id"}
    return Contract.from_dict(doc)


# label: (match key, declared absent_is_null, the new record, CONFIRMED?)
CASES = {
    "omitted, declared": ("description", True, {"id": 2, "subject": "Tax"}, True),
    "omitted, not declared": ("description", False, {"id": 2, "subject": "Tax"}, False),
    "explicit null": (
        "description",
        False,
        {"id": 2, "subject": "Tax", "description": None},
        True,
    ),
    "a value, declared": (
        "description",
        True,
        {"id": 2, "subject": "Tax", "description": "Not asked"},
        False,
    ),
    "nested key": (
        "assigned_to.id",
        False,
        {"id": 2, "subject": "Tax", "assigned_to": {"id": 7}},
        False,
    ),
    "misspelled key": (
        "descripton",
        False,
        {"id": 2, "subject": "Tax", "description": "Not asked"},
        False,
    ),
}


@pytest.mark.parametrize("frame", [True, False])
@pytest.mark.parametrize("label", sorted(CASES))
def test_rest_absent_field_rule_is_the_same_in_judge_and_frame(
    rest_url: str, frame: bool, label: str
) -> None:
    key, declared, new, confirmed = CASES[label]
    # An existing issue carries the field, as on a real tracker; a declared
    # field no record carries stops (see the round 2 tests below).
    STATE["issues"] = [{"id": 1, "subject": "Old", "description": "x"}]
    value: dict[str, Any] = {"param": "v"}
    if declared:
        value["absent_is_null"] = True
    contract = _rest(rest_url, {"subject": {"param": "subject"}, key: value}, frame)
    pending = begin_write(
        contract, {"subject": "Tax", "v": None}, actor=BOT, ledger=MemoryLedger()
    )
    STATE["issues"].append(new)
    result = verify_write(pending)
    assert (result.outcome is Outcome.CONFIRMED) is confirmed, result.reasons


FHIR: dict[str, Any] = {"res": []}


class _Bundle(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(
            {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(FHIR["res"]),
                "entry": [{"resource": r} for r in FHIR["res"]],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/fhir+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.mark.parametrize(
    ("written", "confirmed"), [(None, True), ("mallory@x.test", False)]
)
def test_fhir_null_on_a_path_that_resolves(written: Any, confirmed: bool) -> None:
    server = HTTPServer(("127.0.0.1", 0), _Bundle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/fhir"
        FHIR["res"] = [
            {"resourceType": "Patient", "id": "1", "name": [{"family": "Old"}]}
        ]
        contract = Contract.from_dict(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": {
                    "kind": "fhir",
                    "base_url": url,
                    "resource_type": "Patient",
                    "field_paths": {
                        "id": "id",
                        "family": "name.family",
                        "email": "telecom[system=email].value",
                    },
                },
                "effects": [
                    {
                        "kind": "record_written",
                        "match": {
                            "family": {"param": "family"},
                            "email": {"param": "email"},
                        },
                        "expected_count": 1,
                        "count_new_only": True,
                    }
                ],
                "frame": {"identity_field": "id"},
            }
        )
        pending = begin_write(
            contract,
            {"family": "Quill", "email": None},
            actor=BOT,
            ledger=MemoryLedger(),
        )
        new: dict[str, Any] = {
            "resourceType": "Patient",
            "id": "2",
            "name": [{"family": "Quill"}],
        }
        if written:
            new["telecom"] = [{"system": "email", "value": written}]
        FHIR["res"].append(new)
        result = verify_write(pending)
        assert (result.outcome is Outcome.CONFIRMED) is confirmed, result.reasons
    finally:
        server.shutdown()


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE issue (id INTEGER PRIMARY KEY, subject TEXT, description TEXT);"
        "INSERT INTO issue VALUES (1,'Old','x');"
    )
    con.commit()
    con.close()
    return db


def _sql(db: Path) -> Contract:
    return Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(db),
                "sql_query": "SELECT id, subject, description FROM issue",
            },
            "effects": [
                {
                    "kind": "record_written",
                    "match": {
                        "subject": {"param": "subject"},
                        "description": {"param": "description"},
                    },
                    "expected_count": 1,
                    "count_new_only": True,
                }
            ],
            "frame": {"identity_field": "id"},
        }
    )


def _insert(db: Path, subject: str, description: Any) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO issue (subject, description) VALUES (?, ?)", (subject, description)
    )
    con.commit()
    con.close()


def test_the_ledger_keeps_a_null_write_apart_from_an_empty_one(tmp_path: Path) -> None:
    db = _db(tmp_path)
    contract = _sql(db)
    ledger = MemoryLedger()
    first = begin_write(
        contract, {"subject": "Tax", "description": ""}, actor=BOT, ledger=ledger
    )
    _insert(db, "Tax", "")
    assert verify_write(first).outcome is Outcome.CONFIRMED
    # The "" write shares the null write's old key. It is never handed back
    # as the null write's result; a recent one holds the null write for a
    # person instead (a stop, not a duplicate).
    from openadapt_flow.verifier import WriteHeld

    try:
        second = begin_write(
            contract, {"subject": "Tax", "description": None}, actor=BOT, ledger=ledger
        )
    except AlreadyWritten as exc:  # the old key: handed the "" result
        pytest.fail(f"a null write was handed the empty write's result: {exc}")
    except WriteHeld:
        return
    _insert(db, "Tax", None)
    assert verify_write(second).outcome is Outcome.CONFIRMED


def test_a_null_write_waits_for_an_unresolved_attempt_keyed_the_old_way(
    tmp_path: Path,
) -> None:
    # A "" attempt still open shares the old key of a null write: hold it.
    db = _db(tmp_path)
    contract = _sql(db)
    ledger = MemoryLedger()
    begin_write(
        contract, {"subject": "Tax", "description": ""}, actor=BOT, ledger=ledger
    )
    from openadapt_flow.verifier import WriteHeld

    with pytest.raises(WriteHeld):
        begin_write(
            contract, {"subject": "Tax", "description": None}, actor=BOT, ledger=ledger
        )


@pytest.mark.parametrize("kind", ["ui_readback", "desktop-readback"])
def test_a_screen_read_refuses_a_null_at_begin_write(kind: str) -> None:
    contract = Contract.__new__(Contract)
    contract.config = _sql(Path("unused.sqlite")).config.model_copy(deep=True)
    contract.config.verifier = contract.config.verifier.model_copy(
        update={"kind": kind}
    )
    contract.name, contract.writes = "screen", ()
    with pytest.raises(VerifierError, match="screen"):
        begin_write(
            contract,
            {"subject": "Tax", "description": None},
            actor=BOT,
            ledger=MemoryLedger(),
        )


# -- review round 2 -----------------------------------------------------------


def _legacy_uncertain(tmp_path: Path) -> tuple[Path, Contract, Any, str]:
    """An attempt under the old key (None as ""), left UNCERTAIN: exactly what
    a null write recorded before the upgrade left in the ledger."""
    from openadapt_flow.verifier import Ledger
    from openadapt_flow.write_ledger import FileLedger

    db = _db(tmp_path)
    contract = _sql(db)
    ledger = Ledger(FileLedger(tmp_path / "led"))
    pending = begin_write(
        contract, {"subject": "Tax", "description": ""}, actor=BOT, ledger=ledger
    )
    assert verify_write(pending).outcome is Outcome.UNCERTAIN  # nothing landed
    legacy = ledger.key_for(
        contract.identity,
        {"subject": "Tax", "description": None},
        None,
        legacy_null=True,
    )
    return db, contract, ledger, legacy


def test_a_legacy_attempt_released_as_landed_is_handed_back(tmp_path: Path) -> None:
    from openadapt_flow.verifier import WriteHeld

    db, contract, ledger, legacy = _legacy_uncertain(tmp_path)
    _insert(db, "Tax", None)  # before the upgrade, the null write landed
    params = {"subject": "Tax", "description": None}
    with pytest.raises(WriteHeld, match="--found landed"):
        begin_write(contract, params, actor=BOT, ledger=ledger)
    ledger.release(legacy, by="ops", reason="row 2 is there", found="landed")
    with pytest.raises(AlreadyWritten) as caught:
        begin_write(contract, params, actor=BOT, ledger=ledger)
    assert caught.value.result.get("not_sent_again") is True


def test_a_legacy_attempt_released_as_absent_lets_the_write_go(tmp_path: Path) -> None:
    db, contract, ledger, legacy = _legacy_uncertain(tmp_path)
    ledger.release(legacy, by="ops", reason="not there", found="absent")
    pending = begin_write(
        contract, {"subject": "Tax", "description": None}, actor=BOT, ledger=ledger
    )
    _insert(db, "Tax", None)
    assert verify_write(pending).outcome is Outcome.CONFIRMED


def _declared(kind: str, key: str, url: str = "http://127.0.0.1:1") -> dict[str, Any]:
    verifier: dict[str, Any] = (
        {"kind": "rest", "base_url": url, "records_path": "/i", "records_key": "i"}
        if kind == "rest"
        else {
            "kind": "sql",
            "sqlite_database": "unused.sqlite",
            "sql_query": "SELECT id, subject, description FROM issue",
        }
    )
    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": verifier,
        "effects": [
            {
                "kind": "record_written",
                "match": {
                    "subject": {"param": "subject"},
                    key: {"param": "v", "absent_is_null": True},
                },
                "expected_count": 1,
                "count_new_only": True,
            }
        ],
    }


@pytest.mark.parametrize(
    ("kind", "key", "why"),
    [
        ("rest", "assigned_to.id", "dotted key"),
        ("sql", "descripton", "rest or graphql"),
        ("sql", "description", "rest or graphql"),
    ],
)
def test_absent_is_null_is_refused_where_a_field_is_never_left_out(
    kind: str, key: str, why: str
) -> None:
    with pytest.raises(VerifierError, match=why):
        Contract.from_dict(_declared(kind, key))


def test_a_declared_field_no_record_carries_stops(rest_url: str) -> None:
    # A misspelled top-level key: no record carries "descripton".
    STATE["issues"] = [{"id": 1, "subject": "Old", "description": "x"}]
    contract = Contract.from_dict(_declared("rest", "descripton", rest_url))
    contract.config.verifier.records_path = "/issues.json"
    contract.config.verifier.records_key = "issues"
    pending = begin_write(
        contract, {"subject": "Tax", "v": None}, actor=BOT, ledger=MemoryLedger()
    )
    STATE["issues"].append({"id": 2, "subject": "Tax", "description": "Not asked"})
    result = verify_write(pending)
    assert result.outcome is Outcome.UNCERTAIN, result.reasons
    # The judge's reason, which the reply summarizes as "indeterminate".
    from openadapt_flow.runtime.effects import Effect, EffectState
    from openadapt_flow.runtime.effects._common import judge_records

    effect = Effect(
        kind="record_written",
        match={
            "subject": {"param": "subject"},
            "descripton": {"param": "v", "absent_is_null": True},
        },
        expected_count=1,
        count_new_only=True,
    ).resolve({"subject": "Tax", "v": None})
    before = EffectState(substrate="rest", reachable=True, records=STATE["issues"][:1])
    verdict = judge_records(effect, before, STATE["issues"], substrate="rest")
    assert verdict.verdict.value == "indeterminate"
    assert "no record of the read carries" in verdict.reason


def test_a_declaration_does_not_spill_onto_another_null(rest_url: str) -> None:
    STATE["issues"] = [
        {"id": 1, "subject": "Old", "assigned_to": {"id": 3}, "description": "x"}
    ]
    contract = _rest(
        rest_url,
        {
            "subject": {"param": "subject"},
            "assigned_to": {"param": "a", "absent_is_null": True},
            "description": {"param": "d"},
        },
        True,
    )
    pending = begin_write(
        contract,
        {"subject": "Tax", "a": None, "d": None},
        actor=BOT,
        ledger=MemoryLedger(),
    )
    STATE["issues"].append({"id": 2, "subject": "Tax"})
    assert verify_write(pending).outcome is not Outcome.CONFIRMED


def test_a_declared_field_with_a_value_needs_the_value(rest_url: str) -> None:
    STATE["issues"] = [{"id": 1, "subject": "Old", "description": "x"}]
    contract = _rest(
        rest_url,
        {
            "subject": {"param": "subject"},
            "description": {"param": "d", "absent_is_null": True},
        },
        True,
    )
    pending = begin_write(
        contract, {"subject": "Tax", "d": "Asked"}, actor=BOT, ledger=MemoryLedger()
    )
    STATE["issues"].append({"id": 2, "subject": "Tax"})
    assert verify_write(pending).outcome is not Outcome.CONFIRMED
