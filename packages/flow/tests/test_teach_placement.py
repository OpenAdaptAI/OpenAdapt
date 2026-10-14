"""teach on a real bundle: the recorded target is carried, and the fix lands
where the dialog appeared (B036).

Two gaps kept ``openadapt-flow teach`` from repairing a real bundle:

1. A fix RECORDING compiled to steps with real anchors, but only their intents
   reached the inducer, which then spliced a placeholder target
   (``templates/x.png``) that a real bundle doesn't have.
2. The dismiss step was always spliced BEFORE the step the run stopped on. When
   that step had already acted and the dialog opened in response to it, the
   guard was unmet at that point, the dismiss step was skipped, and the run
   stopped at the same place. The fix now goes after a step that acted: as a
   retry of the step when the operator's fix repeats it, or as a check of the
   step's expected screen when it does not.

These tests drive the real ``Replayer`` with the Phase-2 FakeBackend and a
scripted vision, run ``teach`` through the CLI, and replay what it wrote.
Deterministic and ``$0``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from openadapt_flow.__main__ import main
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Guard,
    Postcondition,
    PostconditionKind,
    Predicate,
    PredicateKind,
    ProgramGraph,
    RunReport,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)
from openadapt_flow.learning.gate import program_regression_gate
from openadapt_flow.learning.interpreter import program_reproduces
from openadapt_flow.learning.synth_stream import StructuralDiffInducer
from openadapt_flow.learning.teach import _choose_guard_fact
from openadapt_flow.learning.trace import ExecutionTrace, TraceStep
from openadapt_flow.runtime.effects.effect import Effect, EffectKind
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.vision.ocr import OcrLine
from tests.test_halt_learn_loop import INTENT_DISMISS as MODAL_ONCE_DISMISS
from tests.test_halt_learn_loop import MODAL_FACT as MODAL_ONCE_FACT
from tests.test_halt_learn_loop import _base_workflow as modal_once_workflow
from tests.test_halt_learn_loop import _replay as modal_once_replay
from tests.test_replayer import FakeBackend, FakeVision, Match, make_png

SKILL = "open-referral-form"
INTENT_OPEN = "click 'Open form'"
INTENT_FIELD = "click 'Field'"
INTENT_DISMISS = "click 'Dismiss'"
MODAL = "Quick survey"
OTHER_MODAL = "Session Timeout"
FORM_READY = "Form ready"

OPEN_REGION = (380, 280, 60, 40)
OPEN_POINT = (410, 300)
FIELD_REGION = (600, 400, 40, 20)
FIELD_POINT = (620, 410)
DISMISS_POINT = (100, 100)


def _program(*, open_risk: str = "reversible", effects: bool = False) -> ProgramGraph:
    """Open the form, then click a field in it. Clicking Open is expected to
    show the form; under drift a survey opens instead."""
    open_step = Step(
        id="s_open",
        intent=INTENT_OPEN,
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/open.png",
            region=OPEN_REGION,
            click_point=OPEN_POINT,
        ),
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT, text=FORM_READY, timeout_s=0.1
            )
        ],
        risk=open_risk,  # type: ignore[arg-type]
        effects=(
            [Effect(kind=EffectKind.RECORD_WRITTEN, match={"type": "form"})]
            if effects
            else []
        ),
        timeout_s=0.1,
    )
    field_step = Step(
        id="s_field",
        intent=INTENT_FIELD,
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/field.png",
            region=FIELD_REGION,
            click_point=FIELD_POINT,
        ),
        timeout_s=0.1,
    )
    return ProgramGraph(
        entry="s_open",
        states={
            "s_open": State(
                id="s_open",
                kind=StateKind.ACTION,
                step=open_step,
                transitions=[Transition(target="s_field")],
            ),
            "s_field": State(
                id="s_field",
                kind=StateKind.ACTION,
                step=field_step,
                transitions=[Transition(target="__end__")],
            ),
            "__end__": State(id="__end__", kind=StateKind.TERMINAL, outcome="success"),
        },
    )


class SurveyAfterOpenVision(FakeVision):
    """The first click on Open opens a survey (``modal``). With ``retry_needed``
    the survey stopped the click from taking effect: after dismissing it, Open
    must be clicked again. Without it the form opened under the survey and is
    ready once the survey is dismissed. ``modal=None`` is a clean run."""

    def __init__(
        self, backend: FakeBackend, *, modal: Optional[str], retry_needed: bool
    ) -> None:
        super().__init__()
        self.backend = backend
        self.modal = modal
        self.retry_needed = retry_needed

    def _clicks_at(self, point: tuple[int, int]) -> int:
        return sum(
            1
            for a in self.backend.actions
            if a[0] == "click"
            and abs(a[1] - point[0]) <= 15
            and abs(a[2] - point[1]) <= 15
        )

    def _modal_up(self) -> bool:
        return (
            self.modal is not None
            and self._clicks_at(OPEN_POINT) >= 1
            and self._clicks_at(DISMISS_POINT) == 0
        )

    def _form_ready(self) -> bool:
        opens = self._clicks_at(OPEN_POINT)
        if self.modal is None:
            return opens >= 1
        needed = 2 if self.retry_needed else 1
        return opens >= needed and self._clicks_at(DISMISS_POINT) >= 1

    def find_template(
        self,
        screen_png,
        template_png,
        *,
        search_region=None,
        prefer_near=None,
        scales=(0.85, 1.0, 1.18),
        threshold=0.82,
    ):
        self.template_calls.append(search_region)
        near = prefer_near or (0, 0)
        if abs(near[0] - OPEN_REGION[0]) <= 5 and abs(near[1] - OPEN_REGION[1]) <= 5:
            if self._modal_up():
                return None
            return Match(point=OPEN_POINT, region=OPEN_REGION, confidence=0.95)
        if abs(near[0] - FIELD_REGION[0]) <= 5 and abs(near[1] - FIELD_REGION[1]) <= 5:
            if not self._form_ready():
                return None
            return Match(point=FIELD_POINT, region=FIELD_REGION, confidence=0.95)
        if self._modal_up():
            return Match(point=DISMISS_POINT, region=(90, 90, 20, 20), confidence=0.95)
        return None

    def _visible(self, text: str) -> bool:
        if text == FORM_READY:
            return self._form_ready()
        return self._modal_up() and text == self.modal

    def find_text(self, screen_png, text, *, region=None, min_ratio=0.8, **_):
        self.text_calls.append(text)
        if self._visible(text):
            return Match(point=(50, 10), region=(30, 5, 160, 16), confidence=0.9)
        return None

    def ocr(self, screen_png, *, region=None):
        if self._modal_up():
            return [OcrLine(text=self.modal, region=(30, 5, 160, 16), confidence=0.9)]
        return []


def _replay(
    workflow: Workflow,
    *,
    modal: Optional[str],
    bundle: Path,
    run_dir: Path,
    retry_needed: bool = True,
) -> tuple[RunReport, FakeBackend]:
    backend = FakeBackend(frame=make_png((1280, 800)), viewport=(1280, 800))
    vision = SurveyAfterOpenVision(backend, modal=modal, retry_needed=retry_needed)
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    return report, backend


def _bundle(
    root: Path, *, open_risk: str = "reversible", effects: bool = False
) -> Path:
    """A base bundle with real crops for its own steps and NO placeholder crop."""
    bundle = root / f"base_bundle_{open_risk}_{int(effects)}"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "open.png").write_bytes(make_png((60, 40)))
    (bundle / "templates" / "field.png").write_bytes(make_png((40, 20)))
    program = _program(open_risk=open_risk, effects=effects)
    Workflow(name=SKILL, program=program).save(bundle)
    return bundle


#: (label, button box) drawn on the fix recording's frames.
DISMISS_BUTTON = ("Dismiss", (60, 84, 220, 132))
OPEN_BUTTON = ("Open form", (340, 276, 500, 324))


def _fix_recording(path: Path, *clicks: tuple[str, tuple[int, int, int, int]]) -> Path:
    """A recording of the operator's fix: one click per button, in order,
    compiled later through the ordinary ``compile_recording`` path."""
    (path / "frames").mkdir(parents=True)
    remaining = list(clicks)
    events = []
    for i, (_label, box) in enumerate(clicks):
        for tag, shown in (("before", remaining), ("after", remaining[1:])):
            img = np.full((800, 1280, 3), 255, np.uint8)
            for text, (x0, y0, x1, y1) in shown:
                cv2.rectangle(img, (x0, y0), (x1, y1), (40, 40, 40), -1)
                cv2.putText(
                    img,
                    text,
                    (x0 + 12, y0 + 32),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (255, 255, 255),
                    2,
                )
            ok, buf = cv2.imencode(".png", img)
            assert ok
            (path / "frames" / f"{i:04d}_{tag}.png").write_bytes(buf.tobytes())
        x0, y0, x1, y1 = box
        events.append(
            {"i": i, "kind": "click", "x": (x0 + x1) // 2, "y": (y0 + y1) // 2}
            | {"t": float(i + 1)}
        )
        remaining = remaining[1:]
    (path / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (path / "meta.json").write_text(
        json.dumps(
            {
                "id": "rec-fix-001",
                "created_at": "2026-10-14T00:00:00+00:00",
                "viewport": [1280, 800],
                "app_url": "http://localhost:0/",
                "params": {},
            }
        )
    )
    return path


def _halted_run(bundle: Path, run_dir: Path, *, retry_needed: bool = True) -> RunReport:
    report, backend = _replay(
        Workflow.load(bundle),
        modal=MODAL,
        bundle=bundle,
        run_dir=run_dir,
        retry_needed=retry_needed,
    )
    assert report.terminal_outcome == "halt"
    # Open was clicked; the survey opened instead of the form.
    assert backend.actions == [("click", *OPEN_POINT, False)]
    assert report.halt is not None and report.halt.intent == INTENT_OPEN
    return report


def _teach(run_dir: Path, fix: Path, bundle: Path, out: Path) -> int:
    return main(
        ["teach", str(run_dir), "--fix", str(fix), "--bundle", str(bundle)]
        + ["--out", str(out)]
    )


# -- the step goes after a delivered step, which is retried -------------------


def test_teach_places_the_fix_after_a_delivered_step_and_retries_it(tmp_path):
    """The operator dismissed the survey and clicked Open again: the learned
    program does the same after Open fails."""
    bundle = _bundle(tmp_path)
    run_dir = tmp_path / "run"
    _halted_run(bundle, run_dir)
    fix = _fix_recording(tmp_path / "fix", DISMISS_BUTTON, OPEN_BUTTON)
    out = tmp_path / "taught"

    assert _teach(run_dir, fix, bundle, out) == 0

    program = Workflow.load(out).program
    assert program is not None
    open_state = program.states["s_open"]
    handler = program.states[open_state.on_exception or ""]
    assert handler.step is not None and handler.step.guard is not None
    # The handler runs only after Open failed, and halts unless the survey shows.
    assert handler.step.guard.on_unmet == "halt"
    assert handler.step.guard.predicate.text == MODAL
    retry = program.states[handler.transitions[0].target]
    assert retry.step is not None and retry.step.intent == INTENT_OPEN
    assert retry.transitions[0].target == "s_field"
    # The normal path is unchanged.
    assert open_state.transitions[0].target == "s_field"

    taught = Workflow.load(out)
    # Same drift: dismiss after Open, open again, continue.
    report, backend = _replay(taught, modal=MODAL, bundle=out, run_dir=tmp_path / "r1")
    assert report.success is True, report.halt
    assert backend.actions == [
        ("click", *OPEN_POINT, False),
        ("click", *DISMISS_POINT, False),
        ("click", *OPEN_POINT, False),
        ("click", *FIELD_POINT, False),
    ]
    # Clean run: the handler is never visited.
    report, backend = _replay(taught, modal=None, bundle=out, run_dir=tmp_path / "r2")
    assert report.success is True
    assert backend.actions == [
        ("click", *OPEN_POINT, False),
        ("click", *FIELD_POINT, False),
    ]
    # A different dialog: the handler's guard is unmet, so the run stops.
    report, backend = _replay(
        taught, modal=OTHER_MODAL, bundle=out, run_dir=tmp_path / "r3"
    )
    assert report.success is False
    assert report.terminal_outcome == "halt"
    assert backend.actions == [("click", *OPEN_POINT, False)]


def test_the_spliced_step_carries_the_recorded_target(tmp_path):
    bundle = _bundle(tmp_path)
    run_dir = tmp_path / "run"
    _halted_run(bundle, run_dir)
    fix = _fix_recording(tmp_path / "fix", DISMISS_BUTTON, OPEN_BUTTON)
    out = tmp_path / "taught"
    assert _teach(run_dir, fix, bundle, out) == 0

    program = Workflow.load(out).program
    assert program is not None
    handler = program.states[program.states["s_open"].on_exception or ""]
    assert handler.step is not None and handler.step.anchor is not None
    anchor = handler.step.anchor
    # The recorded click (140, 108), not the placeholder (100, 100).
    assert anchor.click_point == (140, 108)
    assert anchor.template.startswith("templates/teach-rec-fix-001/")
    copied = out / anchor.template
    compiled = fix / "_teach_compiled" / "templates" / Path(anchor.template).name
    assert copied.read_bytes() == compiled.read_bytes()
    # The base bundle's crops came along too, and the manifest covers them all.
    assert (out / "templates" / "open.png").is_file()
    manifest = json.loads((out / "manifest.json").read_text())
    assert anchor.template in manifest["file_hashes"]
    assert "templates/open.png" in manifest["file_hashes"]


def test_teach_refuses_to_retry_a_delivered_step_that_can_write(tmp_path):
    bundle = _bundle(tmp_path, open_risk="irreversible")
    run_dir = tmp_path / "run"
    _halted_run(bundle, run_dir)
    fix = _fix_recording(tmp_path / "fix", DISMISS_BUTTON, OPEN_BUTTON)
    out = tmp_path / "taught"

    assert _teach(run_dir, fix, bundle, out) == 1
    assert not out.exists()
    assert not (tmp_path / "taught.skills").exists()


def test_the_refusal_names_the_double_write(tmp_path, capsys):
    bundle = _bundle(tmp_path, open_risk="irreversible")
    run_dir = tmp_path / "run"
    _halted_run(bundle, run_dir)
    fix = _fix_recording(tmp_path / "fix", DISMISS_BUTTON, OPEN_BUTTON)
    capsys.readouterr()
    _teach(run_dir, fix, bundle, tmp_path / "taught")
    out = capsys.readouterr().out
    assert "can write to the record" in out
    assert "save twice" in out
    assert "held-out coverage" not in out


def test_a_fix_that_does_not_repeat_the_step_continues_after_it(tmp_path):
    """Open took effect and the survey only covered the form. The operator
    dismissed it and went on: the learned program dismisses it, checks that the
    form is there (Open's own expected screen), and continues. Open is clicked
    once."""
    bundle = _bundle(tmp_path)
    run_dir = tmp_path / "run"
    _halted_run(bundle, run_dir, retry_needed=False)
    fix = _fix_recording(tmp_path / "fix", DISMISS_BUTTON)
    out = tmp_path / "taught"

    assert _teach(run_dir, fix, bundle, out) == 0

    taught = Workflow.load(out)
    program = taught.program
    assert program is not None
    handler = program.states[program.states["s_open"].on_exception or ""]
    assert handler.step is not None
    assert handler.transitions[0].target == "s_field"
    assert FORM_READY in [p.text for p in handler.step.expect]
    report, backend = _replay(
        taught, modal=MODAL, bundle=out, run_dir=tmp_path / "r1", retry_needed=False
    )
    assert report.success is True, report.halt
    assert backend.actions == [
        ("click", *OPEN_POINT, False),
        ("click", *DISMISS_POINT, False),
        ("click", *FIELD_POINT, False),
    ]
    # If the survey had in fact stopped Open, the form is missing after the
    # dismissal and the handler's check stops the run.
    report, backend = _replay(
        taught, modal=MODAL, bundle=out, run_dir=tmp_path / "r2", retry_needed=True
    )
    assert report.success is False
    assert backend.actions == [
        ("click", *OPEN_POINT, False),
        ("click", *DISMISS_POINT, False),
    ]


def test_continuing_after_a_step_that_saves_never_clicks_it_twice(tmp_path):
    """An irreversible step with no system-of-record check may be continued
    after, because the learned program never delivers it again."""
    bundle = _bundle(tmp_path, open_risk="irreversible")
    run_dir = tmp_path / "run"
    _halted_run(bundle, run_dir, retry_needed=False)
    fix = _fix_recording(tmp_path / "fix", DISMISS_BUTTON)
    out = tmp_path / "taught"

    assert _teach(run_dir, fix, bundle, out) == 0

    report, backend = _replay(
        Workflow.load(out),
        modal=MODAL,
        bundle=out,
        run_dir=tmp_path / "r1",
        retry_needed=False,
    )
    assert report.success is True, report.halt
    assert [a for a in backend.actions if a[1:3] == OPEN_POINT] == [
        ("click", *OPEN_POINT, False)
    ]


def test_teach_refuses_to_continue_past_an_unchecked_write(tmp_path):
    """A step whose write is checked against the system of record only after
    its screen check passes: after the fix nothing would check the write."""
    plain = _bundle(tmp_path)
    run_dir = tmp_path / "run"
    _halted_run(plain, run_dir, retry_needed=False)
    with_effects = _bundle(tmp_path, effects=True)
    fix = _fix_recording(tmp_path / "fix", DISMISS_BUTTON)
    out = tmp_path / "taught"

    assert _teach(run_dir, fix, with_effects, out) == 1
    assert not out.exists()


def test_a_fix_without_a_recorded_target_is_refused_on_a_real_bundle(tmp_path):
    """A .json fix names intents only, so the spliced step gets a placeholder
    target. The modal-once bundle of ``test_teach_cli`` carries a crop for that
    placeholder; a real bundle doesn't. There the step could never resolve, so
    teach refuses and writes nothing instead of reporting LEARNED."""
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "verify.png").write_bytes(make_png((50, 20)))
    modal_once_workflow().save(bundle)  # no templates/x.png
    run_dir = tmp_path / "run"
    report, _ = modal_once_replay(
        modal_once_workflow(),
        modal_text=MODAL_ONCE_FACT,
        bundle=bundle,
        run_dir=run_dir,
    )
    assert report.terminal_outcome == "halt"
    fix = tmp_path / "fix.json"
    fix.write_text(
        json.dumps(
            {"resolution_steps": [{"intent": MODAL_ONCE_DISMISS, "action": "click"}]}
        )
    )
    out = tmp_path / "taught"

    assert _teach(run_dir, fix, bundle, out) == 1
    assert not out.exists()


# -- the guard keys on new text ------------------------------------------------


def test_guard_prefers_the_fixing_controls_label_among_new_texts():
    observed = ["MockMed", "Clinical demo - all data is fake", "Survey", "Maybe later"]
    seen = ["MockMed", "Clinical demo - all data is fake", "Save Encounter"]
    assert _choose_guard_fact(observed, seen, ["Maybe later"]) == "Maybe later"


def test_guard_falls_back_to_the_first_new_text_in_reading_order():
    observed = ["MockMed", "Survey", "Before saving, please rate"]
    assert _choose_guard_fact(observed, ["MockMed"], []) == "Survey"


def test_guard_ignores_text_every_screen_shows():
    # "MockMed" is on every screen: a guard on it would fire on clean runs.
    assert _choose_guard_fact(["MockMed"], ["MockMed"], ["Maybe later"]) is None


def test_guard_tolerates_small_ocr_differences():
    observed = ["Clinical demo—all data is fake", "Maybe later"]
    seen = ["Clinical demo — all data is fake"]
    assert _choose_guard_fact(observed, seen, []) == "Maybe later"


def test_without_earlier_frames_only_the_fix_label_qualifies():
    observed = ["MockMed", "Maybe later"]
    assert _choose_guard_fact(observed, None, ["Maybe later"]) == "Maybe later"
    assert _choose_guard_fact(observed, None, []) is None


# -- the symbolic interpreter follows exception edges --------------------------


def _trace(*steps: TraceStep, facts: Optional[dict[str, bool]] = None):
    return ExecutionTrace(trace_id="t", steps=list(steps), facts=facts or {})


def test_a_failed_step_without_a_handler_is_a_coverage_gap():
    result = program_reproduces(
        _program(),
        _trace(
            TraceStep(intent=INTENT_OPEN, failed=True),
            TraceStep(intent=INTENT_FIELD),
        ),
    )
    assert not result.reproduced
    assert "no exception handler" in result.reason


def test_a_failed_step_continues_through_its_handler():
    induced = StructuralDiffInducer().induce(
        [
            _trace(TraceStep(intent=INTENT_OPEN), TraceStep(intent=INTENT_FIELD)),
            _trace(
                TraceStep(intent=INTENT_OPEN, failed=True),
                TraceStep(intent=INTENT_DISMISS),
                TraceStep(intent=INTENT_OPEN),
                TraceStep(intent=INTENT_FIELD),
                facts={MODAL: True},
            ),
        ],
        base=_program(),
    )
    drift = _trace(
        TraceStep(intent=INTENT_OPEN, failed=True),
        TraceStep(intent=INTENT_DISMISS),
        TraceStep(intent=INTENT_OPEN),
        TraceStep(intent=INTENT_FIELD),
        facts={MODAL: True},
    )
    clean = _trace(TraceStep(intent=INTENT_OPEN), TraceStep(intent=INTENT_FIELD))
    assert program_reproduces(induced, drift).reproduced
    assert program_reproduces(induced, clean).reproduced
    # Without the survey on screen the handler's guard halts.
    no_fact = drift.model_copy(update={"facts": {}})
    assert not program_reproduces(induced, no_fact).reproduced


def test_inducer_keeps_the_before_placement_for_a_step_that_never_acted():
    induced = StructuralDiffInducer().induce(
        [
            _trace(TraceStep(intent=INTENT_OPEN), TraceStep(intent=INTENT_FIELD)),
            _trace(
                TraceStep(intent=INTENT_OPEN),
                TraceStep(intent=INTENT_DISMISS),
                TraceStep(intent=INTENT_FIELD),
                facts={MODAL: True},
            ),
        ],
        base=_program(),
    )
    assert induced.states["s_open"].on_exception is None
    dismiss_id = induced.states["s_open"].transitions[0].target
    dismiss = induced.states[dismiss_id]
    assert dismiss.step is not None and dismiss.step.guard is not None
    assert dismiss.step.guard.on_unmet == "skip"
    assert dismiss.transitions[0].target == "s_field"


def test_inducer_leaves_an_ambiguous_splice_point_alone():
    base = _program()
    # Two states perform the same intent: the splice point is ambiguous.
    twin = base.states["s_open"].model_copy(deep=True, update={"id": "s_open2"})
    base.states["s_open2"] = twin
    induced = StructuralDiffInducer().induce(
        [
            _trace(
                TraceStep(intent=INTENT_OPEN),
                TraceStep(intent=INTENT_DISMISS),
                TraceStep(intent=INTENT_FIELD),
                facts={MODAL: True},
            ),
        ],
        base=base,
    )
    assert induced == base


# -- the gate sees writes reachable only through an exception edge -------------


def test_gate_catches_a_write_added_behind_an_exception_handler():
    active = _program()
    candidate = active.model_copy(deep=True)
    write = Step(
        id="s_submit",
        intent="click 'Submit'",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/field.png",
            region=FIELD_REGION,
            click_point=FIELD_POINT,
        ),
        risk="irreversible",
        guard=Guard(
            predicate=Predicate(kind=PredicateKind.TEXT_PRESENT, text=MODAL),
            on_unmet="halt",
        ),
    )
    candidate.states["s_submit"] = State(
        id="s_submit",
        kind=StateKind.ACTION,
        step=write,
        transitions=[Transition(target="__end__")],
    )
    candidate.states["s_open"].on_exception = "s_submit"

    report = program_regression_gate(active, candidate)

    assert not report.passed
    assert any("without effects" in f for f in report.failures), report.failures
