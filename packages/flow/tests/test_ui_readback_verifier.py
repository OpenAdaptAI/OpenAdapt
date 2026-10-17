"""The separate-session UI read-back verifier, without a browser.

A fake driver stands in for the browser so each rule can be pinned exactly:
what the check confirms, what it contradicts, when it refuses to decide, and
how it labels itself. ``tests/test_ui_readback_browser.py`` runs the same
verifier against real pages in a real browser.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator, Optional

import pytest

from openadapt_flow.deployment import (
    EffectsConfig,
    _effect_verifier_config_identity,
    build_effect_verifier,
)
from openadapt_flow.mockmed.chart_pages import (
    READER_PASSWORD,
    READER_USERNAME,
    chart_readback_spec,
)
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    RestRecordVerifier,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.effects.adapter import classify_adapter_result
from openadapt_flow.runtime.effects.ui_readback import (
    FieldReader,
    RawPage,
    ReadbackSessionError,
    SeparateSessionReadbackVerifier,
    SessionFacts,
    UiReadbackSpec,
    build_ui_readback_verifier,
    normalize_expected,
    normalize_value,
    resolve_spec,
)
from openadapt_flow.runtime.replayer import _effect_read_wording
from openadapt_flow.verification import VerificationTier, verifier_effect_tier

BASE = "http://chart.test"
ENV = {
    "CHART_READER_USERNAME": READER_USERNAME,
    "CHART_READER_PASSWORD": READER_PASSWORD,
}
FRESH = SessionFacts(own_browser=True, fresh_context=True, signed_in=True)


class _Session:
    def __init__(self, pages: list[RawPage], facts: SessionFacts) -> None:
        self._pages = pages
        self.facts = facts
        self.reads = 0

    def read(self) -> RawPage:
        page = self._pages[min(self.reads, len(self._pages) - 1)]
        self.reads += 1
        return page


class FakeDriver:
    """Hands out one scripted session per ``open`` call."""

    def __init__(
        self,
        *sessions: list[RawPage],
        facts: SessionFacts = FRESH,
        error: Optional[str] = None,
    ) -> None:
        self._sessions = list(sessions)
        self._facts = facts
        self._error = error
        self.opened: list[_Session] = []
        self.credentials: list[tuple[str, str]] = []

    def open(self, target: Any, username: str, password: str) -> Any:
        self.credentials.append((username, password))
        if self._error is not None:
            raise ReadbackSessionError(self._error)
        pages = self._sessions.pop(0) if len(self._sessions) > 1 else self._sessions[0]
        session = _Session(pages, self._facts)
        self.opened.append(session)

        @contextlib.contextmanager
        def ctx() -> Iterator[_Session]:
            yield session

        return ctx()


def _spec(**overrides: Any) -> UiReadbackSpec:
    data = chart_readback_spec(BASE)
    data.update(overrides)
    return UiReadbackSpec.model_validate(data)


def _verifier(driver: FakeDriver, spec: Optional[UiReadbackSpec] = None) -> Any:
    return build_ui_readback_verifier(
        spec or _spec(),
        {"patient_id": "p1"},
        timeout_s=1.0,
        poll_interval_s=0.0,
        driver=driver,
        environ=ENV,
    )


def _page(*rows: dict[str, Optional[str]], identity: str = "p1", **kw: Any) -> RawPage:
    return RawPage(ok=True, identity=identity, rows=list(rows), **kw)


def _row(rid: str, kind: str = "Triage", note: str = "chest pain") -> dict:
    return {"id": rid, "type": kind, "note": note}


MATCH = {"patient_id": "p1", "type": "Triage"}


def _written(**kw: Any) -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=MATCH,
        expected_count=1,
        forbid_collateral_loss=True,
        timeout_s=0.0,
        **kw,
    )


def _note_equals(value: str = "chest pain") -> Effect:
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match=MATCH,
        field="note",
        value=ValueExpr(literal=value),
        timeout_s=0.0,
    )


EMPTY_BEFORE = EffectState(substrate="ui-readback", reachable=True, records=[])


# -- what it is -----------------------------------------------------------------


def test_declares_tier_two_and_never_a_system_of_record_read() -> None:
    verifier = _verifier(FakeDriver([_page()]))
    assert verifier.substrate == "ui-readback"
    assert verifier.verification_tier is VerificationTier.INDEPENDENT_SESSION
    assert verifier.independent_system_of_record is False
    assert not verifier.verification_tier.reads_system_of_record()
    assert verifier.verification_tier.is_independent_of_actuation()
    # A gate that requires a system-of-record read (tier 1) refuses this
    # verifier: it never claims that tier.
    assert verifier_effect_tier(verifier, _written()) is not (
        VerificationTier.INDEPENDENT_SYSTEM
    )
    # It must never be handed the acting session's backend.
    assert not hasattr(verifier, "bind_backend")


def test_halt_messages_do_not_call_it_a_system_of_record() -> None:
    prefix, read = _effect_read_wording(VerificationTier.INDEPENDENT_SESSION)
    assert "system of record" not in (prefix + read).lower()
    assert "separate session" in read
    assert _effect_read_wording(VerificationTier.INDEPENDENT_SYSTEM)[1] == (
        "system of record"
    )
    assert (
        "system of record"
        not in _effect_read_wording(VerificationTier.IMMEDIATE_SCREEN)[1]
    )


# -- verdicts --------------------------------------------------------------------


def test_confirms_the_saved_row() -> None:
    driver = FakeDriver([_page(_row("7"))])
    verifier = _verifier(driver)
    assert verifier.verify(_written(), EMPTY_BEFORE).verdict is Verdict.CONFIRMED
    assert verifier.verify(_note_equals(), EMPTY_BEFORE).verdict is Verdict.CONFIRMED
    # One fresh session per check, each with its own sign-in.
    assert len(driver.opened) == 2
    assert driver.credentials == [(READER_USERNAME, READER_PASSWORD)] * 2


def test_partial_save_is_refuted_and_labeled_as_a_record_page() -> None:
    verdict = _verifier(FakeDriver([_page(_row("7", note=""))])).verify(
        _note_equals(), EMPTY_BEFORE
    )
    assert verdict.verdict is Verdict.REFUTED
    assert "system of record" not in verdict.reason
    assert "record page" in verdict.reason


def test_a_missing_row_never_proves_that_nothing_was_saved() -> None:
    verdict = _verifier(FakeDriver([_page(empty_marker=True)])).verify(
        _written(), EMPTY_BEFORE
    )
    assert verdict.verdict is Verdict.REFUTED
    # A page can hide a saved row, so absence stays unknown: the run halts
    # into reconciliation instead of reporting that the record did not change.
    assert verdict.observed_count is None
    assert verdict.observed_effect == "unknown"
    assert "not proof that nothing was saved" in verdict.reason


def test_duplicate_and_lost_update_are_refuted() -> None:
    before = EffectState(
        substrate="ui-readback",
        reachable=True,
        records=[{**_row("1", "Consult", "allergy"), "patient_id": "p1"}],
    )
    duplicate = _verifier(
        FakeDriver([_page(_row("1", "Consult", "allergy"), _row("2"), _row("3"))])
    ).verify(_written(), before)
    assert duplicate.verdict is Verdict.REFUTED
    assert duplicate.observed_count == 2
    assert classify_adapter_result(duplicate).value == "conflicting"

    lost = _verifier(FakeDriver([_page(_row("2"))])).verify(_written(), before)
    assert lost.verdict is Verdict.REFUTED
    assert "vanished" in lost.reason


def test_a_page_for_another_record_is_indeterminate() -> None:
    verdict = _verifier(FakeDriver([_page(_row("7"), identity="p2")])).verify(
        _written(), EMPTY_BEFORE
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "different record identifier" in verdict.reason
    assert "p2" not in verdict.reason


def test_a_blocked_request_makes_the_read_indeterminate() -> None:
    verdict = _verifier(FakeDriver([_page(_row("7"), blocked_requests=1)])).verify(
        _written(), EMPTY_BEFORE
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "blocked" in verdict.reason


def test_layout_changes_are_indeterminate() -> None:
    no_cell = _verifier(
        FakeDriver([_page({"id": "7", "type": "Triage", "note": None})])
    ).verify(_written(), EMPTY_BEFORE)
    assert no_cell.verdict is Verdict.INDETERMINATE
    assert "no element for field 'note'" in no_cell.reason

    no_rows_no_marker = _verifier(FakeDriver([_page(empty_marker=False)])).verify(
        _written(), EMPTY_BEFORE
    )
    assert no_rows_no_marker.verdict is Verdict.INDETERMINATE
    assert "layout may have changed" in no_rows_no_marker.reason


def test_an_unloaded_page_is_unavailable() -> None:
    verdict = _verifier(FakeDriver([RawPage(ok=False, reason="timeout")])).verify(
        _written(), EMPTY_BEFORE
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.unavailable is True


def test_shortened_values_block_only_the_rows_they_could_belong_to() -> None:
    shortened = _verifier(FakeDriver([_page(_row("7", note="chest…"))])).verify(
        _note_equals(), EMPTY_BEFORE
    )
    assert shortened.verdict is Verdict.INDETERMINATE
    assert "shortened" in shortened.reason

    # An older Consult row with a long note does not block the Triage check.
    other = _verifier(
        FakeDriver([_page(_row("3", "Consult", "long history..."), _row("7"))])
    ).verify(_note_equals(), EMPTY_BEFORE)
    assert other.verdict is Verdict.CONFIRMED


def test_display_formats_normalize_both_sides() -> None:
    spec = _spec(
        fields={
            "id": {"attribute": "data-record-id"},
            "type": {"selector": "td.enc-type", "casefold": True},
            "note": {"selector": "td.enc-note"},
            "visit": {"selector": "td.visit", "date_format": "%b %d, %Y"},
        }
    )
    row = {"id": "7", "type": "TRIAGE", "note": "chest   pain ", "visit": "Mar 2, 1984"}
    verifier = _verifier(FakeDriver([_page(row)]), spec)
    assert verifier.verify(_note_equals("chest pain"), EMPTY_BEFORE).confirmed
    visit = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match=MATCH,
        field="visit",
        value=ValueExpr(literal="1984-03-02"),
        timeout_s=0.0,
    )
    assert verifier.verify(visit, EMPTY_BEFORE).confirmed

    bad = {**row, "visit": "sometime in March"}
    unparsed = _verifier(FakeDriver([_page(bad)]), spec).verify(visit, EMPTY_BEFORE)
    assert unparsed.verdict is Verdict.INDETERMINATE


def test_normalization_helpers() -> None:
    reader = FieldReader(date_format="%d/%m/%Y", casefold=True)
    assert normalize_value(" 02/03/1984 ", reader) == ("1984-03-02", True)
    assert normalize_value("not a date", reader)[1] is False
    assert normalize_value(None, reader) == (None, True)
    assert normalize_expected("1984-03-02", reader) == "1984-03-02"
    assert normalize_expected("02/03/1984", reader) == "1984-03-02"
    assert normalize_expected("A  B", FieldReader(casefold=True)) == "a b"


def test_polls_until_the_saved_row_appears() -> None:
    driver = FakeDriver(
        [_page(empty_marker=True), _page(empty_marker=True), _page(_row("7"))]
    )
    effect = _written().model_copy(update={"timeout_s": 5.0})
    assert _verifier(driver).verify(effect, EMPTY_BEFORE).confirmed
    assert driver.opened[0].reads == 3


def test_a_session_that_is_not_fresh_is_refused() -> None:
    stale = SessionFacts(own_browser=True, fresh_context=False, signed_in=True)
    verdict = _verifier(FakeDriver([_page(_row("7"))], facts=stale)).verify(
        _written(), EMPTY_BEFORE
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "fresh" in verdict.reason


def test_a_failed_sign_in_is_unavailable_and_leaks_no_secret() -> None:
    driver = FakeDriver([_page()], error="could not open a signed-in session")
    verifier = _verifier(driver)
    verdict = verifier.verify(_written(), EMPTY_BEFORE)
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.unavailable is True
    assert READER_PASSWORD not in verdict.reason
    before = verifier.capture_pre_state()
    assert before.reachable is False
    probe = verifier.test_connection()
    assert probe.ok is False
    assert READER_PASSWORD not in probe.reason


def test_pre_state_and_probe_on_a_readable_page() -> None:
    verifier = _verifier(FakeDriver([_page(_row("1", "Consult", "x"))]))
    before = verifier.capture_pre_state()
    assert before.reachable is True
    assert before.records == [
        {"id": "1", "type": "Consult", "note": "x", "patient_id": "p1"}
    ]
    assert before.detail["fresh_context"] is True
    assert "p1" not in before.detail["record_url_template"]
    probe = verifier.test_connection()
    assert probe.ok is True
    assert "system of record" not in probe.reason

    missing = _verifier(FakeDriver([_page(identity="", not_found=True)]))
    assert missing.capture_pre_state().reachable is True
    assert missing.capture_pre_state().records == []


def test_construction_refuses_missing_credentials() -> None:
    with pytest.raises(ValueError, match="CHART_READER_PASSWORD"):
        build_ui_readback_verifier(
            _spec(),
            {"patient_id": "p1"},
            environ={"CHART_READER_USERNAME": READER_USERNAME},
            driver=FakeDriver([_page()]),
        )


def test_spec_binding() -> None:
    target = resolve_spec(_spec(), {"patient_id": "a/b c"})
    assert target.record_url == BASE + "/chart/patient/a%2Fb%20c"
    assert target.identity_value == "a/b c"
    with pytest.raises(ValueError, match="not supplied"):
        resolve_spec(_spec(), {})
    with pytest.raises(ValueError, match="absolute"):
        _spec(record_url="/chart/patient/{patient}")
    with pytest.raises(ValueError, match="origin"):
        resolve_spec(
            _spec(record_url="http://{patient}/chart"),
            {"patient_id": "evil.test"},
        )
    with pytest.raises(ValueError, match="at least one field"):
        _spec(fields={})
    with pytest.raises(ValueError, match="start with"):
        _spec(read_post_paths=["graphql"])
    with pytest.raises(ValueError, match="identity.reader.selector"):
        _spec(identity={"reader": {"selector": " "}, "value": {"param": "p"}})
    with pytest.raises(ValueError, match="must not also be a row field"):
        _spec(
            identity={
                "reader": {"selector": "#chart-patient"},
                "value": {"param": "patient_id"},
                "field": "note",
            }
        )
    literal = _spec(
        url_params={"patient": "p9"},
        identity={"reader": {"selector": "#chart-patient"}, "value": "p9"},
    )
    assert resolve_spec(literal, {}).record_url.endswith("/chart/patient/p9")


# -- deployment wiring -------------------------------------------------------------


def test_deployment_builds_the_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    cfg = EffectsConfig.model_validate(
        {"kind": "ui_readback", "ui_readback": chart_readback_spec(BASE)}
    )
    verifier = build_effect_verifier(cfg, {"patient_id": "p1"})
    assert isinstance(verifier, SeparateSessionReadbackVerifier)
    with pytest.raises(ValueError, match="requires an effects.ui_readback section"):
        build_effect_verifier(EffectsConfig(kind="ui_readback"), {})
    with pytest.raises(ValueError, match="not supplied"):
        build_effect_verifier(cfg, {})


def test_existing_verifier_identities_do_not_change() -> None:
    # Digests computed with origin/main (before ``ui_readback`` existed). The
    # new optional field must not enter the identity of any verifier that
    # does not use it, or every issued admission bound to one would break.
    rest = RestRecordVerifier("http://sor.test")
    cfg = EffectsConfig(kind="rest", base_url="http://sor.test")
    assert _effect_verifier_config_identity(cfg, rest) == (
        "sha256:9f288a5b15b66001565c6d5bbd0f0a09bf92485280fe5de37b421802c3e0e663"
    )
