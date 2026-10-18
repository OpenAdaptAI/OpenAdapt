"""Unit tests for binding an unlabeled submit control to the form it submits.

These cover the decision rules without a browser: which demonstrations yield
a binding, which live observations a run accepts, that nothing but structure
is retained, and that the compiler and replayer apply the rules. The browser
behaviour (real DOM observation, the delivery lease, two forms, a second
unlabeled icon, a moved button) is in ``test_submit_binding_browser.py``.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Step,
    StructuralLocator,
    SubmitBinding,
    Workflow,
    structural_locator_is_locatable,
)
from openadapt_flow.policy import lint_workflow
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.submit_binding import (
    SubmitBindingObservation,
    decide_submit_binding,
    describe_submit_binding,
    observation_from_page,
    verify_submit_binding,
)
from tests.test_replayer import FakeBackend, FakeVision, Match, make_png

ICON = "s0 0 24 24|pM17 3H5c-1.11 0-2 .9-2 2v14"


def _raw(**changes: Any) -> dict[str, Any]:
    """A page observation of openIMIS's floating Save after three fields.

    The structure is the one a live probe observed on openIMIS 25.10's claim
    form (``scripts/openimis_submit_binding_probe.py``): the Save is a fixed
    ``<button>`` inside ``div > div > span``, a sibling of the ``<form>``.
    """

    raw: dict[str, Any] = {
        "v": 1,
        "tag": "button",
        "type": "button",
        "role": None,
        "labelled": False,
        "in_row": False,
        "fixed": True,
        "fields": 3,
        "form_owner": "fields_only",
        "submit_role": "detached",
        "scope_kind": "container",
        "scope_path": "div>div>span>button",
        "fields_in_scope": True,
        "field_local": False,
        "order": "after",
        "vertical": "below",
        "horizontal": "right",
        "candidates": 1,
        "counted": True,
        "icon": ICON,
    }
    raw.update(changes)
    return raw


def _observation(**changes: Any) -> SubmitBindingObservation:
    return observation_from_page(_raw(**changes))


def _binding(**changes: Any) -> SubmitBinding:
    binding, reason = decide_submit_binding(
        _observation(**changes), field_step_ids=("step_000", "step_002")
    )
    assert reason is None, reason
    assert binding is not None
    return binding


# -- what the page may hand back -------------------------------------------


def test_observation_keeps_structure_only_and_hashes_the_icon() -> None:
    observation = _observation()

    assert observation.icon_sha256 == hashlib.sha256(ICON.encode()).hexdigest()
    dumped = observation.model_dump(mode="json", exclude_none=True)
    assert "icon" not in dumped
    assert ICON not in str(dumped)


@pytest.mark.parametrize(
    "smuggled",
    [
        {"text": "Jane Sample"},
        {"value": "610420118"},
        {"tag": "Jane Sample"},
        {"scope_path": "div>Jane"},
        {"role": "MRN 12345"},
    ],
)
def test_observation_refuses_anything_but_closed_structure(
    smuggled: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        observation_from_page(_raw(**smuggled))


def test_observation_refuses_an_unbounded_icon_and_two_icon_values() -> None:
    with pytest.raises(ValueError):
        observation_from_page(_raw(icon="p" * 20_001))
    with pytest.raises(ValueError):
        observation_from_page(_raw(icon_sha256="a" * 64))


# -- compile time: when a demonstration yields a binding -------------------


def test_floating_save_after_the_fields_is_bound_with_its_evidence() -> None:
    binding = _binding()

    assert binding.form_owner == "fields_only"
    assert binding.submit_role == "detached"
    assert binding.scope_kind == "container"
    assert binding.scope_path == "div>div>span>button"
    assert binding.filled_fields == 3
    assert (binding.order, binding.vertical, binding.horizontal) == (
        "after",
        "below",
        "right",
    )
    assert binding.unlabeled_controls_after_fields == 1
    assert binding.icon_sha256 == hashlib.sha256(ICON.encode()).hexdigest()
    assert binding.field_step_ids == ("step_000", "step_002")
    summary = describe_submit_binding(binding)
    assert "outside the form that owns the filled fields" in summary
    assert "the only unlabeled control after them" in summary


def test_a_submit_button_of_the_shared_form_is_bound() -> None:
    binding = _binding(
        type="submit",
        fixed=False,
        form_owner="shared",
        submit_role="form_submit",
        scope_kind="form",
        scope_path="div>button",
        vertical="below",
        horizontal="within",
    )

    assert binding.form_owner == "shared"
    assert binding.submit_role == "form_submit"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"labelled": True}, "accessible name"),
        ({"in_row": True}, "table row"),
        ({"fields": 0}, "no field the program typed into"),
        ({"form_owner": "other_form"}, "different form"),
        ({"form_owner": "mixed"}, "more than one form"),
        ({"fields_in_scope": False}, "outside the form or container"),
        ({"field_local": True}, "single field's own widget"),
        ({"order": "before"}, "does not follow the filled fields"),
        ({"order": "between"}, "does not follow the filled fields"),
        ({"candidates": 2}, "2 unlabeled controls follow the filled fields"),
        ({"counted": False}, "not a rendered, unlabeled control"),
        ({"scope_path": None}, "no element contains both"),
    ],
)
def test_an_ambiguous_or_unrelated_control_is_never_bound(
    changes: dict[str, Any], reason: str
) -> None:
    binding, why = decide_submit_binding(_observation(**changes))

    assert binding is None
    assert why is not None and reason in why


# -- run time: which live observations a run accepts -----------------------


def test_the_same_structure_verifies() -> None:
    assert verify_submit_binding(_binding(), _observation()) is None


@pytest.mark.parametrize(
    ("live", "reason"),
    [
        # A second unlabeled icon now follows the fields.
        ({"candidates": 2}, "2 unlabeled controls"),
        # The resolver landed on a different glyph in the same place.
        ({"icon": "pM6 19c0 1.1.9 2 2 2h8"}, "different icon"),
        # The button moved: into the header, beside the fields, to the left.
        ({"order": "before"}, "does not follow"),
        ({"vertical": "level"}, "moved relative to the filled fields"),
        ({"horizontal": "left"}, "moved relative to the filled fields"),
        ({"scope_path": "div>div>button"}, "different place in the page"),
        ({"fixed": False}, "fixed positioning"),
        # The resolver landed on another form's icon.
        ({"form_owner": "other_form"}, "different form"),
        ({"form_owner": "shared", "submit_role": "form_control"}, "ownership"),
        # The run typed into a different set of fields.
        ({"fields": 2}, "typed into 2 field(s)"),
        ({"form_owner": "mixed"}, "more than one form"),
        # Another kind of control entirely.
        ({"tag": "a", "type": None}, "<a>"),
        ({"type": "submit"}, "type"),
        ({"role": "link"}, "role"),
        ({"labelled": True}, "accessible name"),
        ({"in_row": True}, "table row"),
    ],
)
def test_any_structural_difference_refuses(live: dict[str, Any], reason: str) -> None:
    why = verify_submit_binding(_binding(), _observation(**live))

    assert why is not None and reason in why, why


# -- the IR contract --------------------------------------------------------


def _anchor(**changes: Any) -> Anchor:
    fields: dict[str, Any] = {
        "template": "templates/save.png",
        "region": (1200, 720, 56, 56),
        "click_point": (1228, 748),
    }
    fields.update(changes)
    return Anchor(**fields)


def test_binding_is_only_valid_on_an_unlocatable_click() -> None:
    binding = _binding()
    Step(
        id="save",
        intent="click recorded visual target",
        action=ActionKind.CLICK,
        anchor=_anchor(structural=StructuralLocator(role="button")),
        submit_binding=binding,
    )
    with pytest.raises(ValueError, match="submit_binding"):
        Step(
            id="save",
            intent="click",
            action=ActionKind.CLICK,
            anchor=_anchor(structural=StructuralLocator(selector="#save")),
            submit_binding=binding,
        )
    with pytest.raises(ValueError, match="submit_binding"):
        Step(
            id="type",
            intent="type",
            action=ActionKind.TYPE,
            text="x",
            submit_binding=binding,
        )
    with pytest.raises(ValueError, match="both"):
        Step(
            id="save",
            intent="click",
            action=ActionKind.CLICK,
            anchor=_anchor(),
            submit_binding=binding,
            submit_binding_unbound_reason="ambiguous",
        )


def test_only_a_selector_automation_id_or_named_role_is_locatable() -> None:
    assert not structural_locator_is_locatable(None)
    assert not structural_locator_is_locatable(StructuralLocator(role="button"))
    assert structural_locator_is_locatable(StructuralLocator(selector="#a"))
    assert structural_locator_is_locatable(StructuralLocator(automation_id="a"))
    assert structural_locator_is_locatable(
        StructuralLocator(role="button", name="Save")
    )


# -- compiler -----------------------------------------------------------------


def _click(step_id: str, *, risk: str = "irreversible", **anchor: Any) -> Step:
    return Step(
        id=step_id,
        intent="click recorded visual target",
        action=ActionKind.CLICK,
        risk=risk,
        anchor=_anchor(**anchor),
    )


def _type(step_id: str) -> Step:
    return Step(id=step_id, intent="type <x>", action=ActionKind.TYPE, param="x")


def test_compiler_binds_only_consequential_unlabeled_clicks() -> None:
    from openadapt_flow.compiler.compile import _attach_submit_bindings

    steps = [
        _type("step_000"),
        _type("step_001"),
        _click("step_002"),
        _type("step_003"),
        _click("step_004", risk="reversible"),
        _click("step_005", structural=StructuralLocator(selector="#save")),
        _click("step_006"),
        _click("step_007"),
    ]
    _attach_submit_bindings(
        steps,
        {
            "step_002": _raw(),
            "step_004": _raw(),
            "step_005": _raw(),
            "step_006": _raw(candidates=2),
            "step_007": {"v": 1, "text": "Jane"},
        },
    )
    by_id = {step.id: step for step in steps}

    bound = by_id["step_002"].submit_binding
    assert bound is not None
    assert bound.field_step_ids == ("step_000", "step_001")
    # A reversible click and a structurally located control are not bound.
    assert by_id["step_004"].submit_binding is None
    assert by_id["step_004"].submit_binding_unbound_reason is None
    assert by_id["step_005"].submit_binding is None
    assert by_id["step_005"].submit_binding_unbound_reason is None
    # Ambiguity and malformed evidence leave a reason, never a binding.
    assert by_id["step_006"].submit_binding is None
    assert "2 unlabeled controls" in (
        by_id["step_006"].submit_binding_unbound_reason or ""
    )
    assert by_id["step_007"].submit_binding is None
    assert "malformed" in (by_id["step_007"].submit_binding_unbound_reason or "")


def test_lint_reports_the_binding_and_the_unbound_click() -> None:
    bound = _click("save")
    bound.submit_binding = _binding()
    unbound = _click("other")
    unbound.submit_binding_unbound_reason = (
        "2 unlabeled controls follow the filled fields, so which one submits "
        "them is ambiguous"
    )
    report = lint_workflow(Workflow(name="claims", steps=[bound, unbound]))
    by_code = {finding.code: finding for finding in report.findings}

    assert by_code["submit_binding"].severity == "info"
    assert by_code["submit_binding"].step_id == "save"
    assert by_code["submit_binding_unbound"].severity == "error"
    assert "refuses this click" in by_code["submit_binding_unbound"].message


# -- replayer -----------------------------------------------------------------


class _BindingBackend(FakeBackend):
    """A fake DOM backend whose live binding check passes or refuses."""

    def __init__(self, refusal: str | None = None) -> None:
        super().__init__(viewport=(1280, 800))
        self.refusal = refusal
        self.bindings_checked: list[SubmitBinding] = []

    def submit_binding_at(self, x: int, y: int) -> dict[str, Any] | None:
        return None

    def arm_guarded_submit_coordinate(
        self, x: int, y: int, *, binding: SubmitBinding
    ) -> None:
        from openadapt_flow.backend import StructuralResolutionRefused

        self.bindings_checked.append(binding)
        if self.refusal is not None:
            raise StructuralResolutionRefused(self.refusal)
        self.arm_guarded_coordinate(x, y)


def _run(backend: FakeBackend, step: Step, tmp_path) -> Any:
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True, exist_ok=True)
    (bundle / "templates" / "save.png").write_bytes(make_png((56, 56)))
    vision = FakeVision()
    vision.template_results = [
        Match(point=(1228, 748), region=(1200, 720, 56, 56), confidence=0.99)
        for _ in range(4)
    ]
    return Replayer(backend, vision=vision).run(
        Workflow(name="claims", steps=[step]),
        bundle_dir=bundle,
        run_dir=tmp_path / "run",
    )


def test_a_bound_click_fires_only_after_the_live_binding_verifies(tmp_path) -> None:
    step = _click("save")
    step.submit_binding = _binding()
    backend = _BindingBackend()

    report = _run(backend, step, tmp_path)

    assert report.success is True, report.results[0].error
    assert backend.bindings_checked == [step.submit_binding]
    assert report.results[0].submit_binding_verified is True
    assert [action[0] for action in backend.actions] == ["click"]


def test_a_live_binding_mismatch_refuses_before_any_input(tmp_path) -> None:
    step = _click("save")
    step.submit_binding = _binding()
    backend = _BindingBackend(refusal="2 unlabeled controls follow the fields")

    report = _run(backend, step, tmp_path)
    result = report.results[0]

    assert report.success is False
    assert result.safety_halt is True
    assert result.submit_binding_verified is False
    assert "2 unlabeled controls" in (result.error or "")
    assert backend.actions == []


def test_a_backend_that_cannot_read_structure_refuses_a_bound_click(
    tmp_path,
) -> None:
    step = _click("save")
    step.submit_binding = _binding()
    backend = FakeBackend(viewport=(1280, 800))

    report = _run(backend, step, tmp_path)

    assert report.success is False
    assert "cannot read the page structure" in (report.results[0].error or "")
    assert backend.actions == []


def test_an_unbound_consequential_click_is_refused_even_without_identity(
    tmp_path,
) -> None:
    """Ambiguity at compile time is a refusal, never a blind click."""

    step = _click("save")
    step.submit_binding_unbound_reason = (
        "2 unlabeled controls follow the filled fields, so which one submits "
        "them is ambiguous"
    )
    backend = _BindingBackend()

    report = _run(backend, step, tmp_path)
    result = report.results[0]

    assert report.success is False
    assert result.safety_halt is True
    assert "could not be tied to the record" in (result.error or "")
    assert backend.actions == []
    assert backend.bindings_checked == []


def test_an_unbound_reversible_click_is_not_refused(tmp_path) -> None:
    """An operator who reclassified the click as reversible owns that call."""

    step = _click("save", risk="reversible")
    step.submit_binding_unbound_reason = "ambiguous"
    backend = _BindingBackend()

    report = _run(backend, step, tmp_path)

    assert report.success is True, report.results[0].error
    assert [action[0] for action in backend.actions] == ["click"]


# -- run gate -------------------------------------------------------------------


def test_the_run_gate_counts_a_submit_binding_as_the_saves_identity() -> None:
    """The gate agrees with the runtime: a bound icon-only Save is armed (its
    binding is checked live before the click); the same Save unbound is not."""

    from openadapt_flow.ir import Workflow
    from openadapt_flow.run_gate import _gate_identity

    def gate(step: Step) -> Any:
        workflow = Workflow(name="w", steps=[step])
        return _gate_identity(
            workflow,
            [step],
            require_current_risk_certification=False,
            certifying_policy=None,
        )

    bound = Step(
        id="save",
        intent="click recorded visual target",
        action=ActionKind.CLICK,
        risk="irreversible",
        identity_armed=False,
        anchor=_anchor(),
        submit_binding=_binding(),
    )
    unbound = bound.model_copy(update={"submit_binding": None})
    assert gate(bound).passed
    result = gate(unbound)
    assert not result.passed and result.offenders == ["save"]
