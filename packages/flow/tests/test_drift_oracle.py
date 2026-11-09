"""Drift-oracle: the optional VLM state-verifier that may CONFIRM a
render-drift-sensitive postcondition which deterministically false-failed.

Veto-safe by construction — these tests pin that:
* only a confident "yes" rescues; "no"/"uncertain"/outage keep the halt,
* structural / text_absent postconditions are NEVER rescued,
* every call and rescue is recorded on the StepResult for the report,
* with no verifier configured (the default) behaviour is unchanged,
* an irreversible or effect-bearing step is never rescued: its failed
  postcondition stays failed (a run with no handler halts with transaction
  outcome RECONCILIATION_REQUIRED), while a navigation step keeps the rescue.
"""

from __future__ import annotations

import io

from PIL import Image

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    ProgramGraph,
    State,
    StateKind,
    Step,
    StepResult,
    Transition,
    Workflow,
)
from openadapt_flow.plain_outcome import plain_result
from openadapt_flow.runtime.effects import Effect, EffectKind
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision, Match

VIEWPORT = (300, 200)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", VIEWPORT, (240, 240, 240)).save(buf, format="PNG")
    return buf.getvalue()


class _Vision:
    """Minimal vision: text_present is scripted; wait_settled re-screenshots."""

    def __init__(self, present: set[str]):
        self._present = present

    def text_present(self, png, text, *, region=None, min_ratio=0.8):
        return text in self._present

    def find_template(self, *a, **k):
        return None

    def phash_png(self, png, region=None):
        return "aa"

    def phash_distance(self, a, b):
        return 99  # never stable, so region_stable fails deterministically

    def wait_settled(self, backend, **k):
        return backend.screenshot()


class _Backend:
    viewport = VIEWPORT

    def screenshot(self):
        return _png()


class _Verifier:
    """Scripted state-verifier. ``answer`` is 'yes'/'no'/'uncertain' or 'boom'
    to raise (simulate an outage the client failed to swallow)."""

    def __init__(self, answer: str):
        self.answer = answer
        self.calls = 0

    def holds(self, screenshot, expected_state) -> bool:
        self.calls += 1
        if self.answer == "boom":
            raise RuntimeError("appliance unreachable")
        return self.answer == "yes"


def _step(
    pc: Postcondition,
    *,
    step_id: str = "s1",
    intent: str = "open the Patients tab",
    label: str = "Patients",
    **fields,
) -> Step:
    """A navigation step by default: no write verb, no declared effect."""
    return Step(
        id=step_id,
        intent=intent,
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/btn.png",
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text=label,
        ),
        expect=[pc],
        **fields,
    )


def _text_present_pc(text: str = "Patient list") -> Postcondition:
    # deterministically FAILS: the vision below never reports the text
    return Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=text, timeout_s=0.0)


def _check(vision, verifier, pc, tmp_path, step=None):
    rp = Replayer(
        _Backend(), vision=vision, state_verifier=verifier, poll_interval_s=0.0
    )
    step = step if step is not None else _step(pc)
    result = StepResult(step_id=step.id, intent=step.intent, ok=False)
    ok, _frame, failed = rp._check_postconditions(step, _png(), tmp_path, {}, result)
    return ok, failed, result


# --------------------------------------------------------------------------


def test_confident_yes_rescues_a_drifted_text_postcondition(tmp_path):
    vision = _Vision(present=set())  # "Patient list" not readable (drift)
    verifier = _Verifier("yes")
    ok, failed, result = _check(vision, verifier, _text_present_pc(), tmp_path)
    assert ok is True and failed == []
    assert result.postcondition_drift_rescues == ["text_present 'Patient list'"]
    assert result.drift_oracle_calls == 1
    assert verifier.calls == 1


def test_no_answer_keeps_the_halt(tmp_path):
    ok, failed, result = _check(
        _Vision(set()), _Verifier("no"), _text_present_pc(), tmp_path
    )
    assert ok is False and failed  # still failed -> halt
    assert result.postcondition_drift_rescues == []
    assert result.drift_oracle_calls == 1


def test_uncertain_answer_keeps_the_halt(tmp_path):
    ok, failed, result = _check(
        _Vision(set()), _Verifier("uncertain"), _text_present_pc(), tmp_path
    )
    assert ok is False and failed
    assert result.postcondition_drift_rescues == []


def test_verifier_error_is_fail_safe_halt(tmp_path):
    # An exception from the verifier must never rescue — it keeps the halt.
    ok, failed, result = _check(
        _Vision(set()), _Verifier("boom"), _text_present_pc(), tmp_path
    )
    assert ok is False and failed
    assert result.postcondition_drift_rescues == []
    assert result.drift_oracle_calls == 1


def test_text_absent_is_never_rescued(tmp_path):
    # text_absent that FAILS (the text IS present) is a real failure, not
    # render drift: it must not even consult the verifier.
    pc = Postcondition(kind=PostconditionKind.TEXT_ABSENT, text="Error", timeout_s=0.0)
    vision = _Vision(present={"Error"})  # "Error" present -> pc fails
    verifier = _Verifier("yes")  # would rescue if consulted
    ok, failed, result = _check(vision, verifier, pc, tmp_path)
    assert ok is False and failed
    assert verifier.calls == 0  # never consulted
    assert result.drift_oracle_calls == 0


def test_region_stable_is_rescuable(tmp_path):
    pc = Postcondition(
        kind=PostconditionKind.REGION_STABLE,
        region=(10, 10, 40, 20),
        phash="zz",
        timeout_s=0.0,
    )
    ok, failed, result = _check(_Vision(set()), _Verifier("yes"), pc, tmp_path)
    assert ok is True and failed == []
    assert result.drift_oracle_calls == 1


def test_no_verifier_leaves_behaviour_unchanged(tmp_path):
    # The default: no appliance -> failed stays failed, zero model calls.
    rp = Replayer(_Backend(), vision=_Vision(set()), poll_interval_s=0.0)
    result = StepResult(step_id="s1", intent="x", ok=False)
    ok, _f, failed = rp._check_postconditions(
        _step(_text_present_pc()), _png(), tmp_path, {}, result
    )
    assert ok is False and failed
    assert result.drift_oracle_calls == 0
    assert result.postcondition_drift_rescues == []


# --------------------------------------------------------------------------
# Irreversible and effect-bearing steps: the oracle never rescues.


def test_irreversible_step_is_never_rescued(tmp_path):
    verifier = _Verifier("yes")  # would rescue if consulted
    step = _step(
        _text_present_pc("Claim submitted"),
        intent="click 'Submit claim'",
        label="Submit claim",
        risk="irreversible",
    )
    ok, failed, result = _check(
        _Vision(set()), verifier, step.expect[0], tmp_path, step
    )
    assert ok is False and failed == ["text_present 'Claim submitted'"]
    assert verifier.calls == 0
    assert result.drift_oracle_calls == 0
    assert result.postcondition_drift_rescues == []


def test_write_shaped_step_is_never_rescued_without_a_risk_label(tmp_path):
    # The compiled label says reversible, but the shared classifier reads
    # "Save" as a write. The fail-closed reading wins.
    verifier = _Verifier("yes")
    step = _step(_text_present_pc("Saved"), intent="click Save", label="Save")
    assert step.risk == "reversible"
    ok, failed, result = _check(
        _Vision(set()), verifier, step.expect[0], tmp_path, step
    )
    assert ok is False and failed
    assert verifier.calls == 0
    assert result.postcondition_drift_rescues == []


def test_effect_bearing_step_is_never_rescued(tmp_path):
    # A navigation-shaped click that declares a system-of-record effect.
    verifier = _Verifier("yes")
    step = _step(
        _text_present_pc(),
        effects=[Effect(kind=EffectKind.RECORD_WRITTEN, match={"id": "1"})],
    )
    ok, failed, result = _check(
        _Vision(set()), verifier, step.expect[0], tmp_path, step
    )
    assert ok is False and failed
    assert verifier.calls == 0
    assert result.postcondition_drift_rescues == []


def test_step_awaiting_risk_review_is_never_rescued(tmp_path):
    verifier = _Verifier("yes")
    step = _step(_text_present_pc(), risk_review_required=True)
    ok, failed, result = _check(
        _Vision(set()), verifier, step.expect[0], tmp_path, step
    )
    assert ok is False and failed
    assert verifier.calls == 0


# --------------------------------------------------------------------------
# Full runs: the halt and its transaction outcome.


def _run(tmp_path, steps, verifier, workflow=None):
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(_png())
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
        for _ in range(20)
    ]
    backend = FakeBackend()
    report = Replayer(
        backend, vision=vision, state_verifier=verifier, poll_interval_s=0.0
    ).run(
        workflow if workflow is not None else Workflow(name="wf", steps=steps),
        bundle_dir=bundle,
        run_dir=tmp_path / "run",
    )
    return report, backend


def test_run_halts_with_reconciliation_when_oracle_says_yes_on_a_write(tmp_path):
    verifier = _Verifier("yes")
    navigate = _step(_text_present_pc("Claims"), step_id="nav")
    submit = _step(
        _text_present_pc("Claim submitted"),
        step_id="submit",
        intent="click 'Submit claim'",
        label="Submit claim",
        risk="irreversible",
    )
    after = _step(_text_present_pc("Done"), step_id="after")
    report, backend = _run(tmp_path, [navigate, submit, after], verifier)

    assert report.success is False
    nav_result, submit_result = report.results[:2]
    # The navigation step was rescued as before.
    assert nav_result.ok is True
    assert nav_result.postcondition_drift_rescues == ["text_present 'Claims'"]
    # The write was delivered, its screen check failed, and the oracle was
    # never asked.
    assert submit_result.ok is False
    assert submit_result.postconditions_ok is False
    assert submit_result.drift_oracle_calls == 0
    assert submit_result.postcondition_drift_rescues == []
    assert "Postconditions failed" in (submit_result.error or "")
    assert verifier.calls == 1  # the navigation step only
    # Fail-closed: the run stops at the write and never reaches the next step.
    assert [r.step_id for r in report.results] == ["nav", "submit"]
    assert len(backend.actions) == 2
    # The write may have landed, so a person checks the record. The run never
    # says it stopped before saving.
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
    plain = plain_result(report.transaction_outcome, report.execution_outcome)
    assert plain.key == "check_the_record"
    assert plain.nothing_written is False
    assert "Stopped before saving" not in plain.label + plain.meaning


def test_run_keeps_the_rescue_on_navigation_steps(tmp_path):
    verifier = _Verifier("yes")
    steps = [
        _step(_text_present_pc("Patient list"), step_id="nav1"),
        _step(
            _text_present_pc("Search"),
            step_id="nav2",
            intent="open the Search tab",
            label="Search",
        ),
    ]
    report, backend = _run(tmp_path, steps, verifier)

    assert report.success is True
    assert [r.postcondition_drift_rescues for r in report.results] == [
        ["text_present 'Patient list'"],
        ["text_present 'Search'"],
    ]
    assert [r.drift_oracle_calls for r in report.results] == [1, 1]
    assert verifier.calls == 2
    assert len(backend.actions) == 2


def _program_with_handler(step: Step) -> Workflow:
    """``step`` routes a failure to a recovery state that presses Escape."""
    escape = Step(
        id="recover", intent="press Escape", action=ActionKind.KEY, key="Escape"
    )
    program = ProgramGraph(
        entry=step.id,
        states={
            step.id: State(
                id=step.id,
                kind=StateKind.ACTION,
                step=step,
                transitions=[Transition(target="done")],
                on_exception="recover",
            ),
            "recover": State(
                id="recover",
                kind=StateKind.ACTION,
                step=escape,
                transitions=[Transition(target="done")],
            ),
            "done": State(id="done", kind=StateKind.TERMINAL, outcome="success"),
        },
    )
    return Workflow(name="wf", program=program)


def test_write_step_failure_reaches_its_handler_unrescued(tmp_path):
    # The oracle would say yes, but it is never asked: the write's check stays
    # failed, and the program's own handler sees the failure exactly as it
    # would with no verifier configured.
    verifier = _Verifier("yes")
    submit = _step(
        _text_present_pc("Claim submitted"),
        step_id="submit",
        intent="click 'Submit claim'",
        label="Submit claim",
        risk="irreversible",
    )
    report, backend = _run(
        tmp_path, [], verifier, workflow=_program_with_handler(submit)
    )

    result = next(r for r in report.results if r.step_id == "submit")
    assert result.ok is False
    assert result.postconditions_ok is False
    assert result.drift_oracle_calls == 0
    assert result.postcondition_drift_rescues == []
    assert verifier.calls == 0
    assert result.exception_handled is True
    assert ("press", "Escape") in backend.actions


def test_exception_edge_still_handles_a_failed_navigation_step(tmp_path):
    # Unchanged behaviour: the oracle says no, the navigation step fails, and
    # the program's handler takes over.
    verifier = _Verifier("no")
    navigate = _step(_text_present_pc("Patient list"), step_id="nav")
    report, backend = _run(
        tmp_path, [], verifier, workflow=_program_with_handler(navigate)
    )

    assert report.success is True
    result = next(r for r in report.results if r.step_id == "nav")
    assert result.ok is False
    assert result.exception_handled is True
    assert result.drift_oracle_calls == 1
    assert ("press", "Escape") in backend.actions
