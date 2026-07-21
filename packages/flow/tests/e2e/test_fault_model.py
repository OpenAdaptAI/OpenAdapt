"""Transactional fault-model study — harness tests.

Two layers:

- Pure-Python tests of the outcome taxonomy (``benchmark.fault_model.faults``)
  and the fault registry — fast, no browser.
- End-to-end tests that replay the SAME compiled triage-save bundle against a
  persistence-backed MockMed under each injected transactional fault and
  assert the ground-truth outcome, plus a pin that the flag-gated hook is
  inert with no ``?fault`` query (the normal benchmark is unaffected).

The e2e layer reuses the session-scoped ``bundle`` and module-scoped
``_browser`` fixtures from ``conftest`` (the bundle's template crops come from
the same static files the fault server serves, so it replays unchanged).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional

import pytest
import requests

from benchmark.fault_model import faults as F
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.mockmed.fault_server import FaultDB, serve as fault_serve
from openadapt_flow.runtime import Replayer

from .conftest import NOTE_TEXT, PARAMS, VIEWPORT

pytestmark = pytest.mark.timeout(600)


# -- pure taxonomy tests (no browser) ----------------------------------------


def _row(note: str = NOTE_TEXT, source: str = "replay", **kw) -> dict:
    base = {
        "id": 1,
        "patient_id": F.TARGET_PATIENT,
        "type": F.TARGET_TYPE,
        "note": note,
        "source": source,
        "key": None,
    }
    base.update(kw)
    return base


class TestClassify:
    def test_success(self) -> None:
        outcome, _ = F.classify(
            report_success=True, records=[_row()], seeded_concurrent=False
        )
        assert outcome == F.SUCCESS

    def test_phantom_success_is_undetected(self) -> None:
        # Reported success, but nothing persisted (optimistic-UI reject).
        outcome, _ = F.classify(
            report_success=True, records=[], seeded_concurrent=False
        )
        assert outcome == F.UNDETECTED_FAILURE

    def test_partial_save_is_undetected(self) -> None:
        # Reported success, row present but note dropped.
        outcome, _ = F.classify(
            report_success=True,
            records=[_row(note="")],
            seeded_concurrent=False,
        )
        assert outcome == F.UNDETECTED_FAILURE

    def test_duplicate_is_wrong_action(self) -> None:
        outcome, _ = F.classify(
            report_success=True,
            records=[_row(id=1), _row(id=2)],
            seeded_concurrent=False,
        )
        assert outcome == F.WRONG_ACTION

    def test_lost_update_is_wrong_action(self) -> None:
        # Seeded concurrent row is gone: last-write-wins lost update.
        outcome, _ = F.classify(
            report_success=True,
            records=[_row()],  # only the replay's row remains
            seeded_concurrent=True,
        )
        assert outcome == F.WRONG_ACTION

    def test_lost_update_not_flagged_when_concurrent_row_survives(self) -> None:
        outcome, _ = F.classify(
            report_success=True,
            records=[_row(id=1, source="other"), _row(id=2)],
            seeded_concurrent=True,
        )
        assert outcome == F.SUCCESS

    def test_timeout_after_write_is_false_abort(self) -> None:
        # Halted, but the correct row landed.
        outcome, _ = F.classify(
            report_success=False, records=[_row()], seeded_concurrent=False
        )
        assert outcome == F.FALSE_ABORT

    def test_clean_halt_is_safe(self) -> None:
        outcome, _ = F.classify(
            report_success=False, records=[], seeded_concurrent=False
        )
        assert outcome == F.SAFE_HALT

    def test_silently_mishandled_flag(self) -> None:
        assert F.is_silently_mishandled(F.WRONG_ACTION, report_success=True)
        assert F.is_silently_mishandled(F.UNDETECTED_FAILURE, report_success=True)
        # A halt (report_success False) is never a SILENT mishandle.
        assert not F.is_silently_mishandled(F.FALSE_ABORT, report_success=False)
        assert not F.is_silently_mishandled(F.SUCCESS, report_success=True)


class TestRegistry:
    def test_seven_transactional_classes_present(self) -> None:
        transactional = [f for f in F.FAULTS if f.fault_class[0].isdigit()]
        # Fault classes 1..7 from the review, each exactly once.
        numbers = sorted(f.fault_class.split(".")[0] for f in transactional)
        assert numbers == ["1", "2", "3", "4", "5", "6", "7"]

    def test_controls_present(self) -> None:
        assert "ok" in F.FAULTS_BY_MODE  # baseline
        assert "idempotent" in F.FAULTS_BY_MODE  # the recommended fix

    def test_only_stale_seeds_a_concurrent_row(self) -> None:
        seeding = {f.mode for f in F.FAULTS if f.seed_concurrent}
        assert seeding == {"stale"}


class TestFaultDB:
    def test_idempotency_key_dedups(self) -> None:
        db = FaultDB()
        db.reset()
        db.add("p1", "Triage", "n", key="k1")
        db.add("p1", "Triage", "n", key="k1")
        assert len(db.snapshot()["records"]) == 1

    def test_no_key_appends(self) -> None:
        db = FaultDB()
        db.reset()
        db.add("p1", "Triage", "n")
        db.add("p1", "Triage", "n")
        assert len(db.snapshot()["records"]) == 2

    def test_overwrite_patient_drops_concurrent_row(self) -> None:
        db = FaultDB()
        db.reset(seed_concurrent=True)
        assert any(r["source"] == "other" for r in db.snapshot()["records"])
        db.add("p1", "Triage", "n", overwrite_patient=True)
        recs = db.snapshot()["records"]
        assert all(r["source"] != "other" for r in recs)


# -- end-to-end fault-injection tests ----------------------------------------


@pytest.fixture(scope="module")
def fault_server() -> Iterator[tuple[str, FaultDB]]:
    """A persistence-backed MockMed served for the whole module."""
    url, db, stop = fault_serve(port=0)
    yield url, db
    stop()


def _replay_fault(
    browser,
    bundle_dir: Path,
    base_url: str,
    fault_mode: Optional[str],
    run_dir: Path,
) -> RunReport:
    query = f"?fault={fault_mode}" if fault_mode else ""
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
    try:
        page.goto(f"{base_url}{query}")
        backend = PlaywrightBackend(page)
        # Floor: the fault-model suite characterizes runtime behavior under
        # API/persistence faults; pin the visual resolution path so outcomes
        # isolate the fault, not the rung (structural default is covered by
        # tests/e2e/test_structural_action.py).
        return Replayer(backend, use_structural=False).run(
            Workflow.load(bundle_dir),
            params=dict(PARAMS),
            bundle_dir=Path(bundle_dir),
            run_dir=Path(run_dir),
        )
    finally:
        page.close()


def test_off_state_pinned(fault_server, bundle, _browser, tmp_path) -> None:
    """With no ?fault query the app never calls the API: normal benchmark.

    The recorded save must still succeed (in-page), and the backend DB must
    stay EMPTY — proving the flag-gated hook is inert when off.
    """
    url, _db = fault_server
    requests.post(url + "api/reset", json={"seed_concurrent": False}, timeout=10)
    report = _replay_fault(_browser, bundle.dir, url, None, tmp_path)
    assert report.success, "off-state replay should succeed exactly as before"
    snap = requests.get(url + "api/db", timeout=10).json()
    assert snap["records"] == [], "no ?fault => the API must never be called"


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("ok", F.SUCCESS),
        ("partial", F.UNDETECTED_FAILURE),
        ("duplicate", F.WRONG_ACTION),
        ("timeout", F.FALSE_ABORT),
        ("optimistic", F.UNDETECTED_FAILURE),
        ("session", F.SAFE_HALT),
        ("stale", F.WRONG_ACTION),
        ("double", F.WRONG_ACTION),
        ("idempotent", F.SUCCESS),
    ],
)
def test_fault_outcome(
    mode, expected, fault_server, bundle, _browser, tmp_path
) -> None:
    """Each injected fault yields the documented ground-truth outcome."""
    url, _db = fault_server
    fault = F.FAULTS_BY_MODE[mode]
    requests.post(
        url + "api/reset",
        json={"seed_concurrent": fault.seed_concurrent},
        timeout=10,
    )
    report = _replay_fault(_browser, bundle.dir, url, mode, tmp_path)
    snap = requests.get(url + "api/db", timeout=10).json()
    outcome, reason = F.classify(
        report_success=bool(report.success),
        records=snap["records"],
        seeded_concurrent=fault.seed_concurrent,
    )
    assert outcome == expected, (
        f"fault={mode}: expected {expected}, got {outcome} "
        f"({reason}); report.success={report.success}, "
        f"db={snap['records']}"
    )


def test_duplicate_writes_two_rows_but_reports_success(
    fault_server, bundle, _browser, tmp_path
) -> None:
    """The headline: a real duplicate write behind a green report."""
    url, _db = fault_server
    requests.post(url + "api/reset", json={"seed_concurrent": False}, timeout=10)
    report = _replay_fault(_browser, bundle.dir, url, "duplicate", tmp_path)
    snap = requests.get(url + "api/db", timeout=10).json()
    assert report.success is True, "replay does not detect the duplicate"
    assert len(snap["records"]) == 2, "two encounter rows were actually written"


def test_optimistic_reports_success_over_empty_db(
    fault_server, bundle, _browser, tmp_path
) -> None:
    """The quietest failure: green report, nothing persisted."""
    url, _db = fault_server
    requests.post(url + "api/reset", json={"seed_concurrent": False}, timeout=10)
    report = _replay_fault(_browser, bundle.dir, url, "optimistic", tmp_path)
    snap = requests.get(url + "api/db", timeout=10).json()
    assert report.success is True, "replay reports a phantom success"
    assert snap["records"] == [], "nothing was persisted"
    assert snap["rejected_writes"] == 1, "the server did reject the write"
