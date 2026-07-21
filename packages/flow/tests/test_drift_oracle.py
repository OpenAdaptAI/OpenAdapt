"""Drift-oracle: the optional VLM state-verifier that may CONFIRM a
render-drift-sensitive postcondition which deterministically false-failed.

Veto-safe by construction — these tests pin that:
* only a confident "yes" rescues; "no"/"uncertain"/outage keep the halt,
* structural / text_absent postconditions are NEVER rescued,
* every call and rescue is recorded on the StepResult for the report,
* with no verifier configured (the default) behaviour is unchanged.
"""

from __future__ import annotations

import io

from PIL import Image

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    StepResult,
)
from openadapt_flow.runtime.replayer import Replayer

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


def _step(pc: Postcondition) -> Step:
    return Step(
        id="s1",
        intent="click Save",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/btn.png",
            region=(100, 100, 50, 20),
            click_point=(110, 105),
            ocr_text="Save",
        ),
        expect=[pc],
    )


def _text_present_pc() -> Postcondition:
    # deterministically FAILS: the vision below never reports "Saved"
    return Postcondition(
        kind=PostconditionKind.TEXT_PRESENT, text="Saved", timeout_s=0.0
    )


def _check(vision, verifier, pc, tmp_path):
    rp = Replayer(
        _Backend(), vision=vision, state_verifier=verifier, poll_interval_s=0.0
    )
    result = StepResult(step_id="s1", intent="click Save", ok=False)
    ok, _frame, failed = rp._check_postconditions(
        _step(pc), _png(), tmp_path, {}, result
    )
    return ok, failed, result


# --------------------------------------------------------------------------


def test_confident_yes_rescues_a_drifted_text_postcondition(tmp_path):
    vision = _Vision(present=set())  # "Saved" not readable (drift)
    verifier = _Verifier("yes")
    ok, failed, result = _check(vision, verifier, _text_present_pc(), tmp_path)
    assert ok is True and failed == []
    assert result.postcondition_drift_rescues == ["text_present 'Saved'"]
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
