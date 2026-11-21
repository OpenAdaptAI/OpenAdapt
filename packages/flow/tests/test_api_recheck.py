"""The delayed recheck of an API read-back and the system's own log.

Each test states the change that happened between the first read and the
recheck, and the outcome a receipt may carry for it. A change never yields
VERIFIED.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from openadapt_flow.runtime.effects.api_recheck import (
    ALWAYS_NOT_COVERED,
    LogEntry,
    LogRead,
    Readback,
    ReadFailed,
    VerifierReadback,
    begin,
    recheck,
    schedule,
    state_problem,
)

T0 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
TARGET = "Patient/7"


class Record:
    """A record the tests change between the two reads."""

    def __init__(self, fields: Mapping[str, Any], version: Optional[str] = "1") -> None:
        self.fields = dict(fields)
        self.version = version
        self.found = True
        self.fail = False
        self.identity: Any = "7"
        self.source_scope: Any = {"system": "test-ehr", "resource": "Patient"}

    def read(self) -> Readback:
        if self.fail:
            raise ReadFailed("503")
        if not self.found:
            return Readback(found=False)
        return Readback(
            found=True,
            fields=dict(self.fields),
            version=self.version,
            identity=self.identity,
        )


class Log:
    """A log placed by entry ids. ``stamp`` is the server's time, kept to
    show that the window never depends on it."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = [
            {"id": "e1", "kind": "create", "by": "self", "on": True, "stamp": 0.0}
        ]
        self.reach = True
        self.hidden = 0
        self.logs: tuple[Any, ...] = ("create", "update", "delete")
        self.fail = False
        self.stream_scope: Any = {"stream": "test-audit", "actor": "agent-1"}

    def add(self, id: str, kind: str, by: str, on: bool, stamp: float = 0.0) -> None:
        self.entries.append(
            {"id": id, "kind": kind, "by": by, "on": on, "stamp": stamp}
        )

    def checkpoint(self) -> Mapping[str, Any]:
        if self.fail:
            raise ReadFailed("log down")
        return {"ids": [e["id"] for e in self.entries]}

    def since(self, checkpoint: Mapping[str, Any]) -> LogRead:
        if self.fail:
            raise ReadFailed("log down")
        old = set(checkpoint["ids"])
        new = [
            LogEntry(e["id"], e["kind"], e["by"], e["on"])
            for e in self.entries
            if e["id"] not in old
        ]
        return LogRead(new, reached=self.reach, hidden=self.hidden, logs=self.logs)


def _setup(log: Optional[Log] = None, **kw: Any):
    rec = Record({"name.family": "Lovelace", "birthDate": "1815-12-10"})
    state = begin(
        rec,
        target=TARGET,
        first_outcome=kw.pop("first_outcome", "VERIFIED"),
        window_s=kw.pop("window_s", 120),
        log=log,
        now=T0,
        **kw,
    )
    return rec, state, schedule(state)


def _run(state, rec, sched, log=None, *, after_s: float = 200, **kw: Any):
    return recheck(
        state,
        rec,
        target=kw.pop("target", TARGET),
        expected_state_sha256=kw.pop("sha", sched["state_sha256"]),
        log=log,
        now=T0 + timedelta(seconds=after_s),
        **kw,
    )


def test_unchanged_record_and_quiet_log_verify_as_of_the_recheck() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    log.add("x1", "update", "other", False)  # someone else's record: ignored
    log.add("r1", "read", "self", True)  # reads change nothing
    out = _run(state, rec, sched, log)
    assert out.outcome == "VERIFIED"
    assert out.late_outcome == "CONFIRMED"
    scope = out.scope()
    assert scope["basis"] == "system_api" and scope["bases"] == [
        "system_api",
        "activity_log",
    ]
    assert scope["check_level"] == "system_of_record"
    assert scope["read_back_at"] == "2026-11-21T12:00:00Z"
    assert scope["rechecked_at"] == "2026-11-21T12:03:20Z"
    assert scope["recheck_window_s"] == 120.0
    for line in ALWAYS_NOT_COVERED:
        assert line in scope["not_covered"]
    assert "persist" not in out.reason


def test_state_and_schedule_hold_no_values() -> None:
    log = Log()
    rec = Record({"name.family": "Lovelace-Q", "birthDate": "1815-12-10 (q)"})
    rec.identity = "Patient-Qz"
    state = begin(rec, target=TARGET, first_outcome="VERIFIED", window_s=5, log=log)
    sched = schedule(state)
    text = json.dumps(state) + json.dumps(sched)
    for value in ("Lovelace-Q", "1815-12-10 (q)", "Patient-Qz", TARGET):
        assert value not in text
    assert state["salt"] not in json.dumps(sched) and "salt" not in sched
    assert state_problem(state) is None


def test_recheck_before_the_due_time_is_not_checked() -> None:
    rec, state, sched = _setup(Log())
    assert sched["due_at"] == "2026-11-21T12:02:01Z"
    out = _run(state, rec, sched, Log(), after_s=120)
    assert out.outcome == "NOT_CHECKED" and "not due" in out.reason


def test_changed_value_refutes() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    rec.fields["birthDate"] = "1815-12-11"
    rec.version = "2"
    assert _run(state, rec, sched, log).outcome == "REFUTED"


def test_record_gone_refutes() -> None:
    rec, state, sched = _setup()
    rec.found = False
    out = _run(state, rec, sched)
    assert out.outcome == "REFUTED" and "no single record" in out.reason


def test_changed_value_by_another_actor_is_not_checked() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    rec.fields["birthDate"] = "1815-12-11"
    log.add("c1", "update", "other", True)
    assert _run(state, rec, sched, log).outcome == "NOT_CHECKED"


def test_a_to_b_to_a_is_never_verified() -> None:
    """Values back where they were, but the version moved."""

    rec, state, sched = _setup()
    rec.version = "3"
    out = _run(state, rec, sched)
    assert out.outcome == "NOT_CHECKED" and "version" in out.reason


def test_a_to_b_to_a_with_no_version_is_caught_by_the_log() -> None:
    log = Log()
    rec = Record({"birthDate": "1815-12-10"}, version=None)
    state = begin(
        rec, target=TARGET, first_outcome="VERIFIED", window_s=60, log=log, now=T0
    )
    log.add("u2", "update", "other", True)
    log.add("u3", "update", "other", True)
    out = recheck(
        state,
        rec,
        target=TARGET,
        expected_state_sha256=schedule(state)["state_sha256"],
        log=log,
        now=T0 + timedelta(seconds=90),
    )
    assert out.outcome == "NOT_CHECKED"


def test_own_extra_write_to_the_record_refutes() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    log.add("u9", "update", "self", True)
    assert _run(state, rec, sched, log).outcome == "REFUTED"


def test_own_late_collateral_refutes_unless_a_later_write_accounts_for_it() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    log.add("d1", "delete", "self", False)
    assert _run(state, rec, sched, log).outcome == "REFUTED"
    excused = {"d1": "delete"}
    assert _run(state, rec, sched, log, excused_events=excused).outcome == "VERIFIED"


def test_the_writes_own_lagging_entry_is_not_a_change() -> None:
    log = Log()
    log.entries = []
    rec, state, sched = _setup(log, own_events={"mine": "create"})
    log.add("mine", "create", "self", True)
    assert _run(state, rec, sched, log).outcome == "VERIFIED"


def test_entry_stamped_before_the_checkpoint_is_still_in_the_window() -> None:
    """A server clock 1.5 s behind stamps a new entry before the checkpoint's
    newest entry. Its id is new, so it is in the window."""

    log = Log()
    log.entries[0]["stamp"] = 1000.0
    rec, state, sched = _setup(log)
    log.add("late", "update", "self", False, stamp=998.5)
    assert _run(state, rec, sched, log).outcome == "REFUTED"


def test_out_of_order_stamps_do_not_hide_an_entry() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    log.add("b", "update", "other", False, stamp=50.0)
    log.add("a", "update", "unknown", True, stamp=10.0)
    assert _run(state, rec, sched, log).outcome == "NOT_CHECKED"


def test_log_that_did_not_reach_the_checkpoint_is_not_checked() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    log.reach = False
    out = _run(state, rec, sched, log)
    assert out.outcome == "NOT_CHECKED" and "checkpoint" in out.reason


def test_hidden_entries_are_not_checked() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    log.hidden = 3
    assert _run(state, rec, sched, log).outcome == "NOT_CHECKED"


def test_unknown_kind_on_the_record_is_not_checked() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    log.add("q", "unknown", "self", True)
    assert _run(state, rec, sched, log).outcome == "NOT_CHECKED"


def test_read_failures_are_not_checked() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    rec.fail = True
    assert _run(state, rec, sched, log).outcome == "NOT_CHECKED"
    rec.fail = False
    log.fail = True
    assert _run(state, rec, sched, log).outcome == "NOT_CHECKED"


def test_missing_log_when_state_has_one_is_not_checked() -> None:
    rec, state, sched = _setup(Log())
    assert _run(state, rec, sched, None).outcome == "NOT_CHECKED"


def test_no_log_still_verifies_but_says_what_it_did_not_cover() -> None:
    rec, state, sched = _setup()
    out = _run(state, rec, sched)
    assert out.outcome == "VERIFIED" and out.scope()["bases"] == ["system_api"]
    assert any("no log read" in line for line in out.not_covered)


def test_log_that_does_not_record_updates_says_so() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    log.logs = ("create", "delete")
    out = _run(state, rec, sched, log)
    assert any("update" in line for line in out.not_covered)


def test_first_result_that_was_not_verified_is_never_raised() -> None:
    rec, state, sched = _setup(first_outcome="SHOWN")
    out = _run(state, rec, sched)
    assert out.outcome == "NOT_CHECKED"


@pytest.mark.parametrize(
    "edit",
    [
        lambda s: s.update(first_outcome="VERIFIED", window_s=1.0),
        lambda s: s.update(due_at="2026-11-21T12:00:01Z"),
        lambda s: s.update(fields_mac="0" * 64),
        lambda s: s.update(extra=1),
        lambda s: s.pop("log"),
    ],
)
def test_tampered_state_is_not_checked(edit: Any) -> None:
    rec, state, sched = _setup(first_outcome="VERIFIED")
    edit(state)
    assert _run(state, rec, sched).outcome == "NOT_CHECKED"


def test_tampered_first_outcome_does_not_match_the_receipt() -> None:
    rec, state, sched = _setup(first_outcome="SHOWN")
    state["first_outcome"] = "VERIFIED"
    out = _run(state, rec, sched)
    assert out.outcome == "NOT_CHECKED" and "receipt" in out.reason


def test_state_for_another_record_is_not_checked() -> None:
    rec, state, sched = _setup()
    assert _run(state, rec, sched, target="Patient/8").outcome == "NOT_CHECKED"


@pytest.mark.parametrize("window", [0, -1, float("nan"), float("inf"), True])
def test_window_must_be_positive_seconds(window: Any) -> None:
    with pytest.raises(ValueError):
        begin(Record({}), target=TARGET, first_outcome="VERIFIED", window_s=window)


def test_begin_fails_loud_when_a_source_cannot_be_read() -> None:
    log = Log()
    log.fail = True
    with pytest.raises(ReadFailed):
        begin(Record({}), target=TARGET, first_outcome="VERIFIED", window_s=5, log=log)


# -- over real HTTP: the REST verifier on main against MockMed ------------------


def test_rest_readback_over_http_verifies_then_refutes_a_late_edit() -> None:
    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.runtime.effects.effect import Effect, EffectKind
    from openadapt_flow.runtime.effects.rest import RestRecordVerifier
    from tests._rest_read_contract_fixtures import mockmed_rest_read_kwargs

    base_url, db, stop = serve()
    try:
        verifier = RestRecordVerifier(base_url, **mockmed_rest_read_kwargs())
        before = verifier.capture_pre_state()
        written = db.add("p1", "Triage", "BP 120/80", key="k1")
        effect = Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"patient_id": "p1", "type": "Triage", "note": "BP 120/80"},
            expected_count=1,
            timeout_s=1.0,
        )
        source = VerifierReadback(
            verifier, effect, before, fields=["patient_id", "note"]
        )
        state = begin(
            source, target="encounter/1", first_outcome="VERIFIED", window_s=1, now=T0
        )
        sha = schedule(state)["state_sha256"]
        later = T0 + timedelta(seconds=5)
        ok = recheck(
            state, source, target="encounter/1", expected_state_sha256=sha, now=later
        )
        assert ok.outcome == "VERIFIED", ok.reason

        # A background job rewrites the note after the first check.
        with db._lock:
            written["note"] = "BP 180/110"
        bad = recheck(
            state, source, target="encounter/1", expected_state_sha256=sha, now=later
        )
        assert bad.outcome == "REFUTED", bad.reason
    finally:
        stop()


def test_rest_readback_over_http_catches_an_undeclared_field_change() -> None:
    """A field the check doesn't declare moved: the whole-record version
    moved, so the recheck is not VERIFIED."""

    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.runtime.effects.effect import Effect, EffectKind
    from openadapt_flow.runtime.effects.rest import RestRecordVerifier
    from tests._rest_read_contract_fixtures import mockmed_rest_read_kwargs

    base_url, db, stop = serve()
    try:
        verifier = RestRecordVerifier(base_url, **mockmed_rest_read_kwargs())
        before = verifier.capture_pre_state()
        written = db.add("p1", "Triage", "BP 120/80", key="k1")
        effect = Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"patient_id": "p1", "note": "BP 120/80"},
            expected_count=1,
            timeout_s=1.0,
        )
        source = VerifierReadback(verifier, effect, before)
        state = begin(
            source, target="encounter/1", first_outcome="VERIFIED", window_s=1, now=T0
        )
        with db._lock:
            written["type"] = "Consult"
        out = recheck(
            state,
            source,
            target="encounter/1",
            expected_state_sha256=schedule(state)["state_sha256"],
            now=T0 + timedelta(seconds=5),
        )
        assert out.outcome == "NOT_CHECKED", out.reason
    finally:
        stop()


def test_state_survives_a_json_round_trip() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    stored = json.loads(json.dumps(state))
    assert _run(stored, rec, sched, log).outcome == "VERIFIED"


class _Verdict:
    def __init__(self, status: str, records: list[dict[str, Any]]) -> None:
        self.verdict = type("V", (), {"value": status})()
        self.matched_records = records
        self.reason = ""


class _Verifier:
    substrate = "fhir"
    verification_tier = 1
    independent_system_of_record = True

    def __init__(self) -> None:
        self.record: dict[str, Any] = {"id": "7", "meta": {"versionId": "1"}}

    def verify(self, effect: Any, before: Any, context: Any = None) -> _Verdict:
        return _Verdict("confirmed", [dict(self.record)])


class _Effect:
    match = {"id": "7"}


@pytest.mark.parametrize(
    "change",
    [
        {"independent_system_of_record": False},
        {"verification_tier": 2},
        {"substrate": "sql"},
        {"substrate": "onscreen"},
    ],
)
def test_only_an_independent_api_verifier_is_a_readback(change: dict[str, Any]) -> None:
    verifier = _Verifier()
    for name, value in change.items():
        setattr(verifier, name, value)
    with pytest.raises(ValueError):
        VerifierReadback(verifier, _Effect(), None)


def test_screen_tier_for_this_effect_is_refused() -> None:
    verifier = _Verifier()
    verifier.verification_tier_for = lambda effect: 4  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        VerifierReadback(verifier, _Effect(), None)


def test_missing_named_version_fails_the_read() -> None:
    verifier = _Verifier()
    source = VerifierReadback(verifier, _Effect(), None, version_path="meta.versionId")
    assert source.read().version == "1"
    verifier.record = {"id": "7"}
    with pytest.raises(ReadFailed):
        source.read()


# -- review round 1: fail-closed cases ------------------------------------------


def test_begin_refuses_an_absent_record() -> None:
    rec = Record({"birthDate": "1815-12-10"})
    rec.found = False
    with pytest.raises(ReadFailed):
        begin(rec, target=TARGET, first_outcome="VERIFIED", window_s=5)


def test_begin_refuses_a_read_back_with_no_identity() -> None:
    rec = Record({"birthDate": "1815-12-10"})
    rec.identity = None
    with pytest.raises(ReadFailed):
        begin(rec, target=TARGET, first_outcome="VERIFIED", window_s=5)


def test_a_read_back_landing_on_another_record_is_not_checked() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    rec.identity = "8"  # same declared values and version, another record
    out = _run(state, rec, sched, log)
    assert out.outcome == "NOT_CHECKED" and "different record" in out.reason


def test_no_log_verified_lists_other_records_as_not_covered() -> None:
    rec, state, sched = _setup()
    out = _run(state, rec, sched)
    assert out.outcome == "VERIFIED"
    assert any("other records" in line for line in out.not_covered)
    assert "log since the checkpoint" not in out.reason


def test_a_log_that_records_nothing_gives_no_log_basis() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    log.logs = ()
    out = _run(state, rec, sched, log)
    assert out.scope()["bases"] == ["system_api"]
    assert any("other records" in line for line in out.not_covered)
    assert "log since the checkpoint" not in out.reason


def test_missing_and_null_fields_and_version_types_differ() -> None:
    rec, state, sched = _setup()
    rec.version = 1  # was the string "1"
    assert _run(state, rec, sched).outcome == "NOT_CHECKED"
    rec.version = "1"
    rec.fields["birthDate"] = None
    assert _run(state, rec, sched).outcome == "REFUTED"


def test_a_value_with_no_exact_json_form_fails_the_read() -> None:
    from decimal import Decimal

    with pytest.raises(ReadFailed):
        begin(
            Record({"amount": Decimal("1.0")}),
            target=TARGET,
            first_outcome="VERIFIED",
            window_s=5,
        )
    rec, state, sched = _setup()
    rec.fields["birthDate"] = Decimal("1815")
    assert _run(state, rec, sched).outcome == "NOT_CHECKED"


def test_exemptions_need_matching_attribution() -> None:
    log = Log()
    log.entries = []
    rec, state, sched = _setup(log, own_events={"mine": "create"})
    log.add("mine", "create", "other", True)  # own id, another author
    assert _run(state, rec, sched, log).outcome == "NOT_CHECKED"

    log = Log()
    rec, state, sched = _setup(log)
    log.add("q", "unknown", "unknown", True)
    assert _run(state, rec, sched, log, excused_events={"q": "update"}).outcome == (
        "NOT_CHECKED"
    )
    log = Log()
    rec, state, sched = _setup(log)
    log.add("d1", "update", "self", False)  # excused as a delete, logged an update
    out = _run(state, rec, sched, log, excused_events={"d1": "delete"})
    assert out.outcome == "NOT_CHECKED"


def test_a_bare_string_is_not_a_set_of_event_ids() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    log.add("1", "delete", "self", False)
    assert _run(state, rec, sched, log, excused_events="12").outcome == "NOT_CHECKED"
    with pytest.raises(ValueError):
        begin(
            Record({}),
            target=TARGET,
            first_outcome="VERIFIED",
            window_s=5,
            own_events="12",  # type: ignore[arg-type]
        )


def test_a_proven_caller_mutation_refutes_despite_another_actor() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    rec.fields["birthDate"] = "1815-12-11"
    log.add("o1", "update", "other", True)
    log.add("d2", "delete", "self", False)
    assert _run(state, rec, sched, log).outcome == "REFUTED"


def test_a_changed_value_by_an_unknown_actor_is_not_checked() -> None:
    log = Log()
    rec, state, sched = _setup(log)
    rec.fields["birthDate"] = "1815-12-11"
    log.add("u1", "update", "unknown", True)
    assert _run(state, rec, sched, log).outcome == "NOT_CHECKED"
    log = Log()
    rec, state, sched = _setup(log)
    rec.fields["birthDate"] = "1815-12-11"
    log.hidden = 1
    assert _run(state, rec, sched, log).outcome == "NOT_CHECKED"


def test_a_state_is_only_recheckable_with_the_same_host_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec, state, sched = _setup()
    monkeypatch.setenv("OPENADAPT_FRAME_SECRET", "22" * 32)
    out = _run(state, rec, sched)
    assert out.outcome == "NOT_CHECKED" and "frame secret" in out.reason


def test_read_failure_reasons_carry_no_upstream_text() -> None:
    class Leaky:
        source_scope = {"system": "test-ehr", "resource": "Patient"}

        def read(self) -> Readback:
            raise ReadFailed("GET https://sor.example/Patient/7 returned 500")

    rec, state, sched = _setup()
    out = _run(state, Leaky(), sched)
    assert out.outcome == "NOT_CHECKED"
    assert "Patient/7" not in json.dumps(out.to_json())


@pytest.mark.parametrize(
    "edit",
    [
        lambda s: s.update(window_s=1e300),
        lambda s: s.update(window_s="120"),
        lambda s: s.update(read_at=s["salt"]),
        lambda s: s.update(version_mac=1),
        lambda s: s.update(own_events=["x"]),
    ],
)
def test_malformed_state_is_refused_everywhere(edit: Any) -> None:
    rec, state, sched = _setup()
    edit(state)
    assert state_problem(state) is not None
    with pytest.raises(ValueError):
        schedule(state)
    assert _run(state, rec, sched).outcome == "NOT_CHECKED"


def test_the_result_reports_the_observation_interval() -> None:
    rec, state, sched = _setup()
    out = _run(state, rec, sched)
    assert out.scope()["observed_between"] == [
        "2026-11-21T12:03:20Z",
        "2026-11-21T12:03:20Z",
    ]
    assert "read between" in out.reason


def test_an_ambiguous_match_fails_the_read() -> None:
    verifier = _Verifier()
    source = VerifierReadback(verifier, _Effect(), None, version_path="meta.versionId")
    verifier.verify = lambda *a: _Verdict(  # type: ignore[method-assign]
        "refuted", [{"id": "7"}, {"id": "9"}]
    )
    with pytest.raises(ReadFailed):
        source.read()
    verifier.verify = lambda *a: _Verdict("refuted", [])  # type: ignore[method-assign]
    assert source.read().found is False


def test_a_missing_declared_field_fails_the_read() -> None:
    verifier = _Verifier()
    source = VerifierReadback(
        verifier, _Effect(), None, fields=["note"], version_path="meta.versionId"
    )
    with pytest.raises(ReadFailed):
        source.read()


def test_redacted_or_projected_verifiers_are_refused() -> None:
    from openadapt_flow.runtime.effects.adapter import RedactionPolicy
    from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier
    from openadapt_flow.runtime.effects.graphql import GraphQLRecordVerifier

    class Note:
        match = {"note": "A"}

    gql = GraphQLRecordVerifier(
        "https://sor.invalid/graphql",
        query="query { notes { id note version } }",
        redaction=RedactionPolicy(redact_fields=["note"]),
    )
    with pytest.raises(ValueError):
        VerifierReadback(gql, Note(), None, version_path="version")
    gql.redaction = None
    with pytest.raises(ValueError):
        VerifierReadback(gql, Note(), None)  # a projection with no version
    VerifierReadback(gql, Note(), None, version_path="version")

    fhir = FhirEffectVerifier(
        "https://sor.invalid/fhir",
        resource_type="Patient",
        field_paths={"note": "note", "version": "meta.versionId"},
    )
    with pytest.raises(ValueError):
        VerifierReadback(fhir, Note(), None)
    VerifierReadback(fhir, Note(), None, version_path="version")


def test_a_redacted_value_in_the_read_back_fails_the_read() -> None:
    verifier = _Verifier()
    verifier.record = {"id": "7", "meta": {"versionId": "1"}, "note": "[redacted]"}
    source = VerifierReadback(
        verifier, _Effect(), None, fields=["note"], version_path="meta.versionId"
    )
    with pytest.raises(ReadFailed):
        source.read()


def test_fhir_projection_omitted_field_none_fails_the_read() -> None:
    """FHIR _flatten inserts None for missing paths; VerifierReadback must refuse it."""
    verifier = _Verifier()
    verifier.record = {"id": "7", "meta": {"versionId": "1"}, "note": None}
    source = VerifierReadback(
        verifier, _Effect(), None, fields=["note"], version_path="meta.versionId"
    )
    with pytest.raises(ReadFailed):
        source.read()


def test_redacting_verifier_wrapper_and_masked_whole_record_are_refused() -> None:
    from openadapt_flow.runtime.effects.adapter import (
        RedactingVerifier,
        RedactionPolicy,
    )
    from openadapt_flow.runtime.effects.rest import RestRecordVerifier
    from tests._rest_read_contract_fixtures import mockmed_rest_read_kwargs

    class _Eff:
        match = {"note": "A"}

    rest = RestRecordVerifier("https://sor.invalid", **mockmed_rest_read_kwargs())
    wrapped = RedactingVerifier(rest, RedactionPolicy(redact_fields=["secret"]))
    with pytest.raises(ValueError, match="redacts evidence"):
        VerifierReadback(wrapped, _Eff(), None)

    # Even if an un-annotated verifier returns a masked undeclared field,
    # whole-record digesting must refuse it before hashing.
    class _RestMasked(_Verifier):
        substrate = "rest"
        base_url = "https://sor.invalid"

    masked = _RestMasked()
    masked.record = {"id": "7", "note": "A", "secret": "[redacted]"}
    source = VerifierReadback(masked, _Eff(), None, fields=["note"])
    with pytest.raises(ReadFailed, match="redacted"):
        source.read()


def test_source_stream_actor_and_first_identity_bindings_are_enforced() -> None:
    log = Log()
    rec, state, sched = _setup(log, first_identity="7")
    assert _run(state, rec, sched, log).outcome == "VERIFIED"

    # Mismatched first_identity at begin fails.
    with pytest.raises(ReadFailed, match="first check"):
        _setup(log, first_identity="999")

    # Different record source scope with identical local id/fields/version -> NOT_CHECKED.
    other_rec = Record({"name.family": "Lovelace", "birthDate": "1815-12-10"})
    other_rec.source_scope = {"system": "other-tenant-ehr", "resource": "Patient"}
    out = _run(state, other_rec, sched, log)
    assert out.outcome == "NOT_CHECKED" and "record source" in out.reason

    # Unavailable source binding -> ReadFailed on begin, NOT_CHECKED on recheck.
    unbound_rec = Record({"name.family": "Lovelace", "birthDate": "1815-12-10"})
    unbound_rec.source_scope = None
    with pytest.raises(ReadFailed, match="source binding"):
        begin(unbound_rec, target=TARGET, first_outcome="VERIFIED", window_s=5)
    assert _run(state, unbound_rec, sched, log).outcome == "NOT_CHECKED"

    # Different log stream/actor with identical checkpoint -> NOT_CHECKED.
    other_log = Log()
    other_log.stream_scope = {"stream": "other-audit", "actor": "agent-2"}
    out = _run(state, rec, sched, other_log)
    assert out.outcome == "NOT_CHECKED" and "log stream" in out.reason

    # Unavailable log stream binding -> ReadFailed on begin, NOT_CHECKED on recheck.
    unbound_log = Log()
    unbound_log.stream_scope = None
    with pytest.raises(ReadFailed, match="stream and actor"):
        begin(rec, target=TARGET, first_outcome="VERIFIED", window_s=5, log=unbound_log)
    assert _run(state, rec, sched, unbound_log).outcome == "NOT_CHECKED"


def test_incomplete_log_returns_not_checked_even_when_fields_changed_or_missing() -> (
    None
):
    log = Log()
    rec, state, sched = _setup(log)
    log.reach = False
    rec.fields["birthDate"] = "1815-12-11"
    out = _run(state, rec, sched, log)
    assert out.outcome == "NOT_CHECKED" and "checkpoint" in out.reason

    rec.found = False
    out2 = _run(state, rec, sched, log)
    assert out2.outcome == "NOT_CHECKED" and "checkpoint" in out2.reason

    # A proven caller mutation in the available page still refutes immediately.
    log.add("bad1", "delete", "self", False)
    out3 = _run(state, rec, sched, log)
    assert out3.outcome == "REFUTED"
