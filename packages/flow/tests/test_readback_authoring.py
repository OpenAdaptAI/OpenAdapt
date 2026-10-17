"""Authoring a UI read-back by demonstration ("Show how to check the save").

MockMed's encounter flow, on fake backends (no browser, no model calls): the
person types a note, saves, then shows the check: open the patient list,
search the patient, open the encounter, and mark the note and the visit type.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from openadapt_flow.approval_mode.coverage import save_coverage
from openadapt_flow.authoring import AuthoringError, AuthoringSession
from openadapt_flow.compiler.compile import compile_recording
from openadapt_flow.compiler.readback_authoring import (
    CHECK_PROMPT,
    CheckError,
    MarkedField,
    apply_check,
    load_check,
    write_check,
)
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.effects.effect import ReadbackNav, ReadbackSpec
from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.verification import VerificationTier
from tests.test_replayer import FakeBackend, FakeVision, Match

NOTE = "chest pain follow up in two weeks"
VISIT = "Triage"
NOTE_BOX = (100, 200, 300, 40)
VISIT_BOX = (100, 260, 200, 30)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (250, 250, 250)).save(buf, format="PNG")
    return buf.getvalue()


def _anchor(x: int, y: int) -> Anchor:
    return Anchor(
        template="templates/t.png", region=(x - 5, y - 5, 10, 10), click_point=(x, y)
    )


def _workflow() -> Workflow:
    """A compiled MockMed encounter save followed by the person's check."""
    return Workflow(
        name="mockmed-encounter",
        params={"note": NOTE, "visit": VISIT, "patient": "Ada Park"},
        steps=[
            Step(
                id="step_000",
                intent="type note",
                action=ActionKind.TYPE,
                param="note",
                text=NOTE,
            ),
            Step(
                id="step_001",
                intent="type visit",
                action=ActionKind.TYPE,
                param="visit",
                text=VISIT,
            ),
            Step(
                id="step_002",
                intent="click 'Save'",
                action=ActionKind.KEY,
                key="Enter",
                risk="irreversible",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT, text="Saved", timeout_s=0.2
                    )
                ],
            ),
            Step(
                id="step_003",
                intent="click 'Patients'",
                action=ActionKind.CLICK,
                anchor=_anchor(10, 10),
            ),
            Step(
                id="step_004",
                intent="type patient",
                action=ActionKind.TYPE,
                param="patient",
                text="Ada Park",
            ),
            Step(
                id="step_005",
                intent="click 'Open encounter'",
                action=ActionKind.CLICK,
                anchor=_anchor(50, 60),
            ),
        ],
    )


def _recording(
    tmp_path: Path, *, fields=None, start_event: int = 3, n_events: int = 6
) -> Path:
    rec = tmp_path / "rec"
    (rec / "frames").mkdir(parents=True)
    (rec / "events.jsonl").write_text(
        "\n".join(json.dumps({"i": i, "kind": "click"}) for i in range(n_events)) + "\n"
    )
    (rec / "frames" / f"{n_events - 1:04d}_after.png").write_bytes(_png())
    write_check(
        rec,
        start_event=start_event,
        fields=fields
        or [
            MarkedField(param="note", region=NOTE_BOX),
            MarkedField(param="visit", region=VISIT_BOX, locator="#visit-type"),
        ],
    )
    return rec


def _marked_screen(shows: dict):
    """OCR of the marked screen: each region shows its text."""

    def ocr(png, *, region=None):
        text = shows.get(tuple(region) if region else None)
        return [SimpleNamespace(text=text, confidence=0.95)] if text else []

    return SimpleNamespace(ocr=ocr)


SHOWS = {NOTE_BOX: f"Note: {NOTE}", VISIT_BOX: VISIT}


# -- compiling the check ---------------------------------------------------------


def test_check_becomes_the_saves_readback_and_leaves_the_task(tmp_path):
    rec = _recording(tmp_path)
    updated, outcome = apply_check(_workflow(), rec, vision=_marked_screen(SHOWS))

    assert [s.id for s in updated.steps] == ["step_000", "step_001", "step_002"]
    assert outcome.removed_steps == ["step_003", "step_004", "step_005"]
    assert outcome.fields == ["note", "visit"]
    assert outcome.different_path and outcome.tier == "persisted_state_reacquisition"
    (effect,) = updated.steps[2].effects
    spec = effect.readback
    assert spec is not None and spec.different_path
    assert [n.action for n in spec.renavigation] == ["click", "type", "click"]
    # The search replays this run's patient, not the demonstration's.
    assert spec.renavigation[1].param == "patient" and spec.renavigation[1].text is None
    assert [f.value.param for f in spec.fields] == ["note", "visit"]
    assert spec.fields[1].locator == "#visit-type"
    assert OnScreenReadbackVerifier.verification_tier_for(effect) is (
        VerificationTier.PERSISTED_STATE_REACQUISITION
    )


def test_marking_on_the_saves_own_screen_is_only_a_screen_check(tmp_path):
    wf = _workflow().model_copy(update={"steps": _workflow().steps[:3]})
    rec = _recording(tmp_path, start_event=3, n_events=3)
    updated, outcome = apply_check(wf, rec, vision=_marked_screen(SHOWS))
    assert not outcome.different_path and outcome.tier == "immediate_screen"
    effect = updated.steps[2].effects[0]
    assert OnScreenReadbackVerifier.verification_tier_for(effect) is (
        VerificationTier.IMMEDIATE_SCREEN
    )


def test_refuses_a_box_that_does_not_show_the_typed_value(tmp_path):
    rec = _recording(tmp_path)
    with pytest.raises(CheckError, match="doesn't show the value typed for it"):
        apply_check(_workflow(), rec, vision=_marked_screen({NOTE_BOX: "Allergies"}))


def test_refuses_a_field_that_was_not_typed(tmp_path):
    rec = _recording(tmp_path, fields=[MarkedField(param="dob", region=NOTE_BOX)])
    with pytest.raises(CheckError, match="isn't a value typed"):
        apply_check(_workflow(), rec, vision=_marked_screen(SHOWS))


def test_refuses_a_check_with_no_save_before_it(tmp_path):
    rec = _recording(tmp_path, start_event=0)
    with pytest.raises(CheckError, match="nothing before the check saves"):
        apply_check(_workflow(), rec, vision=_marked_screen(SHOWS))


def test_refuses_a_check_that_types_a_secret(tmp_path):
    wf = _workflow().model_copy(update={"secret_params": ["patient"]})
    rec = _recording(tmp_path)
    with pytest.raises(CheckError, match="secret"):
        apply_check(wf, rec, vision=_marked_screen(SHOWS))


# -- running it ------------------------------------------------------------------


class RecordBackend(FakeBackend):
    """MockMed record truth behind the screen.

    Enter saves the typed values (or nothing, for a phantom save). The check's
    navigation re-opens the record, so the screen then shows what was saved.
    """

    def __init__(self, *, phantom=False, saved_visit=None):
        super().__init__(viewport=(400, 300))
        self.phantom = phantom
        self.saved_visit = saved_visit
        self.typed: list[str] = []
        self.record: dict = {}
        self.view: dict = {"note": "editing", "visit": "editing"}

    def type_text(self, text):
        super().type_text(text)
        self.typed.append(text)

    def press(self, key):
        super().press(key)
        if key == "Enter" and not self.phantom:
            note, visit = self.typed[0], self.typed[1]
            self.record = {"note": note, "visit": self.saved_visit or visit}

    def click(self, x, y, *, double=False):
        super().click(x, y, double=double)
        self.view = dict(self.record)


def _screen_of(backend: RecordBackend):
    def ocr(png, *, region=None):
        key = {NOTE_BOX: "note", VISIT_BOX: "visit"}.get(
            tuple(region) if region else None
        )
        text = backend.view.get(key) if key else None
        return [SimpleNamespace(text=text, confidence=0.95)] if text else []

    return ocr


def _run(tmp_path, monkeypatch, backend, params):
    import openadapt_flow.vision as vision_mod

    updated, _ = apply_check(
        _workflow(), _recording(tmp_path), vision=_marked_screen(SHOWS)
    )
    monkeypatch.setattr(vision_mod, "ocr", _screen_of(backend))
    vision = FakeVision()
    vision.text_results = {
        "Saved": Match(point=(5, 5), region=(0, 0, 10, 10), confidence=0.9)
    }
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True, exist_ok=True)
    return Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        updated, params=params, bundle_dir=bundle, run_dir=tmp_path / "run"
    )


RUN = {"note": "knee pain recheck", "visit": "Follow-up", "patient": "Ben Cho"}


def test_a_real_save_reads_back_every_field_with_this_runs_values(
    tmp_path, monkeypatch
):
    backend = RecordBackend()
    report = _run(tmp_path, monkeypatch, backend, RUN)
    assert report.success, report
    assert report.results[-1].effect_verified is True
    # The check searched for this run's patient.
    assert "Ben Cho" in backend.typed
    assert report.model_calls == 0


def test_a_phantom_save_halts(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch, RecordBackend(phantom=True), RUN)
    assert not report.success
    assert report.results[-1].effect_verified is False


def test_one_wrong_field_halts(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch, RecordBackend(saved_visit="Triage"), RUN)
    assert not report.success
    assert any("REFUTED" in line for line in report.results[-1].effect_results)


# -- approval and old contracts --------------------------------------------------


def test_problems_only_still_sends_it_to_a_person(tmp_path):
    updated, _ = apply_check(
        _workflow(), _recording(tmp_path), vision=_marked_screen(SHOWS)
    )
    coverage = save_coverage(updated, updated.steps[2])
    assert "onscreen_readback" in coverage.problems
    # The marked fields count as reading the typed inputs back.
    assert "note" not in coverage.uncovered_inputs
    assert "visit" not in coverage.uncovered_inputs


def test_contracts_without_marked_fields_serialize_as_before():
    spec = ReadbackSpec(
        region=(1, 2, 3, 4),
        different_path=True,
        renavigation=[ReadbackNav(action="click", point=(1, 1))],
    )
    dumped = spec.model_dump(mode="json")
    assert "fields" not in dumped
    assert "param" not in dumped["renavigation"][0]


# -- the authoring session -------------------------------------------------------


class ScriptedBackend:
    def __init__(self):
        self.calls = []

    @property
    def viewport(self):
        return (400, 300)

    def screenshot(self):
        return _png()

    def click(self, x, y, *, double=False):
        self.calls.append(("click", x, y))

    def type_text(self, text):
        self.calls.append(("type", text))

    def press(self, key):
        self.calls.append(("press", key))


def _session(tmp_path):
    return AuthoringSession(
        ScriptedBackend(),
        tmp_path / "rec",
        backend_kind="web",
        settle_interval_s=0.01,
        settle_stable_frames=1,
        settle_timeout_s=0.2,
    )


def test_session_prompts_and_stores_the_marks(tmp_path):
    session = _session(tmp_path)
    session.start_record()
    session.click(20, 20)
    session.type_text(NOTE, param="note")
    session.press("Enter")
    assert session.begin_check() == {"prompt": CHECK_PROMPT, "checking": True}
    session.click(10, 10)
    session.click(50, 60)
    session.mark_field(
        param="note", backend_pixels={"x": 100, "y": 200, "w": 300, "h": 40}
    )
    rec = session.stop_record()

    marks = load_check(rec)
    assert marks is not None and marks.start_event == 3
    assert marks.fields[0].region == NOTE_BOX


def test_session_refuses_a_check_with_no_marks(tmp_path):
    session = _session(tmp_path)
    session.start_record()
    session.press("Enter")
    session.begin_check()
    session.click(10, 10)
    with pytest.raises(AuthoringError) as err:
        session.compile(tmp_path / "bundle")
    assert err.value.code == "check_unmarked"


def test_mark_before_begin_check_is_refused(tmp_path):
    session = _session(tmp_path)
    session.start_record()
    with pytest.raises(AuthoringError) as err:
        session.mark_field(
            param="note", backend_pixels={"x": 1, "y": 1, "w": 5, "h": 5}
        )
    assert err.value.code == "no_check"


def test_compile_recording_applies_the_check(tmp_path, monkeypatch):
    import openadapt_flow.vision as vision_mod

    session = _session(tmp_path)
    session.start_record()
    session.click(20, 20)
    session.type_text(NOTE, param="note")
    session.press("Enter")
    session.begin_check()
    session.click(10, 10)
    session.click(50, 60)
    session.mark_field(
        param="note", backend_pixels={"x": 100, "y": 200, "w": 300, "h": 40}
    )
    rec = session.stop_record()

    monkeypatch.setattr(vision_mod, "ocr", _marked_screen({NOTE_BOX: NOTE}).ocr)
    wf = compile_recording(
        rec,
        tmp_path / "bundle",
        name="check",
        risk_overrides={"step_002": "irreversible"},
    )
    assert [s.id for s in wf.steps] == ["step_000", "step_001", "step_002"]
    effect = wf.steps[2].effects[-1]
    assert effect.readback is not None and effect.readback.different_path
    assert Workflow.load(tmp_path / "bundle").steps[2].effects[-1].readback.fields
