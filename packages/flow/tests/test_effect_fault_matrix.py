"""THE PROOF: screen-verify passes but effect-verify catches each transactional
fault class.

The fault-model study (``benchmark/fault_model/``) found screen/vision
verification silently mishandles 5 of 7 transactional fault classes: a
duplicate submission, an optimistic-UI-then-backend-reject, a partial save, a
stale/concurrent overwrite, and a double-delivered click all leave the SCREEN
showing "saved" while the RECORD is wrong. This test drives each fault at the
REAL persistence boundary (``mockmed.fault_server`` -- the same in-process HTTP
system of record the study uses) and shows, per class:

- **screen-verify** -- the documented weak oracle: does the app paint the
  "saved" banner? (encoded from ``mockmed/static/app.js`` behavior; the
  end-to-end version driving the REAL replayer + OCR is
  ``benchmark/fault_model/run.py``). For all 5 classes it PASSES.
- **effect-verify** -- :class:`RestRecordVerifier` reading the system of
  record at ``GET /api/db`` (never the screen). For all 5 classes it REFUTES.

Same criterion the study judges by (the DB), now enforced by a runtime
component. No model calls; localhost only; CI-reproducible.
"""

from __future__ import annotations

import pytest
import requests

from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    RestRecordVerifier,
    Verdict,
)

TARGET = {"patient_id": "p1", "type": "Triage"}
NOTE = "E2E triage booking three months"

# What the SCREEN concludes under each fault, read straight from
# mockmed/static/app.js saveViaBackend(): a painted "saved" banner == success.
#   ok/partial/stale/duplicate/double  -> commitLocalAndShow -> banner -> True
#   optimistic                         -> paint success NOW   -> banner -> True
#   idempotent                         -> banner (once)       -> True
#   timeout                            -> showSaveError       -> False
#   session (401)                      -> bounces to login    -> False
SCREEN_SHOWS_SUCCESS = {
    "ok": True,
    "partial": True,
    "optimistic": True,
    "duplicate": True,
    "double": True,
    "stale": True,
    "idempotent": True,
    "timeout": False,
    "session": False,
}

# The 5 classes the study named as SILENTLY mishandled by screen verification.
SILENT_FAULT_CLASSES = ["duplicate", "optimistic", "partial", "stale", "double"]


@pytest.fixture
def sor():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def _post(base, mode, *, note=NOTE, key=None):
    payload = {"patient_id": "p1", "type": "Triage", "note": note}
    if key is not None:
        payload["key"] = key
    url = f"{base}/api/encounter?fault={mode}"
    try:
        requests.post(url, json=payload, timeout=1.2)
    except requests.exceptions.RequestException:
        # ``timeout`` mode commits the row server-side, THEN hangs past the
        # client abort -- exactly what app.js sees. The row still landed.
        pass


def _drive_persistence_boundary(base, db, mode):
    """Reproduce the write app.js issues under ``mode`` at the real boundary."""
    if mode in ("duplicate", "double"):
        # app.js fires the write twice (double-submit / double-delivered).
        _post(base, mode)
        _post(base, mode)
    elif mode == "idempotent":
        _post(base, mode, key="run-key")
        _post(base, mode, key="run-key")
    else:
        _post(base, mode)


def _effect_verdict(verifier, before):
    """The step's consequential-save contract: BOTH the record-written and the
    field-equals effects must confirm; the first non-confirmed refutes the
    step (the runtime gate)."""
    written = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=TARGET,
        expected_count=1,
        risk="irreversible",
        timeout_s=1.0,
    )
    field = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match=TARGET,
        field="note",
        value=NOTE,
        risk="irreversible",
        timeout_s=1.0,
    )
    v1 = verifier.verify(written, before)
    if not v1.confirmed:
        return v1
    return verifier.verify(field, before)


@pytest.mark.parametrize("mode", SILENT_FAULT_CLASSES)
def test_screen_passes_but_effect_catches(sor, mode):
    base, db = sor
    seed = mode == "stale"
    db.reset(seed_concurrent=seed)

    verifier = RestRecordVerifier(base)
    before = verifier.capture_pre_state()
    assert before.reachable

    _drive_persistence_boundary(base, db, mode)

    # 1. The screen oracle PASSES (the app painted the saved banner).
    assert SCREEN_SHOWS_SUCCESS[mode] is True, (
        f"{mode}: precondition -- the screen must (wrongly) show success"
    )

    # 2. The effect oracle REFUTES against the system of record.
    verdict = _effect_verdict(verifier, before)
    assert verdict.verdict is Verdict.REFUTED, (
        f"{mode}: effect-verify should catch this fault, got "
        f"{verdict.verdict} ({verdict.reason})"
    )
    assert verdict.should_halt


def test_control_clean_write_both_agree(sor):
    base, db = sor
    db.reset()
    verifier = RestRecordVerifier(base)
    before = verifier.capture_pre_state()
    _drive_persistence_boundary(base, db, "ok")
    assert SCREEN_SHOWS_SUCCESS["ok"] is True
    assert _effect_verdict(verifier, before).verdict is Verdict.CONFIRMED


def test_idempotency_key_neutralizes_duplicate(sor):
    # The recommended fix: with an idempotency key the double-submit collapses
    # to ONE row, so effect-verify confirms where the un-keyed duplicate was
    # refuted -- proving the at-most-once check.
    base, db = sor
    db.reset()
    verifier = RestRecordVerifier(base)
    before = verifier.capture_pre_state()
    _drive_persistence_boundary(base, db, "idempotent")
    written = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=TARGET,
        expected_count=1,
        idempotency_key="run-key",
        timeout_s=1.0,
    )
    assert verifier.verify(written, before).verdict is Verdict.CONFIRMED


def test_timeout_effect_verify_is_more_correct_than_screen(sor):
    # The 6th/7th classes the screen DOES flag: ``timeout`` makes the screen
    # report failure (FALSE-ABORT) though the row landed. Effect-verify reads
    # the system of record and CONFIRMS -- preventing the naive retry that
    # would double-write.
    base, db = sor
    db.reset()
    verifier = RestRecordVerifier(base)
    before = verifier.capture_pre_state()
    _drive_persistence_boundary(base, db, "timeout")
    assert SCREEN_SHOWS_SUCCESS["timeout"] is False  # screen: false-abort
    written = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=TARGET,
        expected_count=1,
        timeout_s=1.0,
    )
    assert verifier.verify(written, before).verdict is Verdict.CONFIRMED


def test_session_expiry_both_safe(sor):
    # ``session`` is the class the screen handles correctly (SAFE-HALT): 401 ->
    # no banner -> the screen halts, and effect-verify agrees nothing landed.
    base, db = sor
    db.reset()
    verifier = RestRecordVerifier(base)
    before = verifier.capture_pre_state()
    _drive_persistence_boundary(base, db, "session")
    assert SCREEN_SHOWS_SUCCESS["session"] is False
    written = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=TARGET,
        expected_count=1,
        timeout_s=0.5,
    )
    # Nothing persisted -> record_written refutes (absent); consistent with the
    # screen's safe halt (both refuse to claim success).
    assert verifier.verify(written, before).verdict is Verdict.REFUTED
