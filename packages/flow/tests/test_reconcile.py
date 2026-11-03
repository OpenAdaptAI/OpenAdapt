"""The cross-system reconciler on real readers: SQLite for the EHR and the
payer, X12 278 files for the clearinghouse."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openadapt_flow.reconcile import ReconcileSpec, reconcile, x12
from openadapt_flow.verifier import Outcome, VerifierError

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "x12_fixtures", _HERE.parent / "benchmark" / "reconcile" / "x12_fixtures.py"
)
fx = importlib.util.module_from_spec(_spec)
sys.modules["x12_fixtures"] = fx
_spec.loader.exec_module(fx)  # type: ignore[union-attr]

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _db(path: Path, ddl: str, rows: list[tuple]) -> str:
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.execute(ddl)
    marks = ",".join("?" * len(rows[0])) if rows else ""
    for row in rows:
        con.execute(f"INSERT INTO t VALUES ({marks})", row)
    con.commit()
    con.close()
    return str(path)


def _sql(db: str, query: str) -> dict:
    return {"kind": "sql", "sqlite_database": db, "sql_query": query}


@pytest.fixture
def world(tmp_path: Path):
    """A clean unit: PA-1 ordered, carried, decided and written back."""

    def build(
        *,
        ehr=None,
        reviews=None,
        payer=None,
        writeback=None,
        x12_text=None,
    ) -> ReconcileSpec:
        ehr = (
            ehr
            if ehr is not None
            else [
                ("PA-1", "M1", "97110", "2026-11-20", "2026-11-01T09:00:00Z", "active")
            ]
        )
        payer = (
            payer
            if payer is not None
            else [
                (
                    "PA-1",
                    "M1",
                    "97110",
                    "2026-11-20",
                    "complete",
                    "AUTH9",
                    "2026-11-01T10:00:00Z",
                )
            ]
        )
        writeback = (
            writeback if writeback is not None else [("PA-1", "AUTH9", "complete")]
        )
        reviews = (
            reviews
            if reviews is not None
            else [fx.Review("PA-1", "M1", "97110", "2026-11-20", auth_number="AUTH9")]
        )
        e = _db(
            tmp_path / "ehr.db",
            "CREATE TABLE t (submission_id, member_id, service_code, "
            "service_date, submitted_at, status)",
            ehr,
        )
        p = _db(
            tmp_path / "payer.db",
            "CREATE TABLE t (identifier, member_id, service_code, service_date, "
            "outcome, auth_number, decided_at)",
            payer,
        )
        w = _db(
            tmp_path / "wb.db",
            "CREATE TABLE t (submission_id, auth_number, outcome)",
            writeback,
        )
        inbox = tmp_path / "inbox"
        inbox.mkdir(exist_ok=True)
        for i, r in enumerate(reviews):
            (inbox / f"r{i}.x12").write_text(
                fx.interchange("278", [fx.review_set(r)], control=str(i + 1))
            )
        if x12_text is not None:
            (inbox / "extra.x12").write_text(x12_text)
        anchor = {"param": "ehr.submitted_at"}
        same = {
            "member_id": {"param": "ehr.member_id"},
            "service_code": {"param": "ehr.service_code"},
            "service_date": {"param": "ehr.service_date"},
        }
        return ReconcileSpec.model_validate(
            {
                "unit": "prior_authorization",
                "anchor": "ehr",
                "legs": {
                    "ehr": {
                        "role": "ehr",
                        "verifier": _sql(e, "SELECT * FROM t"),
                        "identity": {"submission_id": {"param": "submission_id"}},
                        "provides": {
                            k: k
                            for k in (
                                "member_id",
                                "service_code",
                                "service_date",
                                "submitted_at",
                            )
                        },
                    },
                    "clearinghouse": {
                        "role": "clearinghouse",
                        "verifier": {
                            "kind": "x12",
                            "options": {
                                "root": str(inbox),
                                "glob": "*.x12",
                                "transaction": "278",
                            },
                        },
                        "identity": {"trace": {"param": "submission_id"}},
                        "compare": same,
                        "duplicate_key": same,
                        "due": {"after": anchor["param"], "within_s": 3600},
                    },
                    "payer": {
                        "role": "payer",
                        "verifier": _sql(p, "SELECT * FROM t"),
                        "identity": {"identifier": {"param": "submission_id"}},
                        "compare": same,
                        "duplicate_key": same,
                        "provides": {
                            "outcome": "outcome",
                            "auth_number": "auth_number",
                            "decided_at": "decided_at",
                        },
                        "due": {"after": anchor["param"], "within_s": 259200},
                    },
                    "writeback": {
                        "role": "writeback",
                        "verifier": _sql(w, "SELECT * FROM t"),
                        "identity": {"submission_id": {"param": "submission_id"}},
                        "compare": {
                            "auth_number": {"param": "payer.auth_number"},
                            "outcome": {"param": "payer.outcome"},
                        },
                        "when": {
                            "param": "payer.outcome",
                            "values": ["complete", "partial"],
                        },
                        "due": {"after": "payer.decided_at", "within_s": 86400},
                    },
                },
            }
        )

    return build


def _kinds(result) -> set[tuple[str, str, str]]:
    return {(b.kind, b.leg, b.outcome.value) for b in result.breaks}


def test_clean_unit_agrees(world) -> None:
    result = reconcile(world(), {"submission_id": "PA-1"}, now=NOW)
    assert result.outcome is Outcome.CONFIRMED, result.reasons
    assert result.breaks == []
    assert result.scope.unread == ()
    assert set(result.scope.read_paths) == {"database", "file"}
    body = result.to_dict()
    text = str(body)
    for value in ("M1", "97110", "AUTH9", "2026-11-20"):
        assert value not in text  # closed fields only


def test_dropped_at_payer_is_refuted_after_due(world) -> None:
    spec = world(payer=[])
    late = datetime(2026, 11, 9, tzinfo=timezone.utc)
    result = reconcile(spec, {"submission_id": "PA-1"}, now=late)
    assert result.outcome is Outcome.REFUTED
    assert ("not_received", "payer", "REFUTED") in _kinds(result)
    # The write-back depends on the payer's decision, which isn't there.
    wb = next(leg for leg in result.legs if leg.name == "writeback")
    assert wb.outcome is Outcome.UNCERTAIN


def test_absent_inside_due_time_is_uncertain(world) -> None:
    result = reconcile(world(payer=[]), {"submission_id": "PA-1"}, now=NOW)
    assert result.outcome is Outcome.UNCERTAIN
    assert ("not_received", "payer", "UNCERTAIN") in _kinds(result)


def test_code_mismatch_at_clearinghouse(world) -> None:
    spec = world(
        reviews=[fx.Review("PA-1", "M1", "97112", "2026-11-20", auth_number="AUTH9")]
    )
    result = reconcile(spec, {"submission_id": "PA-1"}, now=NOW)
    assert result.outcome is Outcome.REFUTED
    b = next(b for b in result.breaks if b.kind == "mismatch")
    assert b.leg == "clearinghouse" and b.fields == ("service_code",)


def test_member_and_date_mismatch_at_payer(world) -> None:
    spec = world(
        payer=[
            (
                "PA-1",
                "M2",
                "97110",
                "2026-11-21",
                "complete",
                "AUTH9",
                "2026-11-01T10:00:00Z",
            )
        ]
    )
    result = reconcile(spec, {"submission_id": "PA-1"}, now=NOW)
    b = next(b for b in result.breaks if b.kind == "mismatch")
    assert set(b.fields) == {"member_id", "service_date"}


def test_missed_write_back(world) -> None:
    late = datetime(2026, 11, 3, 11, tzinfo=timezone.utc)
    result = reconcile(world(writeback=[]), {"submission_id": "PA-1"}, now=late)
    assert ("not_written_back", "writeback", "REFUTED") in _kinds(result)
    # A stale copy inside the due time is UNCERTAIN, not a break.
    stale = world(writeback=[("PA-1", "", "queued")])
    soon = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    result = reconcile(stale, {"submission_id": "PA-1"}, now=soon)
    assert ("not_written_back", "writeback", "UNCERTAIN") in _kinds(result)


def test_pended_decision_needs_no_write_back(world) -> None:
    spec = world(
        payer=[("PA-1", "M1", "97110", "2026-11-20", "queued", None, None)],
        writeback=[],
    )
    result = reconcile(spec, {"submission_id": "PA-1"}, now=NOW)
    assert result.outcome is Outcome.CONFIRMED, result.reasons
    assert next(leg for leg in result.legs if leg.name == "writeback").skipped


def test_duplicate_under_another_identity(world) -> None:
    spec = world(
        reviews=[
            fx.Review("PA-1", "M1", "97110", "2026-11-20", auth_number="AUTH9"),
            fx.Review("PA-1B", "M1", "97110", "2026-11-20", auth_number="AUTH10"),
        ]
    )
    result = reconcile(spec, {"submission_id": "PA-1"}, now=NOW)
    assert ("duplicate", "clearinghouse", "REFUTED") in _kinds(result)


def test_same_identity_twice_is_duplicate(world) -> None:
    row = (
        "PA-1",
        "M1",
        "97110",
        "2026-11-20",
        "complete",
        "AUTH9",
        "2026-11-01T10:00:00Z",
    )
    result = reconcile(world(payer=[row, row]), {"submission_id": "PA-1"}, now=NOW)
    b = next(b for b in result.breaks if b.leg == "payer")
    assert b.kind == "duplicate" and b.observed_count == 2


def test_look_alike_for_another_member_is_not_a_duplicate(world) -> None:
    spec = world(
        reviews=[
            fx.Review("PA-1", "M1", "97110", "2026-11-20", auth_number="AUTH9"),
            fx.Review("PA-7", "M7", "97110", "2026-11-20", auth_number="AUTH70"),
        ]
    )
    result = reconcile(spec, {"submission_id": "PA-1"}, now=NOW)
    assert result.outcome is Outcome.CONFIRMED, result.reasons


def test_malformed_x12_makes_the_leg_unreadable(world) -> None:
    good = fx.interchange(
        "278",
        [fx.review_set(fx.Review("PA-9", "M9", "97110", "2026-11-20"))],
        control="77",
    )
    spec = world(x12_text=good.replace("GE*1*1", "GE*2*1"))
    result = reconcile(spec, {"submission_id": "PA-1"}, now=NOW)
    assert result.outcome is Outcome.UNCERTAIN
    assert "clearinghouse" in result.scope.unread
    assert result.scope.strength == "unchecked"


def test_missing_store_is_uncertain_never_a_pass(world, tmp_path: Path) -> None:
    spec = world()
    spec.legs["payer"].verifier.sqlite_database = str(tmp_path / "gone" / "x.db")
    result = reconcile(spec, {"submission_id": "PA-1"}, now=NOW)
    assert result.outcome is Outcome.UNCERTAIN
    assert "payer" in result.scope.unread


def test_no_ehr_record_is_uncertain(world) -> None:
    result = reconcile(world(), {"submission_id": "PA-404"}, now=NOW)
    assert result.outcome is Outcome.UNCERTAIN
    assert not any(b.outcome is Outcome.REFUTED for b in result.breaks)


def test_no_submission_time_keeps_absence_uncertain(world) -> None:
    spec = world(ehr=[("PA-1", "M1", "97110", "2026-11-20", None, "active")], payer=[])
    late = datetime(2027, 1, 1, tzinfo=timezone.utc)
    result = reconcile(spec, {"submission_id": "PA-1"}, now=late)
    assert ("not_received", "payer", "UNCERTAIN") in _kinds(result)


def test_caller_must_give_identity(world) -> None:
    with pytest.raises(VerifierError):
        reconcile(world(), {}, now=NOW)


def test_spec_refuses_baseline_effects_and_bad_anchor(world) -> None:
    spec = world().model_dump(mode="json")
    spec["anchor"] = "payer"
    with pytest.raises(ValueError):
        ReconcileSpec.model_validate(spec)


def test_x12_envelope_checks() -> None:
    text = fx.interchange(
        "278",
        [fx.review_set(fx.Review("PA-1", "M1", "97110", "2026-11-20"))],
        control="5",
    )
    assert len(x12.parse(text)) == 1
    for broken in (
        text.replace("IEA*1*000000005", "IEA*1*000000006"),
        text.replace("ST*278*0001", "ST*278*0002"),
        text[:50],
        "not x12 at all",
    ):
        with pytest.raises(x12.X12Error):
            x12.parse(broken)


def test_x12_reads_the_patient_name_and_birth_date() -> None:
    status = fx.ClaimStatus(
        "C1", "M1", "99213", "2026-10-01", family="LOPEZ", birth_date="1987-09-12"
    )
    (record,) = x12.parse(fx.interchange("277", [fx.status_set([status])], control="9"))
    assert record["family"] == "LOPEZ" and record["birth_date"] == "1987-09-12"
