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

    def read(self) -> Readback:
        if self.fail:
            raise ReadFailed("503")
        if not self.found:
            return Readback(found=False)
        return Readback(found=True, fields=dict(self.fields), version=self.version)


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
    _, state, sched = _setup(Log())
    text = json.dumps(state) + json.dumps(sched)
    assert "Lovelace" not in text and "1815" not in text and TARGET not in text
    assert "salt" not in sched and state_problem(state) is None


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
    assert _run(state, rec, sched, log, excused_event_ids=["d1"]).outcome == "VERIFIED"


def test_the_writes_own_lagging_entry_is_not_a_change() -> None:
    log = Log()
    log.entries = []
    rec, state, sched = _setup(log, own_event_ids=["mine"])
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
