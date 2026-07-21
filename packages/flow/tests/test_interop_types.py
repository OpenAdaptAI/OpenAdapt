"""Tests for the openadapt-types interop shim (openadapt_flow.interop.types).

Covers:
* the ActionKind -> ActionType enum map is exhaustive and value-correct (and a
  byte-identical subset of the canonical ActionType);
* a representative Step round-trips its shared fields into a valid Action;
* StepResult -> ActionResult carries ok/error (and resolved coords / duration);
* the partial reverse hydrate and its refusal of out-of-vocabulary actions;
* the shim is import-light — importing it does NOT import openadapt_types until
  a function is actually called (asserted in an isolated subprocess).

Tests that need the optional dependency ``pytest.importorskip`` it, so the file
skips gracefully where openadapt-types isn't installed. CI installs it via the
``interop`` extra so these actually run.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from openadapt_flow import ir
from openadapt_flow.interop import types as interop

# The shim itself is import-light and always importable; the *tests* below
# exercise the canonical schema, so skip the whole module when the optional
# dependency is absent. CI installs it via the ``interop`` extra.
pytest.importorskip("openadapt_types")


# --- enum map: exhaustive, value-correct, subset ---------------------------


def test_action_kind_map_is_exhaustive_and_value_correct() -> None:
    """Every one of flow's 6 ActionKinds maps to its identical string value."""
    expected = {
        ir.ActionKind.CLICK: "click",
        ir.ActionKind.DOUBLE_CLICK: "double_click",
        ir.ActionKind.TYPE: "type",
        ir.ActionKind.KEY: "key",
        ir.ActionKind.WAIT: "wait",
        ir.ActionKind.SCROLL: "scroll",
    }
    # All 6 members are present (exhaustive) ...
    assert set(interop.ACTION_KIND_TO_ACTION_TYPE) == set(ir.ActionKind)
    assert len(interop.ACTION_KIND_TO_ACTION_TYPE) == 6
    # ... and each maps to the byte-identical value.
    assert interop.ACTION_KIND_TO_ACTION_TYPE == expected


def test_action_kind_is_byte_identical_subset_of_action_type() -> None:
    """set(ActionKind values) is a subset of set(ActionType values), identical."""
    from openadapt_types import ActionType

    kind_values = {k.value for k in ir.ActionKind}
    type_values = {t.value for t in ActionType}
    assert kind_values <= type_values
    # And each flow value resolves to a real ActionType member with equal value.
    for kind, value in interop.ACTION_KIND_TO_ACTION_TYPE.items():
        assert ActionType(value).value == kind.value


# --- Step -> Action --------------------------------------------------------


def _click_step() -> ir.Step:
    return ir.Step(
        id="s1",
        intent="Click the Submit button",
        action=ir.ActionKind.CLICK,
        anchor=ir.Anchor(
            template="templates/s1.png",
            region=(10, 20, 40, 15),
            click_point=(30, 27),
            ocr_text="Submit",
        ),
        risk="irreversible",
        identity_armed=True,
    )


def test_click_step_round_trips_shared_fields() -> None:
    from openadapt_types import Action, ActionType

    action = interop.step_to_action(_click_step())

    assert isinstance(action, Action)
    assert action.type == ActionType.CLICK
    assert action.target is not None
    assert (action.target.x, action.target.y) == (30.0, 27.0)
    assert action.target.is_normalized is False
    assert action.target.description == "Submit"
    assert action.reasoning == "Click the Submit button"


def test_type_step_with_literal_text() -> None:
    from openadapt_types import ActionType

    step = ir.Step(id="s2", intent="type note", action=ir.ActionKind.TYPE, text="hello")
    action = interop.step_to_action(step)
    assert action.type == ActionType.TYPE
    assert action.text == "hello"


def test_type_step_with_param_yields_valid_placeholder() -> None:
    """A parameterized TYPE step (no literal text) still yields a valid Action."""
    step = ir.Step(id="s3", intent="type mrn", action=ir.ActionKind.TYPE, param="mrn")
    action = interop.step_to_action(step)  # must not raise the TYPE-needs-text error
    assert action.text == "{mrn}"


def test_key_step() -> None:
    from openadapt_types import ActionType

    step = ir.Step(id="s4", intent="press enter", action=ir.ActionKind.KEY, key="Enter")
    action = interop.step_to_action(step)
    assert action.type == ActionType.KEY
    assert action.key == "Enter"


@pytest.mark.parametrize(
    "dx,dy,direction,amount",
    [
        (None, 120, "down", 120),
        (None, -120, "up", 120),
        (90, None, "right", 90),
        (-90, None, "left", 90),
    ],
)
def test_scroll_step_direction_and_amount(dx, dy, direction, amount) -> None:
    step = ir.Step(
        id="s5",
        intent="scroll",
        action=ir.ActionKind.SCROLL,
        scroll_dx=dx,
        scroll_dy=dy,
    )
    action = interop.step_to_action(step)
    assert action.scroll_direction == direction
    assert action.scroll_amount == amount


def test_wait_step_has_no_target() -> None:
    from openadapt_types import ActionType

    step = ir.Step(id="s6", intent="settle", action=ir.ActionKind.WAIT)
    action = interop.step_to_action(step)
    assert action.type == ActionType.WAIT
    assert action.target is None


def test_all_action_kinds_produce_valid_actions() -> None:
    """Exercising every kind through the shim yields validator-passing Actions."""
    from openadapt_types import Action

    kinds_and_kwargs = {
        ir.ActionKind.CLICK: {"anchor": _click_step().anchor},
        ir.ActionKind.DOUBLE_CLICK: {"anchor": _click_step().anchor},
        ir.ActionKind.TYPE: {"text": "x"},
        ir.ActionKind.KEY: {"key": "Enter"},
        ir.ActionKind.WAIT: {},
        ir.ActionKind.SCROLL: {"scroll_dy": 40},
    }
    for kind, kwargs in kinds_and_kwargs.items():
        step = ir.Step(id="k", intent="i", action=kind, **kwargs)
        assert isinstance(interop.step_to_action(step), Action)


# --- StepResult -> ActionResult -------------------------------------------


def test_result_carries_ok() -> None:
    from openadapt_types import ActionResult

    sr = ir.StepResult(
        step_id="s1",
        intent="click",
        ok=True,
        resolution=ir.Resolution(
            rung="template", point=(30, 27), confidence=0.99, elapsed_ms=12.0
        ),
        elapsed_ms=42.4,
    )
    result = interop.result_to_action_result(sr)
    assert isinstance(result, ActionResult)
    assert result.success is True
    assert result.error is None
    assert result.duration_ms == 42
    assert result.resolved_coordinates == (30, 27)


def test_result_carries_error_and_infers_type() -> None:
    sr = ir.StepResult(
        step_id="s1",
        intent="click",
        ok=False,
        error="target not found",
        resolution=None,
    )
    result = interop.result_to_action_result(sr)
    assert result.success is False
    assert result.error == "target not found"
    assert result.error_type == "grounding_error"


def test_result_identity_mismatch_maps_to_state_mismatch() -> None:
    sr = ir.StepResult(
        step_id="s1",
        intent="click",
        ok=False,
        error="wrong patient",
        resolution=ir.Resolution(
            rung="template", point=(1, 2), confidence=0.9, elapsed_ms=1.0
        ),
        identity=ir.IdentityCheck(status="mismatch"),
    )
    result = interop.result_to_action_result(sr)
    assert result.error_type == "state_mismatch"


# --- reverse (partial hydrate) --------------------------------------------


def test_reverse_round_trips_shared_vocabulary() -> None:
    from openadapt_types import Action, ActionTarget, ActionType

    action = Action(type=ActionType.TYPE, text="hello", reasoning="type greeting")
    step = interop.action_to_step(action, step_id="ing1")
    assert step.action == ir.ActionKind.TYPE
    assert step.text == "hello"
    assert step.intent == "type greeting"
    # Coordinates are intentionally dropped (flow targets via visual anchors).
    action2 = Action(type=ActionType.CLICK, target=ActionTarget(x=5, y=6))
    step2 = interop.action_to_step(action2)
    assert step2.anchor is None


def test_reverse_scroll_round_trips() -> None:
    from openadapt_types import Action, ActionType

    action = Action(type=ActionType.SCROLL, scroll_direction="down", scroll_amount=80)
    step = interop.action_to_step(action)
    assert step.scroll_dy == 80
    assert step.scroll_dx is None


def test_reverse_rejects_out_of_vocabulary_action() -> None:
    from openadapt_types import Action, ActionTarget, ActionType

    action = Action(type=ActionType.RIGHT_CLICK, target=ActionTarget(x=1, y=1))
    with pytest.raises(ValueError, match="no flow ActionKind equivalent"):
        interop.action_to_step(action)


# --- import-light guarantee ------------------------------------------------


def test_importing_shim_does_not_import_openadapt_types() -> None:
    """The module must not pull openadapt_types until a function is called.

    Verified in a fresh subprocess so pytest.importorskip / other tests in this
    process can't pollute the result.
    """
    code = (
        "import sys\n"
        "import openadapt_flow.interop.types as t\n"
        "assert 'openadapt_types' not in sys.modules, "
        "'shim imported openadapt_types at module import time'\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


# --- adversarial-review fixes: param round-trip + timeout error_type ----------


def test_param_placeholder_round_trips_back_to_param():
    import pytest

    pytest.importorskip("openadapt_types")
    from openadapt_flow import ir
    from openadapt_flow.interop.types import step_to_action, action_to_step

    s = ir.Step(
        id="s1", intent="type mrn", action=ir.ActionKind.TYPE, param="mrn", text=None
    )
    back = action_to_step(step_to_action(s))
    assert back.param == "mrn" and back.text is None  # not literal "{mrn}"


def test_literal_braced_text_on_non_type_stays_literal():
    import pytest

    pytest.importorskip("openadapt_types")
    from openadapt_flow import ir
    from openadapt_flow.interop.types import action_to_step
    from openadapt_types import Action, ActionType

    a = Action(type=ActionType.KEY, key="{enter}")
    assert action_to_step(a).key == "{enter}"  # untouched (not a TYPE param)


def test_timeout_error_maps_to_timeout_error_type():
    import pytest

    pytest.importorskip("openadapt_types")
    from openadapt_flow import ir
    from openadapt_flow.interop.types import result_to_action_result

    r = ir.StepResult(
        step_id="s1",
        intent="x",
        ok=False,
        error="Timeout (>600.0s) waiting for postcondition",
    )
    assert result_to_action_result(r).error_type == "timeout"
