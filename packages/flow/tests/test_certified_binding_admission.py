"""Certified REST ApiBinding synthesis: propose, admit, GUI fallback, halt.

Compile-time gate (``compiler.binding_admission``) for the ``api`` leaf of
the capability ladder (``docs/design/WORKFLOW_PROGRAM_IR.md`` §4.5, §9).

A REST binding is copied onto a consequential write only when the same
mined ``Effect`` CONFIRMs on a held-out MockMed fixture. A refused proposal
leaves the step on the GUI ladder. An admitted binding still HALTs on
indeterminate delivery -- never a second GUI write of a request that may
have landed.

No model calls. Live-local tests use the in-process MockMed fault server.
"""

from __future__ import annotations

import json
from pathlib import Path

from openadapt_flow.compiler.binding_admission import (
    API_WRITE_KEY,
    AdmissionFixture,
    admit_rest_binding,
    certify_step_rest_binding,
    certify_workflow_rest_bindings,
    propose_rest_binding,
)
from openadapt_flow.compiler.compile import compile_recording
from openadapt_flow.compiler.effect_mining import SOR_AFTER_KEY, SOR_BEFORE_KEY
from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.learning.library import SkillLibrary
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.actuators.api import ActuationStatus, ApiActuator
from openadapt_flow.runtime.effects import (
    EffectKind,
    EffectVerdict,
    RestRecordVerifier,
    Verdict,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer_api_actuator import (
    GuiWritingBackend,
    _dirs,
    _vision_that_confirms_saved,
)

NOTE = "charted via certified REST"
TARGET = {"patient_id": "p1", "type": "Triage"}


def _fault_server():
    url, db, stop = fault_serve()
    return url.rstrip("/"), db, stop


def _save_step(*, risk: str = "irreversible") -> Step:
    return Step(
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
    )


def _demo_record(**over) -> dict:
    rec = {
        "id": 1,
        "patient_id": "p1",
        "type": "Triage",
        "note": NOTE,
        "source": "replay",
        "key": None,
    }
    rec.update(over)
    return rec


def _derived_event(record=None) -> dict:
    return {
        "i": 0,
        "kind": "key",
        "key": "Enter",
        SOR_BEFORE_KEY: [],
        SOR_AFTER_KEY: [record or _demo_record()],
    }


def _fixture(url: str, **over) -> AdmissionFixture:
    kw = dict(
        base_url=url,
        params={"note": NOTE},
        timeout_s=2.0,
    )
    kw.update(over)
    return AdmissionFixture(**kw)


def _save_workflow(step: Step) -> Workflow:
    return Workflow(
        name="certified-rest-save",
        steps=[step],
        params={"note": NOTE},
    )


# -- propose ----------------------------------------------------------------


def test_propose_rest_binding_from_observed_sor_delta():
    """A derived SoR write becomes a REST POST, never fhir/mcp/tool."""
    step = _save_step()
    proposal = propose_rest_binding(
        step,
        _derived_event(),
        fixture=_fixture("http://held-out.example"),
        exclude_texts=(NOTE,),
        params={"note": NOTE},
    )
    assert proposal is not None
    binding = proposal.binding
    assert binding.kind == "rest"
    assert binding.method == "POST"
    assert binding.url_template == "/api/encounter"
    assert binding.body_template["patient_id"] == "p1"
    assert binding.body_template["type"] == "Triage"
    assert binding.body_template["note"] == "{note}"
    assert "id" not in binding.body_template
    assert "source" not in binding.body_template
    assert binding.effects
    assert binding.on_unavailable == "gui"


def test_propose_refuses_fhir_mcp_tool_captures():
    step = _save_step()
    for kind in ("fhir", "mcp", "tool"):
        event = _derived_event()
        event[API_WRITE_KEY] = {
            "kind": kind,
            "method": "POST",
            "url_template": "/api/encounter",
            "body": {"patient_id": "p1"},
        }
        assert (
            propose_rest_binding(
                step,
                event,
                fixture=_fixture("http://held-out.example"),
                exclude_texts=(NOTE,),
            )
            is None
        )


def test_propose_skips_placeholder_without_sor_delta():
    step = _save_step()
    proposal = propose_rest_binding(
        step,
        {"kind": "key", "key": "Enter"},
        fixture=_fixture("http://held-out.example"),
        exclude_texts=(NOTE,),
    )
    assert proposal is None
    assert step.api_binding is None


# -- admit ------------------------------------------------------------------


def test_admit_when_effect_confirms_on_held_out_mockmed(tmp_path):
    url, db, stop = _fault_server()
    try:
        step = _save_step()
        library = SkillLibrary(tmp_path / "skills")
        workflow = _save_workflow(step)
        decisions = certify_workflow_rest_bindings(
            workflow,
            [_derived_event()],
            fixture=_fixture(url),
            exclude_texts=(NOTE,),
            library_root=library.root,
            skill_id="mockmed-save",
        )
        decision = decisions[0]
        assert decision.admitted is True
        assert decision.actuation is not None
        assert decision.actuation.status is ActuationStatus.ACTUATED
        assert decision.verdicts
        assert all(v.confirmed for v in decision.verdicts)
        assert step.api_binding is not None
        assert step.api_binding.kind == "rest"
        assert step.api_binding.url_template == "/api/encounter"
        records = db.snapshot()["records"]
        assert len(records) == 1
        assert records[0]["note"] == NOTE
        # certify_workflow writes a new SkillLibrary instance; reload.
        skill = SkillLibrary(library.root).get("mockmed-save")
        active = skill.active()
        assert active is not None
        assert "certified REST" in active.provenance.note
        assert "save" in active.provenance.trace_ids
        assert active.validation_score == 1.0
    finally:
        stop()


# -- GUI ladder fallback ----------------------------------------------------


def test_refused_admission_leaves_gui_ladder(tmp_path):
    """A proposal that cannot CONFIRM is not copied onto the step.

    Replay then uses the GUI ladder exactly as a bundle with no binding does.
    """
    url, db, stop = _fault_server()
    try:
        step = _save_step()
        decision = certify_step_rest_binding(
            step,
            _derived_event(),
            fixture=_fixture(url, write_path="/api/no-such-write"),
            exclude_texts=(NOTE,),
            params={"note": NOTE},
        )
        assert decision.admitted is False
        assert step.api_binding is None
        assert db.snapshot()["records"] == []

        # Mining attached effects during propose. Keep RECORD_WRITTEN only:
        # GuiWritingBackend posts note="gui", which would refute a note
        # field_equals of the demonstrated payload. The GUI fallback claim
        # is that the write still happens through the ladder, not that the
        # certified API payload is reproduced.
        assert step.effects
        step.effects = [
            eff for eff in step.effects if eff.kind is EffectKind.RECORD_WRITTEN
        ]
        backend = GuiWritingBackend(url)
        bundle, run_dir = _dirs(tmp_path)
        report = Replayer(
            backend,
            vision=_vision_that_confirms_saved(),
            effect_verifier=RestRecordVerifier(url),
            poll_interval_s=0.01,
        ).run(_save_workflow(step), bundle_dir=bundle, run_dir=run_dir)

        assert report.success is True
        assert report.model_calls == 0
        assert backend.actions == [("press", "Enter")]
        records = db.snapshot()["records"]
        assert len(records) == 1
        assert records[0]["note"] == "gui"
    finally:
        stop()


# -- halt on indeterminate (no double-write) --------------------------------


class _IndeterminateVerifier(RestRecordVerifier):
    """Force INDETERMINATE after a reachable pre-state capture."""

    def verify(self, expected, before, context=None):
        return EffectVerdict(
            verdict=Verdict.INDETERMINATE,
            kind=expected.kind,
            substrate=self.substrate,
            reason="forced unreadable system of record",
            unavailable=True,
        )


def test_admitted_binding_halts_on_indeterminate_without_gui_retry(tmp_path):
    """An INDETERMINATE effect after an admitted API write HALTs.

    The GUI must not retry: the request may already have landed.
    """
    url, db, stop = _fault_server()
    try:
        step = _save_step()
        decision = certify_step_rest_binding(
            step,
            _derived_event(),
            fixture=_fixture(url),
            exclude_texts=(NOTE,),
            params={"note": NOTE},
        )
        assert decision.admitted is True
        db.reset()

        backend = GuiWritingBackend(url)
        bundle, run_dir = _dirs(tmp_path)
        report = Replayer(
            backend,
            vision=_vision_that_confirms_saved(),
            effect_verifier=_IndeterminateVerifier(url),
            api_actuator=ApiActuator(url),
            poll_interval_s=0.01,
        ).run(_save_workflow(step), bundle_dir=bundle, run_dir=run_dir)

        assert report.success is False
        result = report.results[0]
        assert result.actuation == "api"
        assert result.effect_verified is False
        assert backend.actions == []
        # API wrote once. GUI did not write a second row.
        assert len(db.snapshot()["records"]) == 1
    finally:
        stop()


def test_admit_requires_actuated_then_confirmed():
    """A before-send UNAVAILABLE (missing param) is not admission."""
    url, db, stop = _fault_server()
    try:
        step = _save_step()
        proposal = propose_rest_binding(
            step,
            _derived_event(),
            fixture=_fixture(url),
            exclude_texts=(NOTE,),
            params={"note": NOTE},
        )
        assert proposal is not None
        # Body template references {note}; omit it so the actuator cannot
        # build the request. Nothing is written; the step stays GUI-only.
        decision = admit_rest_binding(
            proposal,
            fixture=AdmissionFixture(base_url=url, params={}, timeout_s=0.5),
        )
        assert decision.admitted is False
        assert decision.actuation is not None
        assert decision.actuation.status is ActuationStatus.UNAVAILABLE
        assert step.api_binding is None
        assert db.snapshot()["records"] == []
    finally:
        stop()


# -- compile_recording wiring -----------------------------------------------


def _write_recording(tmp_path: Path, *, sor_after: list[dict]) -> Path:
    rec = tmp_path / "recording"
    (rec / "frames").mkdir(parents=True)
    events = [
        {"i": 0, "kind": "type", "text": NOTE, "param": "note", "t": 1.0},
        {
            "i": 1,
            "kind": "key",
            "key": "Enter",
            "t": 2.0,
            SOR_BEFORE_KEY: [],
            SOR_AFTER_KEY: sor_after,
        },
    ]
    (rec / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n"
    )
    (rec / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-certified-001",
                "created_at": "2026-08-28T00:00:00+00:00",
                "viewport": [1280, 800],
                "params": {"note": NOTE},
            }
        )
    )
    return rec


def test_compile_admits_rest_binding_on_held_out_fixture(tmp_path):
    url, db, stop = _fault_server()
    try:
        rec = _write_recording(tmp_path, sor_after=[_demo_record()])
        workflow = compile_recording(
            rec,
            tmp_path / "bundle",
            name="certified-demo",
            risk_overrides={"step_001": "irreversible"},
            admit_api_bindings=True,
            admission_fixture_url=url,
            skill_library_root=tmp_path / "skills",
        )
        save = workflow.steps[1]
        assert save.api_binding is not None
        assert save.api_binding.kind == "rest"
        assert save.api_binding.method == "POST"
        assert save.api_binding.url_template == "/api/encounter"
        assert save.api_binding.on_unavailable == "gui"
        assert save.effects
        assert workflow.steps[0].api_binding is None
        skill = SkillLibrary(tmp_path / "skills").get("certified-demo")
        active = skill.active()
        assert active is not None
        assert "certified REST" in active.provenance.note
        assert db.snapshot()["records"]
    finally:
        stop()


def test_compile_default_off_leaves_gui_ladder(tmp_path):
    rec = _write_recording(tmp_path, sor_after=[_demo_record()])
    workflow = compile_recording(
        rec,
        tmp_path / "bundle",
        name="certified-demo",
        risk_overrides={"step_001": "irreversible"},
    )
    assert all(step.api_binding is None for step in workflow.steps)


def test_compile_missing_fixture_url_leaves_gui_ladder(tmp_path):
    rec = _write_recording(tmp_path, sor_after=[_demo_record()])
    workflow = compile_recording(
        rec,
        tmp_path / "bundle",
        name="certified-demo",
        risk_overrides={"step_001": "irreversible"},
        admit_api_bindings=True,
    )
    assert all(step.api_binding is None for step in workflow.steps)
