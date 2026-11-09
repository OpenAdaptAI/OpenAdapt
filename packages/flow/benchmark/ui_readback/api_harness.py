"""Separate-session UI read-back on the end-to-end fault list (API writes).

Reuses the ``benchmark/effect_e2e`` record service and its independent ground
truth (a read-only connection to the SQLite file, its own classifier). Each
write goes through the real replayer's API tier, as in ``effect_e2e``. The
only thing that changes between arms is where the check reads:

- ``same_screen``  the app's own echo of the save (``GET /api/ui/last-save``),
                   what the screen that typed would show. Tier 4.
- ``ui_readback``  ``effects.kind: ui_readback``: a separate browser, a new
                   context, its own sign-in, the record page by identifier,
                   fields read from the page structure. Tier 2.
- ``api_read``     the REST read of the encounters table. Tier 1.

All arms judge the same two effect contracts as ``effect_e2e``: exactly one
new Triage encounter for p1 with no pre-existing row lost, and its note equal
to the input.

Two scenario sets:

- the fault list: ``effect_e2e``'s scenarios plus ``page_cache``, a phantom
  write whose record page renders from the app's own write echo (a record page
  served from a cache the failed write populated);
- display probes: correct saves whose record page DISPLAYS differently
  (shortened text, a lagging replica, a redesigned layout, a page that posts
  an audit beacon, a reader whose password is wrong). These measure false
  stops, which only the UI read-back can have here.
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from benchmark.effect_e2e import ground_truth
from benchmark.effect_e2e.record_service import (
    TARGET_PATIENT,
    TARGET_TYPE,
    serve,
)
from benchmark.effect_e2e.run import _NullBackend, _NullVision
from openadapt_flow.ir import (
    ActionKind,
    ApiBinding,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.mockmed.chart_pages import (
    READER_PASSWORD,
    READER_USERNAME,
    chart_readback_spec,
)
from openadapt_flow.runtime.actuators import ApiActuator
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    RestRecordVerifier,
    ValueExpr,
)
from openadapt_flow.runtime.effects.ui_readback import (
    UiReadbackSpec,
    build_ui_readback_verifier,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.verification import VerificationTier

NOTE = "End-to-end SWER benchmark triage note"
ACTUATOR_TIMEOUT_S = 0.6
EFFECT_TIMEOUT_S = 0.3
NAVIGATION_TIMEOUT_S = 5.0

ARMS = ("same_screen", "ui_readback", "api_read")


@dataclass(frozen=True)
class Scenario:
    name: str
    fault: str
    seed_concurrent: bool
    blurb: str
    chart_source: str = "encounters"
    display: dict[str, Any] = field(default_factory=dict)
    note: str = NOTE
    reader_attribute: Optional[str] = None
    effect_timeout_s: float = EFFECT_TIMEOUT_S
    reader_password: str = READER_PASSWORD
    #: Whether the service answers the write with 2xx, so the run itself has
    #: no signal that anything went wrong and only a record check can stop it.
    write_accepted: bool = True


FAULTS: tuple[Scenario, ...] = (
    Scenario("ok", "ok", False, "clean accepted write (control)"),
    Scenario("no_persist", "no_persist", False, "2xx, nothing persisted"),
    Scenario("partial", "partial", False, "row saved, note dropped"),
    Scenario("duplicate", "duplicate", False, "the row landed twice"),
    Scenario("wrong_record", "wrong_record", False, "the row landed on p2"),
    Scenario("stale", "stale", True, "last-write-wins removed another row"),
    Scenario(
        "collateral_unaudited",
        "collateral_unaudited",
        False,
        "target row correct, stray row on the billing table",
    ),
    Scenario(
        "page_cache",
        "no_persist",
        False,
        "nothing persisted; the record page renders the app's write echo",
        chart_source="write_cache",
    ),
    Scenario(
        "optimistic", "optimistic", False, "server rejected (409)", write_accepted=False
    ),
    Scenario(
        "session", "session", False, "session expired (401)", write_accepted=False
    ),
    Scenario(
        "timeout",
        "timeout",
        False,
        "committed, then the client timed out",
        write_accepted=False,
    ),
)

PROBES: tuple[Scenario, ...] = (
    Scenario("baseline", "ok", False, "record page as declared"),
    Scenario(
        "long_note_visible_text",
        "ok",
        False,
        "page shortens the note to 20 characters; reader reads the visible text",
        display={"truncate_at": 20},
    ),
    Scenario(
        "long_note_title_attribute",
        "ok",
        False,
        "page shortens the note; reader reads the full note from the title",
        display={"truncate_at": 20},
        reader_attribute="title",
    ),
    Scenario(
        "irregular_whitespace",
        "ok",
        False,
        "input has repeated and trailing spaces; HTML collapses them",
        note="End-to-end   SWER  benchmark triage note ",
    ),
    Scenario(
        "replica_lag_within_timeout",
        "ok",
        False,
        "page shows a new row 1 s late; the effect allows 3 s",
        display={"lag_s": 1.0},
        effect_timeout_s=3.0,
    ),
    Scenario(
        "replica_lag_beyond_timeout",
        "ok",
        False,
        "page shows a new row 1 s late; the effect allows 0.3 s",
        display={"lag_s": 1.0},
    ),
    Scenario(
        "layout_redesign",
        "ok",
        False,
        "row and cell classes renamed; declared selectors no longer match",
        display={"markup_version": 2},
    ),
    Scenario(
        "page_posts_audit_beacon",
        "ok",
        False,
        "record page POSTs a 'viewed' beacon on load",
        display={"view_beacon": True},
    ),
    Scenario(
        "reader_password_wrong",
        "ok",
        False,
        "the read-back account's password is wrong",
        reader_password="not-the-password",
    ),
)


def _effects(timeout_s: float) -> list[Effect]:
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
            probe="exactly one target encounter",
            timeout_s=timeout_s,
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=match,
            field="note",
            value=ValueExpr(param="note"),
            risk="irreversible",
            probe="the note read-back",
            timeout_s=timeout_s,
        ),
    ]


def _workflow(scenario: Scenario) -> Workflow:
    effects = _effects(scenario.effect_timeout_s)
    return Workflow(
        name=f"ui-readback-{scenario.name}",
        steps=[
            Step(
                id="save",
                intent="save encounter",
                action=ActionKind.KEY,
                key="Enter",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT, text="Saved", timeout_s=0.1
                    )
                ],
                risk="irreversible",
                effects=effects,
                api_binding=ApiBinding(
                    method="POST",
                    url_template=f"/api/encounter?fault={scenario.fault}",
                    body_template={
                        "patient_id": TARGET_PATIENT,
                        "type": TARGET_TYPE,
                        "note": "{note}",
                    },
                    effects=[e.model_copy(deep=True) for e in effects],
                    timeout_s=ACTUATOR_TIMEOUT_S,
                ),
            )
        ],
        params={"note": scenario.note},
    )


class _AppEchoVerifier(RestRecordVerifier):
    """The app's own echo of the save: what the screen that typed shows.

    Read over HTTP so the arm runs through the same verifier machinery, but
    labeled for what it is (tier 4), not as the REST system-of-record read.
    """

    substrate = "app-echo"
    verification_tier = VerificationTier.IMMEDIATE_SCREEN
    independent_system_of_record = False


def _verifier(arm: str, scenario: Scenario, base_url: str) -> Any:
    if arm == "same_screen":
        return _AppEchoVerifier(
            base_url,
            records_path="/api/ui/last-save",
            records_key="records",
            timeout_s=EFFECT_TIMEOUT_S,
            poll_interval_s=0.02,
        )
    if arm == "api_read":
        return RestRecordVerifier(
            base_url,
            records_path="/api/records",
            records_key="records",
            timeout_s=EFFECT_TIMEOUT_S,
            poll_interval_s=0.02,
        )
    spec = chart_readback_spec(base_url, note_attribute=scenario.reader_attribute)
    spec["navigation_timeout_s"] = NAVIGATION_TIMEOUT_S
    env = {
        "CHART_READER_USERNAME": READER_USERNAME,
        "CHART_READER_PASSWORD": scenario.reader_password,
    }
    return build_ui_readback_verifier(
        UiReadbackSpec.model_validate(spec),
        {"patient_id": TARGET_PATIENT},
        timeout_s=EFFECT_TIMEOUT_S,
        poll_interval_s=0.1,
        environ=env,
    )


def _stopped_by(report: Any) -> Optional[str]:
    """``check`` when an effect verdict stopped the run, ``run`` otherwise."""

    if report.success:
        return None
    step = report.results[0] if report.results else None
    if step is None:
        return "run"
    if step.effect_results and step.effect_verified is False:
        return "check"
    return "run"


def run_one(
    handle: Any, arm: str, scenario: Scenario, *, work_root: Path, index: int
) -> dict[str, Any]:
    base_url = handle.base_url
    requests.post(
        f"{base_url}/api/reset",
        json={"seed_concurrent": scenario.seed_concurrent},
        timeout=5.0,
    )
    handle.store.chart_source = scenario.chart_source
    handle.chart.reset_display(**scenario.display)
    beacons_before = handle.chart.beacons_received
    before_gt = ground_truth.capture(handle.db_path)

    workflow = _workflow(scenario)
    verifier = _verifier(arm, scenario, base_url)
    run_dir = work_root / f"api-{arm}-{scenario.name}-{index}"
    (run_dir / "bundle" / "templates").mkdir(parents=True, exist_ok=True)
    replayer = Replayer(
        _NullBackend(),
        vision=_NullVision(),
        effect_verifier=verifier,
        api_actuator=ApiActuator(base_url, timeout_s=ACTUATOR_TIMEOUT_S),
        poll_interval_s=0.01,
    )
    started = time.monotonic()
    report = replayer.run(
        workflow, bundle_dir=run_dir / "bundle", run_dir=run_dir / "out"
    )
    elapsed = time.monotonic() - started
    after_gt = ground_truth.capture(handle.db_path)
    gt = ground_truth.judge(before_gt, after_gt, intended_note=scenario.note)
    step = report.results[0] if report.results else None
    reported_success = bool(report.success)
    return {
        "harness": "api_write",
        "arm": arm,
        "scenario": scenario.name,
        "blurb": scenario.blurb,
        "write_accepted": scenario.write_accepted,
        "probe": scenario in PROBES,
        "i": index,
        "reported_success": reported_success,
        "halted": not reported_success,
        "stopped_by": _stopped_by(report),
        "effect_results": list(step.effect_results) if step else [],
        "evidence_tiers": sorted(
            {e.verification_tier for e in step.effect_evidence if e.verification_tier}
        )
        if step
        else [],
        "transaction_outcome": report.transaction_outcome,
        "gt_correct": gt.correct,
        "gt_fault": gt.fault_class,
        "beacons_sent_to_server": handle.chart.beacons_received - beacons_before,
        "silent_wrong": (not gt.correct) and reported_success,
        "caught": (not gt.correct) and not reported_success,
        "false_stop": gt.correct and not reported_success,
        "elapsed_s": round(elapsed, 2),
    }


def run(
    n: int = 1,
    *,
    scenarios: tuple[Scenario, ...] = FAULTS + PROBES,
    arms: tuple[str, ...] = ARMS,
    log: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="ui-readback-api-") as tmp:
        tmp_path = Path(tmp)
        handle = serve(tmp_path / "record.db")
        try:
            for scenario in scenarios:
                for arm in arms:
                    for i in range(n):
                        rows.append(
                            run_one(handle, arm, scenario, work_root=tmp_path, index=i)
                        )
                    last = rows[-1]
                    log(
                        f"api  {scenario.name:28s} {arm:12s} gt={last['gt_fault']:16s} "
                        f"halted={last['halted']!s:5s} by={last['stopped_by']}"
                    )
        finally:
            handle.stop()
    return rows
