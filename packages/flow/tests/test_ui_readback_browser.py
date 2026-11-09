"""The separate-session UI read-back in a real browser.

Runs ``effects.kind: ui_readback`` against MockMed's server-rendered record
pages (``openadapt_flow/mockmed/chart_pages.py``) served by the fault server,
and once through the real replayer. Localhost only, no model calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest
import requests

pytest.importorskip("playwright.sync_api")

from benchmark.effect_e2e.run import _NullBackend, _NullVision  # noqa: E402
from openadapt_flow.ir import ActionKind, ApiBinding, Step, Workflow  # noqa: E402
from openadapt_flow.mockmed.chart_pages import (  # noqa: E402
    READER_PASSWORD,
    READER_USERNAME,
    chart_readback_spec,
)
from openadapt_flow.mockmed.fault_server import serve  # noqa: E402
from openadapt_flow.runtime.actuators import ApiActuator  # noqa: E402
from openadapt_flow.runtime.effects import (  # noqa: E402
    Effect,
    EffectKind,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.effects.ui_readback import (  # noqa: E402
    UiReadbackSpec,
    build_ui_readback_verifier,
)
from openadapt_flow.runtime.replayer import Replayer  # noqa: E402

NOTE = "chest pain, follow up in two weeks"
MATCH = {"patient_id": "p1", "type": "Triage"}


@pytest.fixture()
def server() -> Iterator[tuple[str, Any]]:
    url, db, stop = serve()
    try:
        yield url, db
    finally:
        stop()


def _verifier(
    url: str,
    *,
    password: str = READER_PASSWORD,
    navigation_timeout_s: float = 5.0,
    **spec_kw: Any,
) -> Any:
    spec = chart_readback_spec(url, **spec_kw)
    spec["navigation_timeout_s"] = navigation_timeout_s
    return build_ui_readback_verifier(
        UiReadbackSpec.model_validate(spec),
        {"patient_id": "p1"},
        timeout_s=1.0,
        poll_interval_s=0.05,
        environ={
            "CHART_READER_USERNAME": READER_USERNAME,
            "CHART_READER_PASSWORD": password,
        },
    )


def _effects(timeout_s: float = 0.5) -> list[Effect]:
    return [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match=MATCH,
            expected_count=1,
            forbid_collateral_loss=True,
            risk="irreversible",
            timeout_s=timeout_s,
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=MATCH,
            field="note",
            value=ValueExpr(literal=NOTE),
            risk="irreversible",
            timeout_s=timeout_s,
        ),
    ]


def _save(url: str, fault: str = "ok") -> None:
    requests.post(
        f"{url}api/encounter?fault={fault}",
        json={"patient_id": "p1", "type": "Triage", "note": NOTE},
        timeout=10,
    )


def test_reads_a_save_in_its_own_signed_in_session(server: Any) -> None:
    from playwright.sync_api import sync_playwright

    url, db = server
    requests.post(url + "api/reset", json={"seed_concurrent": True}, timeout=5)
    # The "acting" browser signs in to the chart first. The read-back must
    # not reuse that session: it signs in again in its own browser.
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        acting = browser.new_page()
        acting.goto(url + "chart/signin")
        acting.fill("#chart-username", READER_USERNAME)
        acting.fill("#chart-password", READER_PASSWORD)
        acting.click("#chart-signin")
        acting.wait_for_selector("#chart-signed-in")
        signed_in_before = db.chart.sign_ins
        verifier = _verifier(url)
        before = verifier.capture_pre_state()
        assert before.reachable is True
        assert before.detail["fresh_context"] is True
        assert [r["type"] for r in before.records] == ["Consult"]
        _save(url)
        for effect in _effects():
            assert verifier.verify(effect, before).verdict is Verdict.CONFIRMED
        browser.close()
    # One sign-in for the pre-state read and one per checked effect.
    assert db.chart.sign_ins - signed_in_before == 3


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("partial", Verdict.REFUTED),
        ("stale", Verdict.REFUTED),
        ("wrong_record", Verdict.REFUTED),
        ("no_persist", Verdict.REFUTED),
    ],
)
def test_wrong_saves_are_refuted(server: Any, fault: str, expected: Verdict) -> None:
    url, _db = server
    requests.post(
        url + "api/reset", json={"seed_concurrent": fault == "stale"}, timeout=5
    )
    verifier = _verifier(url)
    before = verifier.capture_pre_state()
    _save(url, fault)
    verdicts = [verifier.verify(effect, before) for effect in _effects(0.3)]
    assert any(v.verdict is expected for v in verdicts)
    if fault == "partial":
        # The note check fails, and its reason names what was read: the
        # application's record page, never a system of record.
        refuted = verdicts[1]
        assert refuted.verdict is Verdict.REFUTED
        assert "system of record" not in refuted.reason
        assert "record page" in refuted.reason


def test_a_wrong_reader_password_cannot_read(server: Any) -> None:
    url, _db = server
    verifier = _verifier(url, password="wrong", navigation_timeout_s=1.5)
    probe = verifier.test_connection()
    assert probe.ok is False
    assert "wrong" not in probe.reason
    before = verifier.capture_pre_state()
    assert before.reachable is False
    verdict = verifier.verify(_effects()[0], before)
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.unavailable is True
    assert "wrong" not in verdict.reason


@pytest.mark.parametrize("transport", ["fetch", "beacon", "xhr", "form"])
def test_the_session_sends_no_write_after_sign_in(server: Any, transport: str) -> None:
    url, db = server
    requests.post(url + "api/reset", json={}, timeout=5)
    _save(url)
    db.chart.reset_display(view_beacon=True, beacon_transport=transport)
    verifier = _verifier(url)
    before = verifier.capture_pre_state()
    assert before.reachable is False
    assert "blocked" in before.detail["reason"]
    verdict = verifier.verify(_effects()[0], before)
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "blocked" in verdict.reason
    assert db.chart.beacons_received == 0


def test_display_changes_stop_rather_than_pass(server: Any) -> None:
    url, db = server
    requests.post(url + "api/reset", json={}, timeout=5)
    verifier = _verifier(url)
    before = verifier.capture_pre_state()
    _save(url)

    db.chart.reset_display(markup_version=2)
    redesigned = verifier.verify(_effects()[0], before)
    assert redesigned.verdict is Verdict.INDETERMINATE

    db.chart.reset_display(truncate_at=10)
    shortened = verifier.verify(_effects()[1], before)
    assert shortened.verdict is Verdict.INDETERMINATE
    full_text = _verifier(url, note_attribute="title").verify(_effects()[1], before)
    assert full_text.verdict is Verdict.CONFIRMED

    db.chart.reset_display(lag_s=0.6)
    lagging = verifier.verify(_effects(timeout_s=3.0)[0], before)
    assert lagging.verdict is Verdict.CONFIRMED


def _api_workflow(fault: str) -> Workflow:
    effects = _effects()
    return Workflow(
        name=f"ui-readback-{fault}",
        steps=[
            Step(
                id="save",
                intent="save encounter",
                action=ActionKind.KEY,
                key="Enter",
                risk="irreversible",
                effects=effects,
                api_binding=ApiBinding(
                    method="POST",
                    url_template=f"/api/encounter?fault={fault}",
                    body_template={"patient_id": "p1", "type": "Triage", "note": NOTE},
                    effects=[e.model_copy(deep=True) for e in effects],
                    timeout_s=2.0,
                ),
            )
        ],
    )


@pytest.mark.parametrize("fault", ["ok", "partial"])
def test_through_the_replayer(server: Any, tmp_path: Path, fault: str) -> None:
    url, _db = server
    requests.post(url + "api/reset", json={}, timeout=5)
    (tmp_path / "bundle" / "templates").mkdir(parents=True)
    replayer = Replayer(
        _NullBackend(),
        vision=_NullVision(),
        effect_verifier=_verifier(url),
        api_actuator=ApiActuator(url.rstrip("/"), timeout_s=2.0),
        poll_interval_s=0.01,
    )
    report = replayer.run(
        _api_workflow(fault),
        bundle_dir=tmp_path / "bundle",
        run_dir=tmp_path / "run",
    )
    step = report.results[0]
    if fault == "ok":
        assert report.success is True
        assert {e.verification_tier for e in step.effect_evidence} == {2}
        assert {e.substrate for e in step.effect_evidence} == {"ui-readback"}
    else:
        assert report.success is False
        assert any("[ui-readback]" in line for line in step.effect_results)
        assert "system of record" not in (step.error or "")
        assert "record page read in a separate session" in (step.error or "")
