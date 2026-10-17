"""Separate-session UI read-back on the GUI fault list (real browser replay).

Records the canonical MockMed triage demo once, compiles it, and replays the
same bundle in a real headless browser against the MockMed fault server
(``benchmark/fault_model``'s persistence boundary). The Save step gets the same
two effect contracts in every arm. Arms differ only in where the check reads:

- ``none``         no record check; the replay's own screen postconditions
                   (the ``fault_model`` study's baseline).
- ``same_screen``  the encounter list on the page the replay typed on, after
                   the save (:class:`ActingScreenVerifier`). Tier 4.
- ``ui_readback``  ``effects.kind: ui_readback``: its own browser, a new
                   context, its own sign-in, ``/chart/patient/p1``. Tier 2.
- ``api_read``     ``GET /api/db``, the fault server's store. Tier 1.

Ground truth is the fault server's in-process store, judged before and after
each replay by ``benchmark/effect_e2e/ground_truth.judge`` (its own
classifier). The ``api_read`` arm reads that same store over HTTP, so on this
harness the API arm is close to definitional; the API-write harness
(``api_harness.py``) has a ground truth that bypasses the service.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from benchmark.effect_e2e import ground_truth
from openadapt_flow.ir import Workflow
from openadapt_flow.mockmed.chart_pages import (
    READER_PASSWORD,
    READER_USERNAME,
    chart_readback_spec,
)
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    RestRecordVerifier,
    ValueExpr,
)
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.ui_readback import (
    UiReadbackSpec,
    build_ui_readback_verifier,
)
from openadapt_flow.verification import VerificationTier

NOTE = "E2E triage booking three months"
TARGET_PATIENT = "p1"
TARGET_TYPE = "Triage"
EFFECT_TIMEOUT_S = 1.0
VIEWPORT = {"width": 1280, "height": 800}

ARMS = ("none", "same_screen", "ui_readback", "api_read")


@dataclass(frozen=True)
class GuiScenario:
    name: str
    fault: str
    seed_concurrent: bool
    blurb: str
    #: Whether the app returns 2xx for the write (only a check can catch a
    #: wrong result then).
    write_accepted: bool = True


SCENARIOS: tuple[GuiScenario, ...] = (
    GuiScenario("ok", "ok", False, "clean accepted write (control)"),
    GuiScenario("idempotent", "idempotent", False, "sent twice, de-duplicated by key"),
    GuiScenario("partial", "partial", False, "row saved, note dropped"),
    GuiScenario(
        "optimistic", "optimistic", False, "UI paints success, server rejects (409)"
    ),
    GuiScenario("duplicate", "duplicate", False, "save submitted twice, two rows"),
    GuiScenario("double", "double", False, "save click delivered twice, two rows"),
    GuiScenario("stale", "stale", True, "last-write-wins removed another row"),
    GuiScenario("wrong_record", "wrong_record", False, "the row landed on p2"),
    GuiScenario("no_persist", "no_persist", False, "200, nothing persisted"),
    GuiScenario(
        "session", "session", False, "401; app returns to sign-in", write_accepted=False
    ),
    GuiScenario(
        "timeout", "timeout", False, "committed, then the client aborted", False
    ),
)


class ActingScreenVerifier:
    """Read the saved encounters off the page the replay typed on.

    Benchmark-only. It reads the MockMed patient page's encounter list in the
    acting session after the save, which is what a same-screen check sees.
    It has no pre-action view of the record (the form is on screen then), so
    it cannot detect a lost update or count only new rows.
    """

    substrate = "acting-screen"
    verification_tier = VerificationTier.IMMEDIATE_SCREEN
    independent_system_of_record = False

    def __init__(self, page: Any) -> None:
        self._page = page

    @staticmethod
    def requires_readable_pre_state_for(effect: Effect) -> bool:
        return False

    def capture_pre_state(self, context: Any = None) -> EffectState:
        return EffectState(substrate=self.substrate, reachable=False, records=[])

    def _read(self) -> Optional[list[dict[str, Any]]]:
        try:
            banner = self._page.inner_text("#patient-banner", timeout=500)
            items = self._page.locator("#encounter-list li.enc-item").all_inner_texts()
        except Exception:  # noqa: BLE001 - not on the patient page
            return None
        found = re.search(r"MRN\s+(\S+)", banner)
        if not found:
            return None
        patient = found.group(1).lower()
        rows = []
        for item in items:
            kind, _, note = item.partition(" — ")
            rows.append(
                {"patient_id": patient, "type": kind.strip(), "note": note.strip()}
            )
        return rows

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        deadline = time.monotonic() + max(0.0, expected.timeout_s)
        while True:
            verdict = judge_records(
                expected, before, self._read(), substrate=self.substrate
            )
            if verdict.confirmed or time.monotonic() >= deadline:
                return verdict
            time.sleep(0.1)


def _effects() -> list[Effect]:
    match = {
        "patient_id": ValueExpr(literal=TARGET_PATIENT),
        "type": ValueExpr(literal=TARGET_TYPE),
    }
    return [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match=match,
            expected_count=1,
            forbid_collateral_loss=True,
            risk="irreversible",
            timeout_s=EFFECT_TIMEOUT_S,
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=match,
            field="note",
            value=ValueExpr(param="note"),
            risk="irreversible",
            timeout_s=EFFECT_TIMEOUT_S,
        ),
    ]


def _with_save_effects(workflow: Workflow) -> Workflow:
    """Attach the effect contracts to the compiled Save step."""

    copy = workflow.model_copy(deep=True)
    saves = [s for s in copy.steps if "save" in s.intent.lower()]
    if len(saves) != 1:
        raise RuntimeError("expected exactly one Save step in the triage bundle")
    saves[0].effects = _effects()
    saves[0].risk = "irreversible"
    return copy


def _snapshot(db: Any) -> ground_truth.Snapshot:
    rows = [dict(r) for r in db.snapshot()["records"]]
    return ground_truth.Snapshot(encounters=rows, counts={"encounters": len(rows)})


def _verifier(arm: str, base_url: str, page: Any) -> Any:
    if arm == "none":
        return None
    if arm == "same_screen":
        return ActingScreenVerifier(page)
    if arm == "api_read":
        return RestRecordVerifier(
            base_url.rstrip("/"),
            records_path="/api/db",
            records_key="records",
            timeout_s=EFFECT_TIMEOUT_S,
            poll_interval_s=0.05,
        )
    spec = chart_readback_spec(base_url)
    spec["navigation_timeout_s"] = 5.0
    return build_ui_readback_verifier(
        UiReadbackSpec.model_validate(spec),
        {"patient_id": TARGET_PATIENT},
        timeout_s=EFFECT_TIMEOUT_S,
        poll_interval_s=0.1,
        environ={
            "CHART_READER_USERNAME": READER_USERNAME,
            "CHART_READER_PASSWORD": READER_PASSWORD,
        },
    )


def _stopped_by(report: Any) -> Optional[str]:
    if report.success:
        return None
    for result in report.results:
        if result.effect_results and result.effect_verified is False:
            return "check"
    return "run"


def replay_once(
    browser: Any,
    workflow: Workflow,
    bundle_dir: Path,
    base_url: str,
    db: Any,
    arm: str,
    scenario: GuiScenario,
    run_dir: Path,
    index: int,
) -> dict[str, Any]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime import Replayer

    requests.post(
        base_url + "api/reset",
        json={"seed_concurrent": scenario.seed_concurrent},
        timeout=10,
    )
    before = _snapshot(db)
    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
    page = context.new_page()
    report = None
    t0 = time.monotonic()
    try:
        page.goto(f"{base_url}?fault={scenario.fault}")
        backend = PlaywrightBackend(page)
        runnable = workflow if arm == "none" else _with_save_effects(workflow)
        replayer = Replayer(backend, effect_verifier=_verifier(arm, base_url, page))
        report = replayer.run(
            runnable,
            params={"note": NOTE},
            bundle_dir=bundle_dir,
            run_dir=run_dir,
        )
    finally:
        context.close()
    elapsed = time.monotonic() - t0
    # The duplicate and timeout faults can land a row after the replay ends.
    time.sleep(0.2 if scenario.fault != "timeout" else 3.5)
    gt = ground_truth.judge(before, _snapshot(db), intended_note=NOTE)
    reported_success = bool(report.success) if report else False
    save = next(
        (r for r in (report.results if report else []) if "save" in r.intent.lower()),
        None,
    )
    return {
        "harness": "gui_replay",
        "arm": arm,
        "scenario": scenario.name,
        "blurb": scenario.blurb,
        "write_accepted": scenario.write_accepted,
        "probe": False,
        "i": index,
        "reported_success": reported_success,
        "halted": not reported_success,
        "stopped_by": _stopped_by(report) if report else "run",
        "effect_results": list(save.effect_results) if save else [],
        "evidence_tiers": sorted(
            {e.verification_tier for e in save.effect_evidence if e.verification_tier}
        )
        if save
        else [],
        "transaction_outcome": report.transaction_outcome if report else None,
        "gt_correct": gt.correct,
        "gt_fault": gt.fault_class,
        "silent_wrong": (not gt.correct) and reported_success,
        "caught": (not gt.correct) and not reported_success,
        "false_stop": gt.correct and not reported_success,
        "elapsed_s": round(elapsed, 2),
    }


def run(
    n: int = 1,
    *,
    scenarios: tuple[GuiScenario, ...] = SCENARIOS,
    arms: tuple[str, ...] = ARMS,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    from playwright.sync_api import sync_playwright

    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.demo_driver import record_triage_demo

    os.environ.pop("ANTHROPIC_API_KEY", None)
    rows: list[dict[str, Any]] = []
    url, db, stop = fault_serve(port=0)
    try:
        with tempfile.TemporaryDirectory(prefix="ui-readback-gui-") as tmp:
            tmp_path = Path(tmp)
            log(f"gui  recording the triage demo against {url}")
            recording = record_triage_demo(url, tmp_path / "recording", note_text=NOTE)
            bundle_dir = tmp_path / "bundle"
            workflow = compile_recording(recording, bundle_dir, name="triage-demo")
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    for scenario in scenarios:
                        for arm in arms:
                            for i in range(n):
                                rows.append(
                                    replay_once(
                                        browser,
                                        workflow,
                                        bundle_dir,
                                        url,
                                        db,
                                        arm,
                                        scenario,
                                        tmp_path
                                        / "runs"
                                        / f"{scenario.name}-{arm}-{i}",
                                        i,
                                    )
                                )
                            last = rows[-1]
                            log(
                                f"gui  {scenario.name:13s} {arm:12s} "
                                f"gt={last['gt_fault']:16s} "
                                f"halted={last['halted']!s:5s} by={last['stopped_by']} "
                                f"({last['elapsed_s']}s)"
                            )
                finally:
                    browser.close()
    finally:
        stop()
    return rows
