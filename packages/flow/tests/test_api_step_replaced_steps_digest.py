"""An API step stands for the exact steps it was admitted for.

The reviewer's case for #1269: folded_checks_digest hashed only the folded
steps' on-screen checks, so changing what a replaced step does (its action,
its value, its parameter, its target or identity, a guard, the save's record
check) left the admitted API step running in its place.
"""

from __future__ import annotations

import pytest

from openadapt_flow.ir import ActionKind, Anchor, Guard, Predicate, PredicateKind
from openadapt_flow.runtime.replayer import Replayer
from tests.test_api_tier_web_form import _span_workflow
from tests.test_replayer import FakeBackend, FakeVision


def _edit(wf, index, **update):
    steps = list(wf.steps)
    steps[index] = steps[index].model_copy(update=update)
    return wf.model_copy(update={"steps": steps})


def _why(wf):
    replayer = Replayer(FakeBackend(), vision=FakeVision(), web_form_actuator=object())
    _end, _save, why = replayer._web_form_span_at(wf, 0, durable=False)
    return why


def test_the_admitted_steps_run_as_the_api_step():
    assert _why(_span_workflow()) is None


@pytest.mark.parametrize(
    "index, update",
    [
        (0, {"action": ActionKind.CLICK, "key": None}),
        (0, {"key": "Shift+Tab"}),
        (0, {"text": "{lname}", "action": ActionKind.TYPE, "key": None}),
        (0, {"param": "lname"}),
        (
            0,
            {
                "anchor": Anchor(
                    template="templates/x.png",
                    region=(0, 0, 10, 10),
                    click_point=(5, 5),
                    context_text="Another record",
                )
            },
        ),
        (
            0,
            {
                "guard": Guard(
                    predicate=Predicate(
                        kind=PredicateKind.PARAM_EQUALS, param="lname", value="x"
                    ),
                    on_unmet="halt",
                )
            },
        ),
        (1, {"key": "Ctrl+Enter"}),
        (1, {"risk": "irreversible"}),
    ],
)
def test_any_change_to_a_replaced_step_keeps_it_on_the_gui(index, update):
    why = _why(_edit(_span_workflow(), index, **update))
    assert why is not None and "replaces changed" in why
