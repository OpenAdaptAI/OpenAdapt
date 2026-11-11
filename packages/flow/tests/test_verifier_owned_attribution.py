"""Owned write and observation contracts on a controlled SQLite store."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from openadapt_flow.verifier import (
    Actor,
    Contract,
    Outcome,
    PendingWrite,
    VerifierError,
    WriteHeld,
    WriteResult,
    begin_write,
    reconcile,
    verify_write,
)
from openadapt_flow.verify_run import (
    VerifyRunError,
    capture_baseline,
    commit_owned_sqlite_insert,
    verify_run,
)
from openadapt_flow.write_ledger import FileLedger, Ledger

ACTOR = Actor(kind="agent", id="synthetic-actuator")
AUTHORIZATION = "synthetic-operator-2028"
VALUES = {"id": 2, "record_key": "synthetic-target", "label": "desired-value"}
PROTECTED = "prior-value"
INSERT = "INSERT INTO records VALUES (:id, :record_key, :label)"
WITNESS_TABLE = "openadapt_owned_insert_events"


@dataclass(frozen=True)
class StoreFrame:
    target_count: int
    protected_label: str


@dataclass(frozen=True)
class Store:
    database: Path
    ledger: Ledger

    def contract(self, *, native_writer: bool = True) -> Contract:
        return Contract.from_dict(
            {
                "schema_version": "openadapt.verify/v1",
                "verifier": {
                    "kind": "sql",
                    "sqlite_database": str(self.database),
                    "sql_query": "SELECT * FROM records",
                },
                "effects": [
                    {
                        "kind": "record_written",
                        "match": {key: {"param": key} for key in VALUES},
                        "expected_count": 1,
                        "count_new_only": True,
                        "timeout_s": 0,
                    }
                ],
                "frame": {"identity_field": "id"},
                **(
                    {
                        "owned_sqlite_insert": {
                            "table": "records",
                            "identity_field": "id",
                        }
                    }
                    if native_writer
                    else {}
                ),
            }
        )

    def read_frame(self) -> StoreFrame:
        with closing(
            sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        ) as reader:
            desired = reader.execute(
                "SELECT COUNT(*) FROM records "
                "WHERE id = :id AND record_key = :record_key AND label = :label",
                VALUES,
            ).fetchone()[0]
            protected = reader.execute(
                "SELECT label FROM records WHERE id = 1"
            ).fetchone()[0]
        return StoreFrame(desired, protected)

    def event_attempts(self) -> tuple[str, ...]:
        with closing(
            sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        ) as reader:
            return tuple(
                row[0]
                for row in reader.execute(
                    f"SELECT attempt FROM {WITNESS_TABLE} ORDER BY attempt"
                )
            )

    def set_protected(self, label: str) -> None:
        with closing(sqlite3.connect(self.database)) as independent:
            independent.execute("UPDATE records SET label = ? WHERE id = 1", (label,))
            independent.commit()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    database = tmp_path / "store.sqlite"
    with closing(sqlite3.connect(database)) as initializer:
        initializer.executescript(
            "CREATE TABLE records "
            "(id INTEGER PRIMARY KEY, record_key TEXT NOT NULL, "
            "label TEXT NOT NULL) STRICT;"
            "INSERT INTO records VALUES (1, 'synthetic-protected', 'prior-value');"
            f"CREATE TABLE {WITNESS_TABLE} "
            "(attempt TEXT PRIMARY KEY NOT NULL, witness TEXT NOT NULL) STRICT;"
        )
    return Store(database, Ledger(FileLedger(tmp_path / "journal")))


def _external_commit(connection: sqlite3.Connection) -> int:
    target_changes = connection.execute(INSERT, VALUES).rowcount
    assert target_changes == 1
    connection.commit()
    return target_changes


def _noop(connection: sqlite3.Connection) -> int:
    assert connection.total_changes == 0
    return 0


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("entrypoint", ["pending", "direct"])
def test_external_commit_around_noop_is_observation_only(
    store: Store, tmp_path: Path, when: str, entrypoint: str
) -> None:
    contract = store.contract(native_writer=False)
    pending = None
    baseline = tmp_path / "owned.json"
    if entrypoint == "pending":
        pending = _begin(store, contract)
    else:
        capture_baseline(contract.config, VALUES, baseline, purpose="owned_attempt")
    before = store.read_frame()
    assert before == StoreFrame(0, PROTECTED)
    with (
        closing(sqlite3.connect(store.database)) as independent,
        closing(sqlite3.connect(store.database)) as actuator,
    ):
        external_changes = _external_commit(independent) if when == "before" else 0
        before_dispatch = store.read_frame()
        actuator_changes = _noop(actuator)
        if when == "after":
            external_changes = _external_commit(independent)
        after_dispatch = store.read_frame()

    premise = (
        before.target_count,
        external_changes,
        actuator_changes,
        after_dispatch.target_count,
    )
    assert premise == (0, 1, 0, 1)
    assert before_dispatch.target_count == int(when == "before")
    assert before_dispatch.protected_label == PROTECTED
    assert after_dispatch.protected_label == PROTECTED
    if pending is not None:
        _assert_no_owned_credit(store, pending, verify_write(pending, seal=False))
    else:
        result = verify_run(
            contract.config,
            params=VALUES,
            run_record=ACTOR.run_record(),
            authorization_ref=AUTHORIZATION,
            baseline_path=baseline,
            write_attempted=True,
        )
        _assert_unsupported_owned_observation(result)


def _assert_unsupported_owned_observation(result: dict) -> None:
    receipt = result["receipt"]
    assert receipt["write_attribution"] == {
        "purpose": "owned_attempt",
        "status": "unattributed",
    }
    assert result["evidence"]["write_attribution"] == receipt["write_attribution"]
    assert receipt["outcome"] == "reconciliation_required"
    assert receipt["effect_verdict"] == "INDETERMINATE"
    assert receipt["delivery_uncertain"] is True
    assert receipt["contracts"]["effect_passed"] is False
    (effect,) = result["evidence"]["readback"]["effects"]
    assert effect["verdict"] == "CONFIRMED" and effect["observed_effect"] == "present"


@pytest.mark.parametrize("entrypoint", ["pending", "direct"])
def test_legacy_observation_keeps_its_desired_state_verdict(
    store: Store, tmp_path: Path, entrypoint: str
) -> None:
    contract = store.contract(native_writer=False)
    pending = None
    baseline = tmp_path / "observe.json"
    if entrypoint == "pending":
        pending = begin_write(
            contract,
            VALUES,
            actor=ACTOR,
            ledger=store.ledger,
            write_key="legacy-observation-attempt",
            authorization_ref=AUTHORIZATION,
        )
    else:
        capture_baseline(contract.config, VALUES, baseline)
    with closing(sqlite3.connect(store.database)) as independent:
        _external_commit(independent)
    assert store.read_frame() == StoreFrame(1, PROTECTED)

    if pending is not None:
        result = verify_write(pending, seal=False)
        assert result.outcome is Outcome.CONFIRMED
        receipt, evidence = result.receipt, result.evidence
    else:
        document = verify_run(
            contract.config,
            params=VALUES,
            run_record=ACTOR.run_record(),
            authorization_ref=AUTHORIZATION,
            baseline_path=baseline,
        )
        receipt, evidence = document["receipt"], document["evidence"]
    assert receipt["write_attribution"] == {
        "purpose": "observation",
        "status": "observed",
    }
    assert evidence["write_attribution"] == receipt["write_attribution"]
    assert receipt["outcome"] == "verified" and receipt["effect_verdict"] == "CONFIRMED"
    assert receipt["contracts"]["effect_passed"] is True


@pytest.mark.parametrize(
    ("captured", "requested"),
    [("owned_attempt", "observation"), ("observation", "owned_attempt")],
)
def test_explicit_purpose_cannot_relabel_a_baseline(
    store: Store, tmp_path: Path, captured: str, requested: str
) -> None:
    cfg = store.contract(native_writer=False).config
    baseline = tmp_path / "captured.json"
    capture_baseline(cfg, VALUES, baseline, purpose=captured)
    with pytest.raises(VerifyRunError):
        verify_run(
            cfg,
            params=VALUES,
            run_record=ACTOR.run_record(),
            authorization_ref=AUTHORIZATION,
            baseline_path=baseline,
            purpose=requested,
        )
    assert store.read_frame() == StoreFrame(0, PROTECTED)


def _begin(store: Store, contract: Contract) -> PendingWrite:
    pending = begin_write(
        contract,
        VALUES,
        actor=ACTOR,
        ledger=store.ledger,
        write_key="synthetic-owned-attempt",
        authorization_ref=AUTHORIZATION,
        purpose="owned_attempt",
    )
    active = store.ledger.latest(pending.write_key)
    assert active is not None and active.attempt_id == pending.write_id
    assert active.state == "open" and len(store.ledger.attempts()) == 1
    assert active.begun["purpose"] == "owned_attempt"
    assert json.loads(pending.baseline_path.read_bytes())["purpose"] == "owned_attempt"
    return pending


def _assert_no_owned_credit(
    store: Store, pending: PendingWrite, result: WriteResult
) -> None:
    _assert_unsupported_owned_observation(
        {"receipt": result.receipt, "evidence": result.evidence}
    )
    assert result.outcome is Outcome.UNCERTAIN and not result.verified
    assert result.observed == "present" and not result.retry_safe and not result.landed
    active = store.ledger.latest(pending.write_key)
    assert active is not None and active.attempt_id == pending.write_id
    assert active.state == "uncertain" and active.resolved is not None
    assert active.resolved["outcome"] == "UNCERTAIN"
    assert active.resolved["retry_safe"] is False
    assert active.resolved["landed"] is False
    assert active.resolved["result"]["receipt_outcome"] != "verified"


def test_owned_native_commit_has_an_independent_witness(store: Store) -> None:
    contract = store.contract()
    pending = _begin(store, contract)
    assert store.read_frame() == StoreFrame(0, PROTECTED)
    assert store.event_attempts() == ()
    commit_owned_sqlite_insert(contract.config, VALUES, pending.baseline_path)
    assert store.read_frame() == StoreFrame(1, PROTECTED)
    assert len(store.event_attempts()) == 1

    result = verify_write(pending, seal=False)
    assert result.outcome is Outcome.CONFIRMED and result.verified
    assert result.receipt_outcome == "verified" and result.observed == "present"
    assert result.checked["delta"] == result.checked["frame"] == "held"
    assert result.authorization == "operator" and not result.retry_safe
    assert result.receipt["write_attribution"] == {
        "purpose": "owned_attempt",
        "status": "attributed",
    }
    assert result.evidence["write_attribution"] == result.receipt["write_attribution"]
    active = store.ledger.latest(pending.write_key)
    assert active is not None and active.attempt_id == pending.write_id
    assert active.resolved is not None and active.resolved["outcome"] == "CONFIRMED"
    assert len(store.ledger.attempts()) == 1


def test_same_actor_other_attempt_cannot_confirm_this_one(
    store: Store, tmp_path: Path
) -> None:
    contract = store.contract()
    pending = _begin(store, contract)
    other = tmp_path / "other-owned.json"
    other_baseline = capture_baseline(
        contract.config, VALUES, other, purpose="owned_attempt"
    )
    commit_owned_sqlite_insert(contract.config, VALUES, other)
    assert store.read_frame() == StoreFrame(1, PROTECTED)
    assert store.event_attempts() == (other_baseline["execution_id"],)
    other_result = verify_run(
        contract.config,
        params=VALUES,
        run_record=ACTOR.run_record(),
        authorization_ref=AUTHORIZATION,
        baseline_path=other,
        write_attempted=True,
    )
    assert other_result["receipt"]["outcome"] == "verified"
    assert other_result["receipt"]["write_attribution"] == {
        "purpose": "owned_attempt",
        "status": "attributed",
    }
    assert len(store.ledger.attempts()) == 1

    _assert_no_owned_credit(store, pending, verify_write(pending, seal=False))


def _disturb_witness(store: Store, fault: str) -> None:
    with closing(sqlite3.connect(store.database)) as independent:
        if fault == "missing":
            independent.execute(f"DELETE FROM {WITNESS_TABLE}")
        elif fault == "mismatched":
            independent.execute(
                f"UPDATE {WITNESS_TABLE} SET witness = 'synthetic-invalid-witness'"
            )
        elif fault == "duplicate":
            independent.executescript(
                f"ALTER TABLE {WITNESS_TABLE} RENAME TO saved_event;"
                f"CREATE TABLE {WITNESS_TABLE} "
                "(attempt TEXT NOT NULL, witness TEXT NOT NULL) STRICT;"
                f"INSERT INTO {WITNESS_TABLE} SELECT * FROM saved_event;"
                f"INSERT INTO {WITNESS_TABLE} SELECT * FROM saved_event;"
                "DROP TABLE saved_event;"
            )
        else:
            assert fault == "unavailable"
            independent.execute(f"DROP TABLE {WITNESS_TABLE}")
        independent.commit()


@pytest.mark.parametrize("fault", ["missing", "mismatched", "duplicate", "unavailable"])
def test_uncertain_witness_keeps_desired_observation_without_credit(
    store: Store, fault: str
) -> None:
    contract = store.contract()
    pending = _begin(store, contract)
    commit_owned_sqlite_insert(contract.config, VALUES, pending.baseline_path)
    assert len(store.event_attempts()) == 1
    _disturb_witness(store, fault)
    assert store.read_frame() == StoreFrame(1, PROTECTED)
    if fault == "missing":
        assert store.event_attempts() == ()
    elif fault == "duplicate":
        first, second = store.event_attempts()
        assert first == second
    elif fault == "unavailable":
        with pytest.raises(sqlite3.OperationalError):
            store.event_attempts()

    _assert_no_owned_credit(store, pending, verify_write(pending, seal=False))


def test_missing_native_capability_keeps_an_explicit_owned_attempt_held(
    store: Store,
) -> None:
    contract = store.contract(native_writer=False)
    pending = _begin(store, contract)
    with closing(sqlite3.connect(store.database)) as independent:
        _external_commit(independent)
    assert store.read_frame() == StoreFrame(1, PROTECTED)
    assert store.event_attempts() == ()

    _assert_no_owned_credit(store, pending, verify_write(pending, seal=False))
    assert reconcile(contract, pending.write_key, ledger=store.ledger) == "uncertain"
    active = store.ledger.latest(pending.write_key)
    assert active is not None and active.resolved is not None
    assert active.resolved["outcome"] == "UNCERTAIN"
    with pytest.raises(WriteHeld):
        _begin(store, contract)
    assert len(store.ledger.attempts()) == 1


def test_valid_witness_cannot_overrule_known_refutation_or_later_upgrade(
    store: Store,
) -> None:
    contract = store.contract()
    pending = _begin(store, contract)
    commit_owned_sqlite_insert(contract.config, VALUES, pending.baseline_path)
    witness = store.event_attempts()
    assert len(witness) == 1
    store.set_protected("collateral-change")
    assert store.read_frame() == StoreFrame(1, "collateral-change")

    result = verify_write(pending, seal=False)
    assert result.outcome is Outcome.REFUTED and not result.verified
    assert result.checked["frame"] == "refuted" and not result.retry_safe
    assert result.receipt_outcome != "verified"
    prior = store.ledger.latest(pending.write_key)
    assert prior is not None and prior.state == "held" and prior.resolved is not None
    assert prior.resolved["outcome"] == "REFUTED"

    store.set_protected(PROTECTED)
    assert store.read_frame() == StoreFrame(1, PROTECTED)
    assert store.event_attempts() == witness
    assert reconcile(contract, pending.write_key, ledger=store.ledger) == "held"
    final = store.ledger.latest(pending.write_key)
    assert final is not None and final.attempt_id == prior.attempt_id
    assert final.resolved == prior.resolved and final.revision == prior.revision
    with pytest.raises(WriteHeld):
        _begin(store, contract)


def test_owned_contract_cannot_be_opened_for_observation(store: Store) -> None:
    with pytest.raises(VerifierError):
        begin_write(
            store.contract(),
            VALUES,
            actor=ACTOR,
            ledger=store.ledger,
            write_key="synthetic-downgrade",
            authorization_ref=AUTHORIZATION,
            purpose="observation",
        )
    assert store.read_frame() == StoreFrame(0, PROTECTED)
    assert store.event_attempts() == () and store.ledger.attempts() == []



def test_native_witness_append_failure_rolls_back_target_write(store: Store) -> None:
    with closing(sqlite3.connect(store.database)) as initializer:
        initializer.executescript(
            f"DROP TABLE {WITNESS_TABLE};"
            f"CREATE TABLE {WITNESS_TABLE} ("
            "attempt TEXT PRIMARY KEY NOT NULL, "
            "witness TEXT NOT NULL CHECK (witness = 'reject-every-native-witness')"
            ") STRICT;"
        )
    contract = store.contract()
    pending = _begin(store, contract)
    with pytest.raises(VerifyRunError) as refusal:
        commit_owned_sqlite_insert(contract.config, VALUES, pending.baseline_path)
    assert isinstance(
        refusal.value.__cause__ or refusal.value.__context__, sqlite3.IntegrityError
    )
    assert store.read_frame() == StoreFrame(0, PROTECTED)
    assert store.event_attempts() == ()
    with closing(sqlite3.connect(store.database)) as independent:
        _external_commit(independent)
    assert store.read_frame() == StoreFrame(1, PROTECTED)
    _assert_no_owned_credit(store, pending, verify_write(pending, seal=False))


def test_pending_and_reconcile_cannot_downgrade_journal_purpose(store: Store) -> None:
    contract = store.contract(native_writer=False)
    pending = _begin(store, contract)
    saved = json.loads(pending.baseline_path.read_bytes())
    saved["purpose"] = "observation"
    pending.baseline_path.write_text(json.dumps(saved), encoding="utf-8")
    with closing(sqlite3.connect(store.database)) as independent:
        _external_commit(independent)
    assert store.read_frame() == StoreFrame(1, PROTECTED)

    result = verify_write(pending, seal=False)
    assert result.outcome is Outcome.UNCERTAIN and not result.verified
    assert result.receipt_outcome != "verified" and not result.retry_safe
    assert reconcile(contract, pending.write_key, ledger=store.ledger) == "uncertain"
    active = store.ledger.latest(pending.write_key)
    assert active is not None and active.attempt_id == pending.write_id
    assert active.begun["purpose"] == "owned_attempt" and active.resolved is not None
    assert active.resolved["outcome"] == "UNCERTAIN"
    assert active.resolved["retry_safe"] is False and active.resolved["landed"] is False
    with pytest.raises(WriteHeld):
        _begin(store, contract)
    assert len(store.ledger.attempts()) == 1
