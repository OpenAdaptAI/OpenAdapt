"""Self-repair keeps a visual save's submit binding (found on openIMIS, #1331).

openIMIS's Save is an icon-only control recorded on the visual path, so its
step carries a ``submit_binding`` and no usable structural locator. When the
vendor relabelled it "Save claim", the agent proposed the right control, but
the patch put the DOM locator under the point on the anchor, and the patched
bundle no longer validated: a step may not carry both. The patch now keeps
such a step visual and keeps its binding. No browser.
"""

from __future__ import annotations

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Step,
    StructuralLocator,
    SubmitBinding,
    Workflow,
)
from openadapt_flow.repair.self_repair import _patched_structural


def _binding() -> SubmitBinding:
    return SubmitBinding(
        control_tag="button",
        control_type="button",
        fixed_position=True,
        form_owner="fields_only",
        submit_role="detached",
        scope_kind="container",
        scope_path="div>button",
        filled_fields=3,
        vertical="below",
        horizontal="right",
    )


def _save(**fields) -> Step:
    return Step(
        id="step_015",
        intent="click recorded visual target",
        action=ActionKind.CLICK,
        risk="irreversible",
        anchor=Anchor(
            template="templates/step_015.png", region=(0, 0, 10, 10), click_point=(5, 5)
        ),
        **fields,
    )


def _locator() -> StructuralLocator:
    return StructuralLocator(selector="#save-claim", role="button", name="Save claim")


def test_a_visual_save_with_a_binding_stays_visual_and_keeps_it() -> None:
    step = _save(submit_binding=_binding())
    kept = _patched_structural(step, _locator())
    assert kept is None
    anchor = step.anchor.model_copy(
        update={"template": "templates/step_015_repair.png", "structural": kept}
    )
    patched = step.model_copy(update={"anchor": anchor})
    # The patched step validates, as the patched bundle must on load.
    Workflow.model_validate({"name": "w", "steps": [patched.model_dump(mode="json")]})
    assert patched.submit_binding == step.submit_binding


def test_a_step_without_a_binding_takes_the_new_locator() -> None:
    locator = _locator()
    assert _patched_structural(_save(), locator) is locator
