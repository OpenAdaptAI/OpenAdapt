"""Document and writer-view refusals apply before durable final promotion.

These are protocol controls. The retained browser regression separately checks
actual HTTP submissions, SQLite rows and durable ledger state.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from openadapt_flow.runtime.effects import Verdict
from openadapt_flow.runtime.effects.deferred_report import PendingLedger
from openadapt_flow.runtime.effects.ui_readback import (
    SessionFacts,
    UiReadbackSpec,
    build_ui_readback_verifier,
    settle_ui_readback,
)
from tests.test_ui_read_checks import (
    BASE,
    EMPTY_BEFORE,
    ENV,
    RECORD_URL,
    Backend,
    DesktopBackend,
    Driver,
    _final,
    _note,
    _page,
    _provisional,
    _row,
    _spec,
    _verifier,
)


@pytest.mark.parametrize("url", [None, "", "about:blank", 7])
def test_bound_browser_unknown_view_never_confirms(url: Any) -> None:
    result = _verifier(Driver([_page(_row())]), backend=Backend(url)).verify(
        _note(), EMPTY_BEFORE
    )
    assert result.verdict is Verdict.INDETERMINATE
    assert result.read_evidence["independence"]["distinct_view"] == "unknown"
    assert result.read_evidence["independence"]["qualifies"] is False
    assert "distinct_view" in result.reason


class _UrlProperty:
    @property
    def url(self) -> None:
        return None


class _FailedUrlProperty:
    @property
    def url(self) -> str:
        raise RuntimeError("synthetic writer observation unavailable")


class _MissingUrlProperty:
    @property
    def url(self) -> str:
        raise AttributeError("synthetic declared URL unavailable")


@pytest.mark.parametrize(
    "backend", [_UrlProperty(), _FailedUrlProperty(), _MissingUrlProperty()]
)
def test_declared_url_observation_is_not_a_native_backend(backend: Any) -> None:
    result = _verifier(Driver([_page(_row())]), backend=backend).verify(
        _note(), EMPTY_BEFORE
    )
    assert result.verdict is Verdict.INDETERMINATE
    assert result.read_evidence["independence"]["distinct_view"] == "unknown"
    assert result.read_evidence["independence"]["qualifies"] is False


@pytest.mark.parametrize("backend", [None, DesktopBackend()])
def test_standalone_and_native_read_controls_remain_qualified(backend: Any) -> None:
    result = _verifier(Driver([_page(_row())]), backend=backend).verify(
        _note(), EMPTY_BEFORE
    )
    assert result.verdict is Verdict.CONFIRMED
    assert result.read_evidence["independence"]["qualifies"] is True


def test_unknown_writer_view_can_still_refute_a_wrong_value() -> None:
    result = _verifier(
        Driver([_page(_row(note="different"))]), backend=Backend(None)
    ).verify(_note(), EMPTY_BEFORE)
    assert result.verdict is Verdict.REFUTED
    assert result.read_evidence["independence"]["qualifies"] is False


@pytest.mark.parametrize("shape", ["same", "changed", "gone", "rejected"])
def test_unfetched_final_document_cannot_settle_any_record(
    tmp_path, shape: str
) -> None:
    spec, first = _provisional(tmp_path)
    raw = _page(_row(status="Approved"), fetched=False)
    if shape == "changed":
        raw = _page(_row(note="changed", status="Approved"), fetched=False)
    elif shape == "gone":
        raw = replace(raw, rows=[], not_found=True)
    elif shape == "rejected":
        raw = _page(_row(status="Denied"), fetched=False)
    settled = settle_ui_readback(spec, driver=Driver([raw]), environ=ENV)
    assert settled == []
    assert PendingLedger(spec.final.ledger).get(first.deferred_check).state == "waiting"


@pytest.mark.parametrize("refusal", ["unreadable", "blocked", "identity", "field"])
def test_document_refusals_keep_final_entry_waiting(tmp_path, refusal: str) -> None:
    spec, first = _provisional(tmp_path)
    raw = _page(_row(status="Approved"))
    if refusal == "unreadable":
        raw = replace(raw, ok=False)
    elif refusal == "blocked":
        raw = replace(raw, blocked_requests=1)
    elif refusal == "identity":
        raw = replace(raw, identity="other-record")
    else:
        raw = replace(raw, rows=[{"id": "7", "type": "Triage", "status": "Approved"}])
    assert settle_ui_readback(spec, driver=Driver([raw]), environ=ENV) == []
    assert PendingLedger(spec.final.ledger).get(first.deferred_check).state == "waiting"


class _SessionFactsDriver(Driver):
    def __init__(self, facts: SessionFacts) -> None:
        super().__init__([_page(_row(status="Approved"))])
        self.facts = facts

    def open(self, target: Any, username: str, password: str) -> Any:
        opened = super().open(target, username, password)
        self.sessions[-1].facts = self.facts
        return opened


@pytest.mark.parametrize(
    "facts",
    [
        SessionFacts(False, True, True),
        SessionFacts(True, False, True),
        SessionFacts(True, True, False),
    ],
)
def test_final_settle_retains_session_fact_refusals(
    tmp_path, facts: SessionFacts
) -> None:
    spec, first = _provisional(tmp_path)
    driver = _SessionFactsDriver(facts)
    assert settle_ui_readback(spec, driver=driver, environ=ENV) == []
    assert driver.sessions[0].reads == []
    assert PendingLedger(spec.final.ledger).get(first.deferred_check).state == "waiting"


@pytest.mark.parametrize("params", [{}, {"job_key": "synthetic-job"}])
def test_fixed_target_pending_guard_covers_empty_parameters(
    tmp_path, params: dict
) -> None:
    data = _spec(final=_final(tmp_path), index_lag_s=0).model_dump(mode="json")
    data.update(record_url=RECORD_URL, url_params={})
    data["identity"]["value"] = {"literal": "p1"}
    spec = UiReadbackSpec.model_validate(data)

    def reader():
        return build_ui_readback_verifier(
            spec,
            params,
            timeout_s=1.0,
            driver=Driver([_page(_row())]),
            environ=ENV,
            backend=Backend(BASE + "/save"),
        )

    first = reader().verify(_note(), EMPTY_BEFORE)
    assert first.deferred_check
    restarted = reader()
    assert restarted.requires_readable_pre_state_for(_note()) is True
    before = restarted.capture_pre_state()
    assert before.reachable is False
    assert "still provisional" in before.detail["reason"]
    assert len(PendingLedger(spec.final.ledger).entries()) == 1
    settled = settle_ui_readback(
        spec, driver=Driver([_page(_row(status="Approved"))]), environ=ENV
    )
    assert [item.state for item in settled] == ["verified"]
    assert reader()._waiting_entry() is None


@pytest.mark.parametrize(
    "legacy",
    [
        "matching",
        "missing_config",
        "invalid_config",
        "unknown_config",
        "changed_config",
        "other_target",
        "missing_config_other_target",
        "same_target_other_url",
        "equivalent_identity",
    ],
)
def test_legacy_null_digest_waiting_entry_needs_positive_other_target(
    tmp_path, legacy: str
) -> None:
    import json
    from pathlib import Path

    data = _spec(final=_final(tmp_path), index_lag_s=0).model_dump(mode="json")
    data.update(record_url=RECORD_URL, url_params={})
    data["identity"]["value"] = {"literal": "p1"}
    spec = UiReadbackSpec.model_validate(data)
    prior = data.copy()
    if legacy in {
        "changed_config",
        "other_target",
        "missing_config_other_target",
        "same_target_other_url",
        "equivalent_identity",
    }:
        prior = {
            **data,
            "final": {**data["final"], "describe": "different configuration"},
        }
    if legacy in {"other_target", "missing_config_other_target"}:
        prior.update(
            record_url=BASE + "/chart/patient/p2",
            identity={**data["identity"], "value": {"literal": "p2"}},
        )
    if legacy in {"same_target_other_url", "equivalent_identity"}:
        prior.update(record_url=RECORD_URL + "?tab=other")
    if legacy == "equivalent_identity":
        data["identity"]["reader"]["casefold"] = True
        spec = UiReadbackSpec.model_validate(data)
        prior["identity"] = {**data["identity"], "value": {"literal": "P1"}}
    prior_spec = UiReadbackSpec.model_validate(prior)
    prior_verifier = build_ui_readback_verifier(
        prior_spec, {}, driver=Driver([_page(_row())]), environ=ENV
    )
    ledger = PendingLedger(spec.final.ledger)
    entry = ledger.add_ui(
        effect_contract_hash=_note().contract_hash(),
        spec_digest=prior_verifier._final_digest(),
        describe=spec.final.describe,
        deadline_hours=24,
        due_after_s=0,
        locator={
            "record_url": prior_spec.record_url,
            "identity": "p2"
            if legacy in {"other_target", "missing_config_other_target"}
            else ("P1" if legacy == "equivalent_identity" else "p1"),
        },
        record_payload="synthetic legacy fixture",
        params_digest=None,
    )
    stored = entry.model_dump(mode="json")
    if legacy in {"missing_config", "missing_config_other_target"}:
        stored.pop("report_format_digest")
    elif legacy == "invalid_config":
        stored["report_format_digest"] = "unknown"
    elif legacy == "unknown_config":
        stored["report_format_digest"] = "0" * 64
    (Path(spec.final.ledger) / (entry.id.split(":")[1] + ".json")).write_text(
        json.dumps(stored)
    )
    restarted = build_ui_readback_verifier(
        spec, {}, driver=Driver([_page(_row())]), environ=ENV
    )
    before = restarted.capture_pre_state()
    assert before.reachable is (legacy == "other_target")
    if legacy != "other_target":
        assert restarted.requires_readable_pre_state_for(_note()) is True
        assert "still provisional" in before.detail["reason"]


def _lagging_provisional(tmp_path):
    spec = _spec(final=_final(tmp_path), index_lag_s=0.05)
    first = _verifier(
        Driver([_page(_row(status="Pending review"))]), spec, backend=DesktopBackend()
    ).verify(_note(), EMPTY_BEFORE)
    assert first.deferred_check
    return spec, first


def test_stale_replica_cannot_settle_after_a_late_change(tmp_path) -> None:
    """A lagging view serves the old copy (HTTP 200) on the first final read;
    the canonical late change shows once the declared lag has passed."""

    spec, first = _lagging_provisional(tmp_path)
    stale = _page(_row(status="Approved"))
    fresh = _page(_row(note="edited later", status="Approved"))
    driver = Driver([stale, fresh])
    settled = settle_ui_readback(spec, driver=driver, environ=ENV)
    assert [(s.id, s.state, s.code) for s in settled] == [
        (first.deferred_check, "reconciliation_required", "changed")
    ]
    assert len(driver.sessions) == 1 and len(driver.sessions[0].reads) == 2


def test_lagging_view_verifies_only_after_the_lag_rereads(tmp_path) -> None:
    spec, first = _lagging_provisional(tmp_path)
    driver = Driver([_page(_row(status="Approved"))])
    settled = settle_ui_readback(spec, driver=driver, environ=ENV)
    assert [(s.id, s.state) for s in settled] == [(first.deferred_check, "verified")]
    assert len(driver.sessions[0].reads) == 2


def test_lagging_view_unreadable_reread_keeps_waiting(tmp_path) -> None:
    spec, first = _lagging_provisional(tmp_path)
    ok = _page(_row(status="Approved"))
    driver = Driver([ok, replace(ok, ok=False)])
    assert settle_ui_readback(spec, driver=driver, environ=ENV) == []
    assert PendingLedger(spec.final.ledger).get(first.deferred_check).state == "waiting"


def test_undeclared_lag_never_settles_verified(tmp_path) -> None:
    """Without ``index_lag_s`` the view's freshness is unknown: a matching
    final read keeps the entry waiting, and only a deadline or another
    check settles it."""

    spec = _spec(final=_final(tmp_path))
    assert spec.index_lag_s is None
    first = _verifier(
        Driver([_page(_row(status="Pending review"))]), spec, backend=DesktopBackend()
    ).verify(_note(), EMPTY_BEFORE)
    assert first.deferred_check
    assert "freshness_undeclared" in first.reason
    assert first.read_evidence["final"]["freshness"] == "freshness_undeclared"
    assert (
        settle_ui_readback(
            spec, driver=Driver([_page(_row(status="Approved"))]), environ=ENV
        )
        == []
    )
    entry = PendingLedger(spec.final.ledger).get(first.deferred_check)
    assert entry.state == "waiting"


def test_undeclared_lag_still_reconciles_a_change(tmp_path) -> None:
    """A UI read may still add evidence against a save."""

    spec = _spec(final=_final(tmp_path))
    first = _verifier(
        Driver([_page(_row(status="Pending review"))]), spec, backend=DesktopBackend()
    ).verify(_note(), EMPTY_BEFORE)
    settled = settle_ui_readback(
        spec,
        driver=Driver([_page(_row(note="edited later", status="Approved"))]),
        environ=ENV,
    )
    assert [(s.id, s.code) for s in settled] == [(first.deferred_check, "changed")]


def test_explicit_zero_lag_is_a_primary_view_single_read(tmp_path) -> None:
    spec, first = _provisional(tmp_path)
    assert spec.index_lag_s == 0
    driver = Driver([_page(_row(status="Approved"))])
    settled = settle_ui_readback(spec, driver=driver, environ=ENV)
    assert [(s.id, s.state) for s in settled] == [(first.deferred_check, "verified")]
    assert len(driver.sessions[0].reads) == 1


def test_settle_cli_labels_a_ui_settlement_screen_grade(
    tmp_path, monkeypatch, capsys
) -> None:
    # A UI final read never reads as a record-store verification.
    from openadapt_flow import cli_deferred
    from openadapt_flow.__main__ import build_parser
    from openadapt_flow.runtime.effects import ui_readback
    from openadapt_flow.runtime.effects.deferred_report import Settlement

    spec = _spec(index_lag_s=0, final=_final(tmp_path))
    settled = Settlement("deferred:" + "a" * 32, "verified", "final", "unchanged")
    monkeypatch.setattr(cli_deferred, "_ui_final_from_config", lambda _path: [spec])
    monkeypatch.setattr(ui_readback, "settle_ui_readback", lambda _spec: [settled])
    args = build_parser().parse_args(["deferred", "settle", "--config", "x.yaml"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert f"{settled.id}: VERIFIED (screen_readback)" in out
