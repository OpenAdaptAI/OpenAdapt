"""Lending (MockLoan) transactional fault-model - end-to-end harness tests.

The non-healthcare counterpart of ``tests/e2e/test_fault_model.py``. It replays
the SAME compiled disbursement-authorize bundle through the REAL ``Replayer``
against a persistence-backed MockLoan under each injected transactional fault
and asserts the ground-truth outcome (judged by ``GET /api/db``, never the
screen), plus:

- a pin that the flag-gated ``?fault=`` hook is inert with no query (the normal
  benchmark is byte-for-byte unaffected), and
- a resolution-ladder check: under a template-breaking ``?drift=theme`` the
  DEFAULT (full-ladder) replayer recovers the clean write model-free, while the
  template-only rung halts before the consequential write (no wrong money
  movement).

To respect the "only ONE sync Playwright per thread" constraint (see the
``_browser`` note in ``conftest.py``), the whole browser study runs inside a
SINGLE module-scoped ``sync_playwright`` context (exactly like
``benchmark/lending_fault_model/run.py``); the individual tests then assert
cheaply against its collected results.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
import requests

from benchmark.lending_fault_model import faults as F
from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.compiler import compile_recording
from openadapt_flow.demo_driver import record_disbursement_demo
from openadapt_flow.ir import Workflow
from openadapt_flow.mockloan.fault_server import serve as fault_serve
from openadapt_flow.runtime import Replayer

from .conftest import VIEWPORT

pytestmark = pytest.mark.timeout(900)

PARAMS = {"memo": F.MEMO_TEXT}


def _replay(browser, bundle_dir, base_url, query, run_dir, *, use_structural):
    page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
    try:
        page.goto(f"{base_url}{query}")
        return Replayer(PlaywrightBackend(page), use_structural=use_structural).run(
            Workflow.load(bundle_dir),
            params=dict(PARAMS),
            bundle_dir=Path(bundle_dir),
            run_dir=Path(run_dir),
        )
    finally:
        page.close()


@pytest.fixture(scope="module")
def study(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    """Run the whole MockLoan browser study once inside ONE Playwright context.

    Returns a dict with per-fault ``(report_success, records, outcome)`` under
    the screen-only (template-isolated) replayer, the off-state pin, and the
    resolution-ladder outcomes.
    """
    from playwright.sync_api import sync_playwright

    url, _db, stop = fault_serve(port=0)
    runs = tmp_path_factory.mktemp("ml_e2e_runs")
    out: dict = {"faults": {}, "ladder": {}}
    try:
        rec = record_disbursement_demo(
            url, tmp_path_factory.mktemp("ml_rec"), memo_text=F.MEMO_TEXT
        )
        bundle = tmp_path_factory.mktemp("ml_bundle")
        compile_recording(rec, bundle, name="disburse-demo")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                # Off-state pin: no ?fault => the API is never called.
                requests.post(
                    url + "api/reset", json={"seed_concurrent": False}, timeout=10
                )
                rep = _replay(
                    browser, bundle, url, "", runs / "off", use_structural=False
                )
                snap = requests.get(url + "api/db", timeout=10).json()
                out["off_state"] = (bool(rep.success), snap["records"])

                # Every fault, judged by the independent ledger.
                for fault in F.FAULTS:
                    requests.post(
                        url + "api/reset",
                        json={"seed_concurrent": fault.seed_concurrent},
                        timeout=10,
                    )
                    rep = _replay(
                        browser,
                        bundle,
                        url,
                        f"?fault={fault.mode}",
                        runs / fault.mode,
                        use_structural=False,
                    )
                    snap = requests.get(url + "api/db", timeout=10).json()
                    outcome, reason = F.classify(
                        report_success=bool(rep.success),
                        records=snap.get("records", []),
                        seeded_concurrent=fault.seed_concurrent,
                    )
                    out["faults"][fault.mode] = {
                        "report_success": bool(rep.success),
                        "outcome": outcome,
                        "reason": reason,
                        "records": snap.get("records", []),
                    }

                # Resolution ladder under a template-breaking cosmetic drift.
                for cfg, use_structural in (("full", True), ("tpl", False)):
                    requests.post(
                        url + "api/reset",
                        json={"seed_concurrent": False},
                        timeout=10,
                    )
                    rep = _replay(
                        browser,
                        bundle,
                        url,
                        "?fault=ok&drift=theme",
                        runs / f"ladder_{cfg}",
                        use_structural=use_structural,
                    )
                    snap = requests.get(url + "api/db", timeout=10).json()
                    out["ladder"][cfg] = {
                        "report_success": bool(rep.success),
                        "records": snap.get("records", []),
                    }
            finally:
                browser.close()
    finally:
        stop()
    yield out


def test_off_state_pinned(study: dict) -> None:
    """With no ?fault query the app never calls the API: normal benchmark."""
    success, records = study["off_state"]
    assert success, "off-state replay should succeed exactly as before"
    assert records == [], "no ?fault => the API must never be called"


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
def test_fault_outcome(study: dict, mode: str, expected: str) -> None:
    """Each injected fault produces its documented ground-truth outcome."""
    got = study["faults"][mode]
    assert got["outcome"] == expected, f"{mode}: {got['outcome']} ({got['reason']})"


def test_silently_mishandled_classes(study: dict) -> None:
    """The screen-only replay silently mishandles the dangerous write faults."""
    silent_modes = ["partial", "duplicate", "optimistic", "stale", "double"]
    caught = [
        m
        for m in silent_modes
        if F.is_silently_mishandled(
            study["faults"][m]["outcome"], study["faults"][m]["report_success"]
        )
    ]
    assert set(caught) == set(silent_modes), caught


def test_five_of_seven_transactional_classes_silent(study: dict) -> None:
    """The headline: 5 of the 7 transactional classes are silently mishandled."""
    transactional = [f.mode for f in F.FAULTS if f.fault_class[0].isdigit()]
    silent = [
        m
        for m in transactional
        if F.is_silently_mishandled(
            study["faults"][m]["outcome"], study["faults"][m]["report_success"]
        )
    ]
    assert len(silent) == 5, silent


def test_resolution_ladder_recovers_theme_drift(study: dict) -> None:
    """Full ladder recovers a template-breaking drift model-free; the
    template-only rung never books a wrong disbursement."""
    full = study["ladder"]["full"]
    assert full["report_success"], "full ladder should recover theme drift"
    assert len(full["records"]) == 1
    assert full["records"][0]["memo"] == F.MEMO_TEXT

    tpl = study["ladder"]["tpl"]
    if tpl["report_success"]:
        assert len(tpl["records"]) == 1
        assert tpl["records"][0]["memo"] == F.MEMO_TEXT
    else:
        assert tpl["records"] == [], "a halt must leave no wrong disbursement"


def _row(memo: str = F.MEMO_TEXT, source: str = "replay", **kw) -> dict:
    base = {
        "id": 1,
        "loan_id": F.TARGET_LOAN,
        "product": F.TARGET_PRODUCT,
        "amount": F.TARGET_AMOUNT,
        "memo": memo,
        "source": source,
        "key": None,
    }
    base.update(kw)
    return base


class TestClassifyPure:
    """Pure taxonomy sanity (no browser) - mirrors the clinical study."""

    def test_success(self) -> None:
        outcome, _ = F.classify(
            report_success=True, records=[_row()], seeded_concurrent=False
        )
        assert outcome == F.SUCCESS

    def test_duplicate_is_wrong_action(self) -> None:
        outcome, _ = F.classify(
            report_success=True,
            records=[_row(id=1), _row(id=2)],
            seeded_concurrent=False,
        )
        assert outcome == F.WRONG_ACTION

    def test_phantom_is_undetected(self) -> None:
        outcome, _ = F.classify(
            report_success=True, records=[], seeded_concurrent=False
        )
        assert outcome == F.UNDETECTED_FAILURE
