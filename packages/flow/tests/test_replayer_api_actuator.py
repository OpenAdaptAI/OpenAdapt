"""Live-local tests for the API/tool actuator tier (top of the capability ladder).

These drive the REAL Replayer wired with a REAL
:class:`~openadapt_flow.runtime.actuators.ApiActuator` against a REAL system of
record (the in-process MockMed ``fault_server``), confirmed by the same
:class:`~openadapt_flow.runtime.effects.RestRecordVerifier` the GUI-write
effects tests use. No model calls, no network beyond localhost -- runs in CI.

The theses these pin (RFC ``docs/design/WORKFLOW_PROGRAM_IR.md`` section 4, the
``api`` implementation of a transition contract):

- a step with a REACHABLE ``ApiBinding`` performs its write via the API, the
  EffectVerifier CONFIRMS it against the record, and the GUI actuation is
  SKIPPED entirely -- ``$0``, zero model calls;
- an UNREACHABLE API falls through to the GUI ladder CLEANLY, with NO
  double-write (the request never left the client, so nothing was written);
- a step with NO binding replays byte-identically to today (back-compat);
- a REFUTED effect after an API write HALTS (the record, not the screen, is the
  oracle);
- an API write whose outcome is unknown / rejected HALTs (never GUI-retried).
"""

from __future__ import annotations

import requests
from openadapt_flow.runtime.actuators import ActuationStatus, ApiActuator
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    RestRecordVerifier,
)

# Reuse the scripted fakes from the main replayer unit tests (pytest's prepend
# import mode puts tests/ on sys.path).
from tests.test_replayer import FakeBackend, FakeVision, Match

from openadapt_flow.ir import (
    ActionKind,
    ApiBinding,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.replayer import Replayer

TARGET = {"patient_id": "p1", "type": "Triage"}


class GuiWritingBackend(FakeBackend):
    """A GUI backend whose ``press`` writes to the system of record.

    Models the CURRENT (GUI) path: the consequential keypress makes the app
    POST an encounter. Used to prove the API tier SKIPS the GUI (no press
    lands) on the actuated path, and to prove the fall-through path DOES
    GUI-write when the API tier is unavailable. ``record_presses`` records
    every ``press`` so a test can assert the GUI was or was not driven.
    """

    def __init__(self, sor_url, *, viewport=(300, 200)):
        super().__init__(viewport=viewport)
        self.sor_url = sor_url.rstrip("/")

    def press(self, key):
        super().press(key)
        requests.post(
            f"{self.sor_url}/api/encounter",
            json={"patient_id": "p1", "type": "Triage", "note": "gui"},
            timeout=5,
        )


def _fault_server():
    url, db, stop = fault_serve()
    return url.rstrip("/"), db, stop


def _api_save_workflow(
    *, url_template="/api/encounter", effects, risk="reversible", effects_on="step"
):
    """A one-step workflow: press Enter (the GUI action), but carrying an
    ApiBinding so the API tier performs the write instead. The screen
    postcondition PASSES -- the point is that the API tier bypasses it.

    ``effects_on`` places the effect contract on the ``"step"`` (the canonical
    location, verified on BOTH the API tier and a GUI fall-through) or on the
    ``"binding"`` (the self-contained-binding case, used by the API tier when
    the step declares none)."""
    step_effects = effects if effects_on == "step" else []
    binding_effects = effects if effects_on == "binding" else []
    return Workflow(
        name="api-save",
        steps=[
            Step(
                id="save",
                intent="save encounter",
                action=ActionKind.KEY,
                key="Enter",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                        timeout_s=0.2,
                    )
                ],
                risk=risk,
                effects=step_effects,
                api_binding=ApiBinding(
                    method="POST",
                    url_template=url_template,
                    body_template={
                        "patient_id": "p1",
                        "type": "Triage",
                        "note": "{note}",
                    },
                    effects=binding_effects,
                    timeout_s=2.0,
                ),
            )
        ],
        params={"note": "charted via API"},
    )


def _vision_that_confirms_saved():
    vision = FakeVision()
    vision.text_results = {
        "Saved": Match(point=(50, 10), region=(30, 5, 40, 10), confidence=0.9)
    }
    return vision


def _dirs(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    return bundle, tmp_path / "run"


def _record_written(**over):
    kw = dict(
        kind=EffectKind.RECORD_WRITTEN, match=TARGET, expected_count=1, timeout_s=2.0
    )
    kw.update(over)
    return Effect(**kw)


# -- ACTUATED + CONFIRMED: API performs the write, GUI is skipped -----------


def test_api_binding_actuates_and_confirms_skipping_gui(tmp_path):
    url, db, stop = _fault_server()
    try:
        backend = GuiWritingBackend(url)
        vision = _vision_that_confirms_saved()
        # The effect contract rides on the BINDING itself (self-contained) to
        # prove the API tier confirms even when the step declares no effects.
        workflow = _api_save_workflow(effects=[_record_written()], effects_on="binding")
        bundle, run_dir = _dirs(tmp_path)
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(url),
            api_actuator=ApiActuator(url),
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is True
        r = report.results[0]
        # The write landed via the API and was CONFIRMED against the record.
        assert r.actuation == "api"
        assert r.effect_verified is True
        assert any("CONFIRMED" in line for line in r.effect_results)
        assert any("actuated" in line for line in r.effect_results)
        # The GUI was SKIPPED: no click/type/press was ever issued.
        assert backend.actions == []
        # Exactly one record, written by the API with the run's param value.
        records = db.snapshot()["records"]
        assert len(records) == 1
        assert records[0]["note"] == "charted via API"
        # $0 guarantee: the API path makes no model calls.
        assert report.model_calls == 0
        assert report.est_model_cost_usd == 0.0
        # Audit: the run report counts the deterministic top-of-ladder tier.
        assert report.rung_counts.get("api") == 1
    finally:
        stop()


# -- UNREACHABLE API falls through to the GUI ladder cleanly (no double-write) --


def test_unreachable_api_falls_through_to_gui_no_double_write(tmp_path):
    url, db, stop = _fault_server()
    try:
        # The actuator points at a DEAD endpoint (connection refused): the
        # request is never sent, so the API tier is UNAVAILABLE and the step
        # falls through to the GUI, which performs the write exactly once.
        dead = "http://127.0.0.1:1"  # nothing listens here -> ConnectionError
        backend = GuiWritingBackend(url)
        vision = _vision_that_confirms_saved()
        workflow = _api_save_workflow(effects=[_record_written()])
        bundle, run_dir = _dirs(tmp_path)
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(url),
            api_actuator=ApiActuator(dead, timeout_s=1.0),
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is True
        r = report.results[0]
        # Fell through to the GUI: actuation is NOT "api"; the keypress fired.
        assert r.actuation is None
        assert ("press", "Enter") in backend.actions
        # An audit breadcrumb records the API tier was unavailable.
        assert any("unavailable" in line.lower() for line in r.effect_results)
        # The write happened EXACTLY ONCE (no double-write): the GUI wrote it,
        # the dead API did not, and the effect check CONFIRMS the single row.
        assert len(db.snapshot()["records"]) == 1
        assert r.effect_verified is True
        assert report.model_calls == 0
    finally:
        stop()


# -- REFUTED effect after an API write HALTS ---------------------------------


def test_api_write_refuted_by_record_halts(tmp_path):
    url, db, stop = _fault_server()
    try:
        # The API write lands ONE row, but the effect asserts a record for a
        # DIFFERENT patient (p2) -- the record refutes it (0 found, expected 1)
        # -> HALT, even though the API returned 2xx.
        backend = GuiWritingBackend(url)
        vision = _vision_that_confirms_saved()
        workflow = _api_save_workflow(
            effects=[_record_written(match={"patient_id": "p2", "type": "Triage"})]
        )
        bundle, run_dir = _dirs(tmp_path)
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(url),
            api_actuator=ApiActuator(url),
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is False
        r = report.results[0]
        assert r.actuation == "api"
        assert r.effect_verified is False
        assert r.ok is False
        assert "refuted" in (r.error or "").lower()
        assert "system of record" in (r.error or "")
        # The API DID write one (p1) row -- the write was performed; it is the
        # RECORD check that refused it, and the GUI never ran (no double-write).
        assert backend.actions == []
        assert len(db.snapshot()["records"]) == 1
    finally:
        stop()


# -- ATTEMPTED-but-rejected API write HALTS (never GUI-retried) --------------


def test_api_non_2xx_halts_never_double_writes(tmp_path):
    url, db, stop = _fault_server()
    try:
        # ?fault=session makes /api/encounter return 401: the request WAS sent
        # (nothing persisted here, but that is not knowable in general), so the
        # actuator must HALT rather than fall through and GUI-write a possible
        # duplicate.
        backend = GuiWritingBackend(url)
        vision = _vision_that_confirms_saved()
        workflow = _api_save_workflow(
            url_template="/api/encounter?fault=session", effects=[_record_written()]
        )
        bundle, run_dir = _dirs(tmp_path)
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(url),
            api_actuator=ApiActuator(url),
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is False
        r = report.results[0]
        assert r.actuation == "api"
        assert r.ok is False
        assert "halted" in (r.error or "").lower()
        # The GUI was NEVER driven -- the attempted API write is not re-done.
        assert backend.actions == []
        assert db.snapshot()["records"] == []
    finally:
        stop()


# -- FAIL-SAFE: an API binding with no effect to confirm the write -> HALT ----


def test_api_binding_without_effects_is_config_error_halt(tmp_path):
    url, db, stop = _fault_server()
    try:
        backend = GuiWritingBackend(url)
        vision = _vision_that_confirms_saved()
        # An ApiBinding with NO effects (neither on the step nor the binding):
        # the write could not be confirmed, so it must be refused BEFORE any
        # request is sent.
        workflow = _api_save_workflow(effects=[])
        bundle, run_dir = _dirs(tmp_path)
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(url),
            api_actuator=ApiActuator(url),
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is False
        r = report.results[0]
        assert r.ok is False
        assert "must be verifiable" in (r.error or "")
        # Nothing was written and the GUI never ran -- refused before actuating.
        assert backend.actions == []
        assert db.snapshot()["records"] == []
    finally:
        stop()


# -- FAIL-SAFE: an API binding but no EffectVerifier configured -> HALT -------


def test_api_binding_without_verifier_halts(tmp_path):
    url, db, stop = _fault_server()
    try:
        backend = GuiWritingBackend(url)
        vision = _vision_that_confirms_saved()
        workflow = _api_save_workflow(effects=[_record_written()])
        bundle, run_dir = _dirs(tmp_path)
        # An ApiActuator but NO EffectVerifier: an API write we cannot confirm.
        replayer = Replayer(
            backend,
            vision=vision,
            api_actuator=ApiActuator(url),
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is False
        r = report.results[0]
        assert r.ok is False
        assert "no EffectVerifier" in (r.error or "")
        assert backend.actions == []
        assert db.snapshot()["records"] == []
    finally:
        stop()


# -- BACK-COMPAT: a binding present but NO actuator configured -> GUI path ----


def test_binding_present_but_no_actuator_uses_gui_unchanged(tmp_path):
    url, db, stop = _fault_server()
    try:
        backend = GuiWritingBackend(url)
        vision = _vision_that_confirms_saved()
        workflow = _api_save_workflow(effects=[_record_written()])
        bundle, run_dir = _dirs(tmp_path)
        # NO api_actuator -> the API tier is OFF; the step actuates via the GUI
        # exactly as today (the binding is inert, its declared effects are still
        # verified by the normal GUI effect path).
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(url),
            poll_interval_s=0.01,
        )
        report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

        assert report.success is True
        r = report.results[0]
        assert r.actuation is None
        assert ("press", "Enter") in backend.actions
        assert r.effect_verified is True
        assert len(db.snapshot()["records"]) == 1
    finally:
        stop()


# -- BACK-COMPAT: a no-binding, no-effects bundle replays byte-identically ----


def test_no_binding_bundle_replays_unchanged(tmp_path):
    # A plain step with no api_binding and no effects: no API machinery engages,
    # the GUI runs, and the result carries no actuation marker.
    backend = FakeBackend()
    vision = _vision_that_confirms_saved()
    workflow = Workflow(
        name="plain",
        steps=[
            Step(
                id="save",
                intent="save",
                action=ActionKind.KEY,
                key="Enter",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                        timeout_s=0.2,
                    )
                ],
            )
        ],
    )
    bundle, run_dir = _dirs(tmp_path)
    replayer = Replayer(
        backend,
        vision=vision,
        api_actuator=ApiActuator("http://127.0.0.1:1"),  # present but unused
        poll_interval_s=0.01,
    )
    report = replayer.run(workflow, bundle_dir=bundle, run_dir=run_dir)

    assert report.success is True
    r = report.results[0]
    assert r.actuation is None
    assert r.effect_verified is None
    assert r.effect_results == []
    assert ("press", "Enter") in backend.actions


# -- Unit: the actuator's fail-safe classification (no double-write contract) --


def test_actuator_unavailable_on_connection_refused():
    binding = ApiBinding(
        url_template="http://127.0.0.1:1/api/encounter",
        body_template={"patient_id": "p1"},
        timeout_s=1.0,
    )
    res = ApiActuator().actuate(binding, {})
    assert res.status is ActuationStatus.UNAVAILABLE
    assert res.should_fall_through is True


def test_actuator_unavailable_on_missing_param():
    # A URL/body that references a param the run did not supply cannot be built
    # -> UNAVAILABLE (before-send, nothing written, safe to fall through).
    binding = ApiBinding(
        url_template="/api/encounter", body_template={"note": "{missing}"}
    )
    res = ApiActuator("http://127.0.0.1:9").actuate(binding, {})
    assert res.status is ActuationStatus.UNAVAILABLE
    assert "missing" in res.reason


def test_actuator_actuated_on_2xx():
    url, db, stop = _fault_server()
    try:
        binding = ApiBinding(
            url_template="/api/encounter",
            body_template={"patient_id": "p1", "type": "Triage", "note": "n"},
            timeout_s=2.0,
        )
        res = ApiActuator(url).actuate(binding, {})
        assert res.status is ActuationStatus.ACTUATED
        assert res.http_status == 200
        assert len(db.snapshot()["records"]) == 1
    finally:
        stop()


def test_actuator_halts_on_non_2xx():
    url, _db, stop = _fault_server()
    try:
        binding = ApiBinding(
            url_template="/api/encounter?fault=session",
            body_template={"patient_id": "p1"},
            timeout_s=2.0,
        )
        res = ApiActuator(url).actuate(binding, {})
        assert res.status is ActuationStatus.HALT
        assert res.http_status == 401
        assert res.should_halt is True
    finally:
        stop()
