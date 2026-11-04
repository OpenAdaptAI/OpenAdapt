"""What makes a UI read-back count: independence, history, provisional/final.

A scripted session stands in for the browser so each rule is pinned exactly.
``benchmark/ui_second_read/`` runs the same verifier against OpenEMR and
Dolibarr with planted faults.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

import pytest

from openadapt_flow.ir import EffectVerificationEvidence
from openadapt_flow.mockmed.chart_pages import (
    READER_PASSWORD,
    READER_USERNAME,
    chart_readback_spec,
)
from openadapt_flow.policy_packs import load_policy_pack
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.effects.deferred_report import PendingLedger
from openadapt_flow.runtime.effects.ui_read_checks import (
    assess_independence,
    same_view,
)
from openadapt_flow.runtime.effects.ui_readback import (
    RawPage,
    SessionFacts,
    UiReadbackSpec,
    build_ui_readback_verifier,
    settle_ui_readback,
)
from openadapt_flow.verification import VerificationTier

BASE = "http://chart.test"
RECORD_URL = BASE + "/chart/patient/p1"
ENV = {
    "CHART_READER_USERNAME": READER_USERNAME,
    "CHART_READER_PASSWORD": READER_PASSWORD,
    "ACTING_USER": "clerk",
}
FRESH = SessionFacts(own_browser=True, fresh_context=True, signed_in=True)
EMPTY_BEFORE = EffectState(substrate="ui-readback", reachable=True, records=[])


class _Session:
    """Record page reads come from ``pages``; history reads from ``views``."""

    def __init__(self, pages: list[RawPage], views: list[RawPage]) -> None:
        self.facts = FRESH
        self._pages = pages
        self._views = views
        self.reads: list[Optional[str]] = []

    def read(self, target: Any = None) -> RawPage:
        self.reads.append(getattr(target, "record_url", None))
        return self._pages[min(len(self.reads) - 1, len(self._pages) - 1)]

    def read_view(self, url: str, payload: dict[str, Any]) -> RawPage:
        return self._views.pop(0)


class Driver:
    def __init__(self, pages: list[RawPage], views: Optional[list[RawPage]] = None):
        self.pages = pages
        self.views = list(views or [])
        self.sessions: list[_Session] = []

    def open(self, target: Any, username: str, password: str) -> Any:
        session = _Session(self.pages, self.views)
        self.sessions.append(session)

        @contextlib.contextmanager
        def ctx() -> Iterator[_Session]:
            yield session

        return ctx()


class Backend:
    def __init__(self, url: Optional[str]) -> None:
        self._url = url

    def url(self) -> Optional[str]:
        return self._url


class DesktopBackend:
    """An acting session with no URL (desktop, RDP, Citrix)."""

    def screenshot(self) -> bytes:  # pragma: no cover - never read
        return b""


def _row(note: str = "chest pain", status: str = "Approved") -> dict:
    return {"id": "7", "type": "Triage", "note": note, "status": status}


def _page(
    *rows: dict, fetched: Optional[bool] = True, url: str = RECORD_URL
) -> RawPage:
    return RawPage(
        ok=True, identity="p1", rows=list(rows), fetched=fetched, view_url=url
    )


def _spec(**extra: Any) -> UiReadbackSpec:
    data = chart_readback_spec(BASE)
    data["fields"]["status"] = {"selector": "td.status"}
    data.update(extra)
    return UiReadbackSpec.model_validate(data)


def _verifier(
    driver: Driver, spec: Optional[UiReadbackSpec] = None, backend: Any = None
):
    return build_ui_readback_verifier(
        spec or _spec(),
        {"patient_id": "p1"},
        timeout_s=1.0,
        poll_interval_s=0.0,
        driver=driver,
        environ=ENV,
        backend=backend,
    )


def _note(value: str = "chest pain") -> Effect:
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"patient_id": "p1", "type": "Triage"},
        field="note",
        value=ValueExpr(literal=value),
        timeout_s=0.0,
    )


# -- independence ------------------------------------------------------------------


def test_view_comparison_ignores_the_fragment_only() -> None:
    assert same_view(RECORD_URL + "#top", RECORD_URL) is True
    # The same page with another query is the same view.
    assert same_view(RECORD_URL + "?tab=history&saved=1", RECORD_URL) is True
    assert same_view(BASE + "/chart/search?q=p1", RECORD_URL) is False
    # A hash route is a different view in a single-page app.
    assert same_view(BASE + "/app#/patients/p1", BASE + "/app#/save") is False
    assert same_view(None, RECORD_URL) is None


def test_a_distinct_view_in_a_fresh_session_confirms_and_says_why() -> None:
    verdict = _verifier(
        Driver([_page(_row())]), backend=Backend(BASE + "/chart/encounter/save")
    ).verify(_note(), EMPTY_BEFORE)
    assert verdict.verdict is Verdict.CONFIRMED
    independence = verdict.read_evidence["independence"]
    assert independence["qualifies"] is True
    assert independence["distinct_view"] == "yes"
    assert independence["requery"] == "yes"
    assert independence["read_seconds"] >= 0


def test_reading_the_page_the_save_left_never_confirms() -> None:
    verdict = _verifier(Driver([_page(_row())]), backend=Backend(RECORD_URL)).verify(
        _note(), EMPTY_BEFORE
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "distinct_view" in verdict.reason
    assert verdict.read_evidence["independence"]["qualifies"] is False


def test_a_same_view_read_still_refutes_a_wrong_value() -> None:
    verdict = _verifier(
        Driver([_page(_row(note="wrong"))]), backend=Backend(RECORD_URL)
    ).verify(_note(), EMPTY_BEFORE)
    assert verdict.verdict is Verdict.REFUTED


def test_a_cached_document_never_confirms() -> None:
    verdict = _verifier(
        Driver([_page(_row(), fetched=False)]), backend=Backend(BASE + "/other")
    ).verify(_note(), EMPTY_BEFORE)
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "requery" in verdict.reason


def test_a_desktop_acting_session_is_a_different_view() -> None:
    verdict = _verifier(Driver([_page(_row())]), backend=DesktopBackend()).verify(
        _note(), EMPTY_BEFORE
    )
    assert verdict.verdict is Verdict.CONFIRMED
    assert verdict.read_evidence["independence"]["distinct_view"] == "yes"


def test_a_shared_session_never_qualifies() -> None:
    rule = assess_independence(
        own_browser=True,
        fresh_context=False,
        signed_in=True,
        fetched=None,
        read_url=RECORD_URL,
        acting_url=None,
        acting_bound=False,
        acting_has_url=False,
    )
    assert not rule.qualifies
    assert rule.failed == ["separate_session", "requery"]


# -- history -------------------------------------------------------------------------


HISTORY = {
    "url": BASE + "/chart/audit",
    "ready_selector": "#audit",
    "row_selector": "tr",
    "entry": {"record": "td.pid", "what": "td.what", "user": "td.user"},
    "explained": ["^encounter added$"],
    "acting_user_env": "ACTING_USER",
}


def _entry(record: str, what: str, user: str = "clerk") -> dict:
    return {"record": record, "what": what, "user": user}


def _view(*rows: dict) -> RawPage:
    return RawPage(ok=True, rows=list(rows))


OLD = _entry("p9", "encounter added")


def _history_verdict(after: list[dict], before: Optional[list[dict]] = None):
    before_rows = [OLD] if before is None else before
    driver = Driver([_page(_row())], [_view(*before_rows), _view(*after)])
    verifier = _verifier(driver, _spec(history=HISTORY), backend=DesktopBackend())
    pre = verifier.capture_pre_state()
    return verifier.verify(_note(), pre)


def test_history_that_explains_the_save_confirms() -> None:
    verdict = _history_verdict([OLD, _entry("p1", "encounter added")])
    assert verdict.verdict is Verdict.CONFIRMED
    assert verdict.read_evidence["history"]["ok"] is True


def test_history_entry_on_another_record_is_collateral() -> None:
    verdict = _history_verdict(
        [OLD, _entry("p1", "encounter added"), _entry("p2", "encounter added")]
    )
    assert verdict.verdict is Verdict.REFUTED
    assert "another record" in verdict.reason


def test_unexplained_history_entry_is_collateral() -> None:
    verdict = _history_verdict(
        [OLD, _entry("p1", "encounter added"), _entry("p1", "allergy deleted")]
    )
    assert verdict.verdict is Verdict.REFUTED


def test_no_history_entry_cannot_confirm() -> None:
    verdict = _history_verdict([OLD])
    assert verdict.verdict is Verdict.INDETERMINATE


def test_other_users_entries_are_excused() -> None:
    verdict = _history_verdict(
        [OLD, _entry("p1", "encounter added"), _entry("p2", "x", user="nurse")]
    )
    assert verdict.verdict is Verdict.CONFIRMED
    assert verdict.read_evidence["history"]["excused_other_users"] == 1


def test_history_url_may_not_use_the_captured_reference() -> None:
    with pytest.raises(ValueError, match="url_params"):
        _spec(history={**HISTORY, "url": BASE + "/audit/{claim}"})


# -- provisional, then final -----------------------------------------------------------


def _final(tmp_path, **extra: Any) -> dict:
    return {
        "ledger": str(tmp_path / "ledger"),
        "after_s": 0,
        "deadline_hours": 24,
        "status_field": "status",
        "final_values": ["Approved"],
        "pending_values": ["Pending review"],
        "rejected_values": ["Denied"],
        **extra,
    }


def _provisional(tmp_path, status: str = "Pending review", **extra: Any):
    spec = _spec(final=_final(tmp_path, **extra))
    verdict = _verifier(
        Driver([_page(_row(status=status))]), spec, backend=DesktopBackend()
    ).verify(_note(), EMPTY_BEFORE)
    return spec, verdict


def test_a_provisional_read_waits_for_its_final_read(tmp_path) -> None:
    spec, verdict = _provisional(tmp_path)
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.deferred_check and verdict.deferred_check.startswith("deferred:")
    assert verdict.read_evidence["final"]["provisional"] is True
    entry = PendingLedger(spec.final.ledger).get(verdict.deferred_check)
    assert entry.state == "waiting" and entry.record_mac
    # The ledger keeps a digest of the values, never the values.
    assert "chest pain" not in entry.model_dump_json()


def test_a_rejected_status_refutes_now(tmp_path) -> None:
    _spec_, verdict = _provisional(tmp_path, status="Denied")
    assert verdict.verdict is Verdict.REFUTED


def _settle(spec, rows: list[dict], now=None):
    return settle_ui_readback(spec, driver=Driver([_page(*rows)]), environ=ENV, now=now)


def test_final_read_with_unchanged_values_verifies(tmp_path) -> None:
    spec, verdict = _provisional(tmp_path)
    settled = _settle(spec, [_row(status="Approved")])
    assert [(s.id, s.state) for s in settled] == [(verdict.deferred_check, "verified")]


def test_final_read_catches_a_late_change(tmp_path) -> None:
    spec, verdict = _provisional(tmp_path)
    settled = _settle(spec, [_row(note="edited later", status="Approved")])
    assert settled[0].state == "reconciliation_required"
    assert settled[0].code == "changed"


def test_still_pending_keeps_waiting(tmp_path) -> None:
    spec, verdict = _provisional(tmp_path)
    assert _settle(spec, [_row(status="Pending review")]) == []
    assert (
        PendingLedger(spec.final.ledger).get(verdict.deferred_check).state == "waiting"
    )


def test_final_read_waits_for_the_quiet_window(tmp_path) -> None:
    spec, verdict = _provisional(tmp_path, after_s=3600)
    assert _settle(spec, [_row(status="Approved")]) == []
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    assert _settle(spec, [_row(status="Approved")], now=later)[0].state == "verified"


def test_one_session_settles_a_whole_batch(tmp_path) -> None:
    spec = _spec(final=_final(tmp_path))
    ids = []
    for _ in range(5):
        ids.append(
            _verifier(Driver([_page(_row())]), spec, backend=DesktopBackend())
            .verify(_note(), EMPTY_BEFORE)
            .deferred_check
        )
    driver = Driver([_page(_row(status="Approved"))])
    settled = settle_ui_readback(spec, driver=driver, environ=ENV)
    assert sorted(s.id for s in settled) == sorted(ids)
    assert len(driver.sessions) == 1 and len(driver.sessions[0].reads) == 5


def test_a_record_gone_at_the_final_read_reconciles(tmp_path) -> None:
    spec, _verdict = _provisional(tmp_path)
    settled = _settle(spec, [])
    assert settled[0].code == "gone"


# -- policy and receipts -----------------------------------------------------------------


def test_regulated_ui_read_pack_admits_tier_two_only_by_name() -> None:
    assert load_policy_pack("regulated").minimum_effect_tier is (
        VerificationTier.INDEPENDENT_SYSTEM
    )
    pack = load_policy_pack("regulated-ui-read")
    assert pack.minimum_effect_tier is VerificationTier.INDEPENDENT_SESSION
    assert not pack.allow_local_dev_signer and pack.require_identity_on_writes


def test_evidence_without_a_ui_read_keeps_its_bytes() -> None:
    evidence = EffectVerificationEvidence(
        effect_contract_hash="sha256:" + "0" * 64,
        substrate="sql",
        initial_verdict="confirmed",
        final_verdict="confirmed",
    )
    assert "read_evidence" not in evidence.model_dump(mode="json")
    with_read = evidence.model_copy(update={"read_evidence": {"independence": {}}})
    assert "read_evidence" in with_read.model_dump(mode="json")


# -- traps: cached reads, lagging indexes, durable provisional state -------------------


class _SharedContextDriver(Driver):
    """A 'fresh' session that actually reuses a profile (client cache)."""

    def open(self, target: Any, username: str, password: str) -> Any:
        ctx = super().open(target, username, password)
        self.sessions[-1].facts = SessionFacts(
            own_browser=True, fresh_context=False, signed_in=True
        )
        return ctx


def test_a_reused_profile_never_confirms_even_when_the_page_agrees() -> None:
    verdict = _verifier(
        _SharedContextDriver([_page(_row(), fetched=None)]), backend=DesktopBackend()
    ).verify(_note(), EMPTY_BEFORE)
    assert verdict.verdict is Verdict.INDETERMINATE


def test_a_lagging_index_is_polled_for_its_measured_window() -> None:
    empty = RawPage(ok=True, identity="p1", rows=[], fetched=True, view_url=RECORD_URL)
    pages = [empty, empty, _page(_row())]
    lagging = _verifier(
        Driver(pages), _spec(index_lag_s=2.0), backend=DesktopBackend()
    ).verify(_note(), EMPTY_BEFORE)
    assert lagging.verdict is Verdict.CONFIRMED
    # Without the measured window the first empty read is the answer, and an
    # empty search is never taken as proof of anything.
    impatient = _verifier(Driver(pages), backend=DesktopBackend()).verify(
        _note(), EMPTY_BEFORE
    )
    assert impatient.verdict is not Verdict.CONFIRMED


def test_found_then_gone_is_never_a_quiet_not_saved() -> None:
    empty = RawPage(ok=True, identity="p1", rows=[], fetched=True, view_url=RECORD_URL)
    pages = [_page(_row(note="stale")), empty]
    verdict = _verifier(
        Driver(pages), _spec(index_lag_s=2.0), backend=DesktopBackend()
    ).verify(_note(), EMPTY_BEFORE)
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "stopped showing" in verdict.reason


def test_a_provisional_item_is_not_sent_again_after_a_restart(tmp_path) -> None:
    spec, verdict = _provisional(tmp_path)
    # A new process (a restart, a re-run of the worklist) builds a new
    # verifier for the same item: it refuses before input.
    again = _verifier(Driver([_page(_row())]), spec, backend=DesktopBackend())
    assert again.requires_readable_pre_state_for(_note()) is True
    before = again.capture_pre_state()
    assert before.reachable is False
    assert "still provisional" in before.detail["reason"]
    # Once the final read settles it, the item is free again.
    _settle(spec, [_row(status="Approved")])
    assert again._waiting_entry() is None
    assert again.capture_pre_state().reachable is True


def test_a_long_provisional_item_raises_an_alert(tmp_path) -> None:
    spec, verdict = _provisional(tmp_path, alert_hours=48)
    ledger = PendingLedger(spec.final.ledger)
    assert ledger.overdue(48) == []
    later = datetime.now(timezone.utc) + timedelta(hours=49)
    assert [e.id for e in ledger.overdue(48, now=later)] == [verdict.deferred_check]
