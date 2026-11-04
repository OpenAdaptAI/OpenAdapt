"""Backend-neutral admission of a repair's new clicks (element_identity).

These are rule tests over small stand-in backends. The real-app
qualification is tests/test_repair_every_backend_real_app.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

import pytest

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Step,
    StructuralHandle,
    Workflow,
)
from openadapt_flow.repair.element_identity import (  # noqa: I001
    _TREE_READS,
    BASIS_ELEMENT_AND_RECORD_CHECK,
    BASIS_RECORD_CHECK,
    BASIS_RECORD_IDENTITY,
    ClickRefused,
    ElementIdentity,
    admit_new_click,
    backend_kind,
    read_element_identity,
    record_check_cover,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr

WINDOW = "Chart - Okafor"
LATER = {
    "source": "ax",
    "role": "button",
    "name": "Later",
    "automation_id": None,
    "ancestry": [["AXSheet", "Update available"], ["AXWindow", WINDOW]],
    "window": WINDOW,
    "process": "Chart",
    "dialog_region": [100, 80, 300, 160],
}


class PixelBackend:
    def screenshot(self) -> bytes:
        return b""

    def click(self, x: int, y: int) -> None:
        pass


class TreeBackend(PixelBackend):
    """Accessibility reads, but no element identity yet (Windows today)."""

    def structural_locator_at(self, x: int, y: int) -> Any:
        return None


class AXBackend(PixelBackend):
    def __init__(self, identity: Any = LATER, matches: int = 1, region=None):
        self.identity = identity
        self.matches = matches
        self.region = region or (200, 200, 80, 30)

    def element_identity_at(self, x: int, y: int) -> Any:
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity

    def locate_structural(self, locator: Any) -> Optional[StructuralHandle]:
        if self.matches == 0:
            return None
        if self.matches > 1:
            raise StructuralResolutionRefused("candidate_count=2")
        x, y, w, h = self.region
        return StructuralHandle(
            point=(x + w // 2, y + h // 2),
            region=self.region,
            target_fingerprint="f" * 64,
            candidate_count=1,
        )


class DomBackend(PixelBackend):
    page = object()


POINT = (220, 210)


def _workflow(*, effects=True, armed=True, saves=True) -> Workflow:
    save_anchor = Anchor(
        template="templates/save.png",
        region=(10, 10, 40, 20),
        click_point=(30, 20),
        context_text="MRN 4408 Okafor, Philip" if armed else None,
    )
    steps = [
        Step(
            id="open",
            intent="click 'New note'",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="templates/open.png",
                region=(10, 10, 40, 20),
                click_point=(30, 20),
            ),
            risk="reversible",
        )
    ]
    if saves:
        steps.append(
            Step(
                id="save",
                intent="click 'Save note'",
                action=ActionKind.CLICK,
                anchor=save_anchor,
                risk="irreversible",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"note": ValueExpr(param="note")},
                    )
                ]
                if effects
                else [],
            )
        )
    return Workflow(name="chart", steps=steps, params={"note": "x"})


def _target(**kwargs) -> Any:
    values = {"effect_verifier": object(), "profile": "standard"}
    values.update(kwargs)
    return SimpleNamespace(**values)


def _no_record_identity() -> Any:
    raise RuntimeError("a new bridge step needs a record identity; none was read")


# -- ElementIdentity -------------------------------------------------------------


def test_identity_parses_and_binds_its_ancestry() -> None:
    identity = ElementIdentity.from_mapping(LATER)
    assert identity is not None
    assert identity.dialog() == ("AXSheet", "Update available")
    assert identity.ends_at_window()
    assert identity.dialog_region == (100, 80, 300, 160)
    locator = identity.locator()
    assert (locator.role, locator.name, locator.window_name) == (
        "button",
        "Later",
        WINDOW,
    )
    other = ElementIdentity.from_mapping(
        {**LATER, "ancestry": [["AXSheet", "Delete record?"], ["AXWindow", WINDOW]]}
    )
    assert other is not None and other.digest() != identity.digest()


@pytest.mark.parametrize(
    "change",
    [
        {"source": "pixels"},
        {"role": ""},
        {"window": None},
        {"ancestry": []},
        {"ancestry": [["AXWindow"]]},
        {"ancestry": [[None, None]]},
        {"ancestry": [["AXGroup", None]] * 40},
        {"dialog_region": [1, 2, 0, 4]},
        {"dialog_region": [1, 2, True, 4]},
    ],
)
def test_malformed_identity_is_none(change: dict) -> None:
    assert ElementIdentity.from_mapping({**LATER, **change}) is None
    assert ElementIdentity.from_mapping("button") is None


# -- backend kind ----------------------------------------------------------------


def test_backend_kind_comes_from_capabilities() -> None:
    assert backend_kind(DomBackend()) == "dom"
    assert backend_kind(AXBackend()) == "accessibility"
    assert backend_kind(PixelBackend()) == "pixel"


def test_tree_backend_without_identity_is_refused_not_pixel() -> None:
    with pytest.raises(ClickRefused, match="can't read element identity yet"):
        backend_kind(TreeBackend())


def test_real_backends_have_the_expected_kind() -> None:
    from openadapt_flow.backends.macos_backend import MacOSBackend
    from openadapt_flow.backends.rdp_backend import FreeRDPBackend
    from openadapt_flow.backends.remote_display import RemoteDisplayBackend

    from openadapt_flow.backends.linux_backend import LinuxBackend

    assert callable(getattr(MacOSBackend, "element_identity_at", None))
    assert callable(getattr(LinuxBackend, "element_identity_at", None))
    for backend in (FreeRDPBackend, RemoteDisplayBackend):
        for name in ("element_identity_at", "page", *_TREE_READS):
            assert not hasattr(backend, name), (backend, name)


# -- reading the element -------------------------------------------------------


@pytest.mark.parametrize(
    "identity, reason",
    [
        (None, "no element identity"),
        (RuntimeError("AX died"), "no element identity"),
        ({**LATER, "name": None}, "no name or id"),
        (
            {**LATER, "ancestry": [["AXWindow", "Other window"]]},
            "not inside the pinned window",
        ),
    ],
)
def test_unreadable_or_unscoped_element_is_refused(identity, reason) -> None:
    with pytest.raises(ClickRefused, match=reason):
        read_element_identity(AXBackend(identity), POINT)


def test_an_id_without_a_name_is_locatable() -> None:
    identity = read_element_identity(
        AXBackend({**LATER, "name": None, "automation_id": "later-button"}), POINT
    )
    assert identity.locator().automation_id == "later-button"


# -- record-check cover -------------------------------------------------------


def test_cover_holds_with_verifier_profile_effects_and_identity() -> None:
    assert record_check_cover(_workflow(), _target()) is None


@pytest.mark.parametrize(
    "workflow, target, reason",
    [
        (_workflow(), _target(effect_verifier=None), "no system-of-record verifier"),
        (_workflow(), _target(profile="demo"), "profile is 'demo'"),
        (_workflow(), SimpleNamespace(effect_verifier=object()), "profile is None"),
        (_workflow(saves=False), _target(), "no save"),
        (_workflow(effects=False), _target(), "no confirmed system-of-record"),
        (_workflow(armed=False), _target(), "no armed identity check"),
    ],
)
def test_cover_fails_closed(workflow, target, reason) -> None:
    why = record_check_cover(workflow, target)
    assert why is not None and reason in why


# -- admitting a new click -------------------------------------------------------


def _admit(backend: Any, *, record=_no_record_identity, covered: bool = True):
    return admit_new_click(
        backend,
        POINT,
        record_identity=record,
        cover=lambda: None if covered else "the run has no verifier",
    )


@pytest.mark.parametrize("backend", [DomBackend(), AXBackend(), PixelBackend()])
def test_a_verified_record_identity_admits_on_every_backend(backend) -> None:
    admission = _admit(backend, record=lambda: "template", covered=False)
    assert admission.basis == BASIS_RECORD_IDENTITY
    assert admission.identity_template == "template"
    assert not admission.needs_person


def test_browser_keeps_its_rule_even_with_a_record_check() -> None:
    with pytest.raises(ClickRefused, match="needs a record identity"):
        _admit(DomBackend(), covered=True)


def test_accessibility_click_needs_unique_element_and_record_check() -> None:
    admission = _admit(AXBackend())
    assert admission.basis == BASIS_ELEMENT_AND_RECORD_CHECK
    assert admission.needs_person
    assert admission.element is not None
    assert admission.structural(None).name == "Later"
    assert admission.identity_template is None


@pytest.mark.parametrize(
    "backend, reason",
    [
        (AXBackend(matches=2), "not unique"),
        (AXBackend(matches=0), "could not be found again"),
        (AXBackend(region=(0, 0, 10, 10)), "not the one under the click"),
        (AXBackend(None), "no element identity"),
    ],
)
def test_accessibility_click_fails_closed(backend, reason) -> None:
    with pytest.raises(ClickRefused, match=reason):
        _admit(backend)


def test_accessibility_click_without_record_check_is_refused() -> None:
    with pytest.raises(ClickRefused, match="none covers this workflow"):
        _admit(AXBackend(), covered=False)


def test_pixel_click_rests_on_the_record_check_only() -> None:
    admission = _admit(PixelBackend())
    assert admission.basis == BASIS_RECORD_CHECK
    assert admission.needs_person
    assert admission.element is None
    with pytest.raises(ClickRefused, match="on a pixel backend"):
        _admit(PixelBackend(), covered=False)


def test_tree_backend_without_identity_refuses_even_with_record_check() -> None:
    with pytest.raises(ClickRefused, match="can't read element identity yet"):
        _admit(TreeBackend())


# -- the new step's dialog band (self_repair._dialog_band) ---------------------


def _panel_png(with_text: bool = True) -> bytes:
    import cv2
    import numpy as np

    image = np.full((400, 600, 3), 255, np.uint8)
    cv2.rectangle(image, (100, 80), (500, 320), (60, 60, 60), 2)
    if with_text:
        cv2.putText(
            image, "Update available", (130, 140), 0, 1.0, (0, 0, 0), 2, cv2.LINE_AA
        )
    cv2.rectangle(image, (130, 240), (250, 280), (90, 90, 90), 2)
    return cv2.imencode(".png", image)[1].tobytes()


class _Vision:
    def __init__(self, lines):
        self.lines = lines

    def ocr(self, png):
        return self.lines


def _line(text, region):
    return SimpleNamespace(text=text, region=region, confidence=0.99)


def test_dialog_band_uses_the_tree_box_and_hashes_its_text() -> None:
    from openadapt_flow.repair.element_identity import ClickAdmission
    from openadapt_flow.repair.self_repair import _dialog_band

    admission = ClickAdmission(
        BASIS_ELEMENT_AND_RECORD_CHECK, element=ElementIdentity.from_mapping(LATER)
    )
    vision = _Vision(
        [
            _line("Update available", (120, 100, 200, 30)),
            _line("Version 2.1 is ready", (120, 140, 200, 24)),
            _line("MRN RC79284 Okafor", (10, 10, 80, 20)),  # outside the dialog
        ]
    )
    template, region, crop = _dialog_band(
        _workflow(), admission, _panel_png(), (190, 200), vision
    )
    assert region == (100, 80, 300, 160)
    assert crop.startswith(b"\x89PNG")
    assert template is not None and template.tokens
    assert template.salt


def test_dialog_band_finds_the_drawn_box_on_pixels() -> None:
    from openadapt_flow.repair.element_identity import ClickAdmission
    from openadapt_flow.repair.self_repair import _dialog_band

    vision = _Vision([_line("Update available", (130, 110, 230, 34))])
    _template, region, _crop = _dialog_band(
        _workflow(),
        ClickAdmission(BASIS_RECORD_CHECK),
        _panel_png(),
        (190, 260),
        vision,
    )
    x, y, w, h = region
    assert x <= 190 < x + w and y <= 260 < y + h and w < 600


@pytest.mark.parametrize(
    "png, point, lines, reason",
    [
        (None, (190, 260), [], "no dialog was found"),
        ("panel", (190, 260), [], "no readable text"),
    ],
)
def test_dialog_band_fails_closed(png, point, lines, reason) -> None:
    import cv2
    import numpy as np

    from openadapt_flow.repair.element_identity import ClickAdmission
    from openadapt_flow.repair.self_repair import SelfRepairRefused, _dialog_band

    image = (
        _panel_png(with_text=False)
        if png == "panel"
        else cv2.imencode(".png", np.full((400, 600, 3), 255, np.uint8))[1].tobytes()
    )
    with pytest.raises(SelfRepairRefused, match=reason):
        _dialog_band(
            _workflow(),
            ClickAdmission(BASIS_RECORD_CHECK),
            image,
            point,
            _Vision(lines),
        )


def test_dialog_band_refuses_a_click_outside_the_tree_box() -> None:
    from openadapt_flow.repair.element_identity import ClickAdmission
    from openadapt_flow.repair.self_repair import SelfRepairRefused, _dialog_band

    admission = ClickAdmission(
        BASIS_ELEMENT_AND_RECORD_CHECK, element=ElementIdentity.from_mapping(LATER)
    )
    with pytest.raises(SelfRepairRefused, match="outside the dialog"):
        _dialog_band(_workflow(), admission, _panel_png(), (5, 5), _Vision([]))


# -- a person approves any fix that rests on the record check ------------------


def test_patch_needs_a_person_when_a_click_rests_on_the_record_check() -> None:
    from openadapt_flow.repair.element_identity import ClickAdmission
    from openadapt_flow.repair.self_repair import _Patch

    def patch(*bases: str) -> _Patch:
        return _Patch(
            attempt=1,
            step=_workflow().steps[0],
            workflow=_workflow(),
            bundle_dir=None,  # type: ignore[arg-type]
            evidence_dir=None,  # type: ignore[arg-type]
            candidate=None,  # type: ignore[arg-type]
            target_region=(0, 0, 1, 1),
            admissions={f"s{i}": ClickAdmission(b) for i, b in enumerate(bases)},
        )

    assert not patch().needs_person
    assert not patch(BASIS_RECORD_IDENTITY).needs_person
    assert patch(BASIS_RECORD_IDENTITY, BASIS_RECORD_CHECK).needs_person
    assert patch(BASIS_ELEMENT_AND_RECORD_CHECK).needs_person


def test_standard_change_is_never_applied_to_a_record_check_fix() -> None:
    import inspect

    from openadapt_flow.repair.self_repair import SelfRepair

    source = inspect.getsource(SelfRepair._govern_and_resume)
    assert "self.standard_changes is not None and not patch.needs_person" in source


# -- the repair hold answers element_identity ------------------------------------


def test_hold_returns_a_validated_element_identity() -> None:
    from openadapt_flow.repair.hold import _handle

    reply, done = _handle(AXBackend(), {"op": "element_identity", "x": 1, "y": 2})
    assert not done and reply["ok"]
    assert reply["identity"]["name"] == "Later"
    assert reply["identity"]["dialog_region"] == [100, 80, 300, 160]
    for backend in (PixelBackend(), AXBackend({**LATER, "ancestry": []})):
        reply, _ = _handle(backend, {"op": "element_identity", "x": 1, "y": 2})
        assert reply == {"ok": True, "identity": None}
    reply, _ = _handle(AXBackend(), {"op": "element_identity", "x": "1", "y": 2})
    assert not reply["ok"]


def test_agent_teach_takes_the_dialog_box_from_the_identity() -> None:
    from openadapt_flow.agent.repair import _element_identity

    class Screen:
        def __init__(self, value):
            self.value = value

        def element_identity(self, x, y):
            return self.value

    assert _element_identity(Screen(LATER), 1, 2)["dialog_region"] == [
        100,
        80,
        300,
        160,
    ]
    assert _element_identity(Screen({**LATER, "window": None}), 1, 2) is None
    assert _element_identity(object(), 1, 2) is None


# -- Windows UI Automation's shape (PR #1707) ----------------------------------

UIA_OK = {
    "source": "uia",
    "role": "button",
    "name": "OK",
    "automation_id": None,
    "ancestry": [[None, "Update available"], [None, "Patient chart"]],
    "window": "Patient chart",
    "process": None,
}


class UIABackend(AXBackend):
    """Windows scopes its locator by the innermost window, not the owner."""

    def __init__(self, locator: Any = "default", **kwargs: Any) -> None:
        super().__init__(UIA_OK, **kwargs)
        self._locator = locator

    def structural_locator_at(self, x: int, y: int) -> Any:
        from openadapt_flow.ir import StructuralLocator

        if self._locator == "default":
            return StructuralLocator(
                role="button", name="OK", window_name="Update available"
            )
        return self._locator


def test_uia_identity_with_unmapped_ancestor_roles_is_admitted() -> None:
    identity = ElementIdentity.from_mapping(UIA_OK)
    assert identity is not None
    assert identity.ancestry == (("", "Update available"), ("", "Patient chart"))
    assert identity.process is None
    admission = _admit(UIABackend())
    assert admission.basis == BASIS_ELEMENT_AND_RECORD_CHECK
    # The backend's own locator (innermost window) is what replay stores.
    assert admission.structural(None).window_name == "Update available"


def test_uia_id_without_a_role_is_locatable() -> None:
    identity = ElementIdentity.from_mapping(
        {**UIA_OK, "role": None, "name": None, "automation_id": "okButton"}
    )
    assert identity is not None and identity.locatable()
    assert ElementIdentity.from_mapping({**UIA_OK, "role": None}) is None
    assert (
        ElementIdentity.from_mapping(
            {**UIA_OK, "ancestry": [[None, None], [None, "Patient chart"]]}
        )
        is None
    )


@pytest.mark.parametrize(
    "locator, reason",
    [
        (None, "gives no locator"),
        ("cancel", "names a different element"),
    ],
)
def test_backend_locator_must_name_the_identified_element(locator, reason) -> None:
    from openadapt_flow.ir import StructuralLocator

    if locator == "cancel":
        locator = StructuralLocator(role="button", name="Cancel", window_name="x")
    with pytest.raises(ClickRefused, match=reason):
        _admit(UIABackend(locator))


# -- same_fields on a screen with no form to read ------------------------------


def test_glued_ocr_counts_for_a_long_multiword_value_only() -> None:
    from openadapt_flow.repair.self_repair import _glued_on_a_line

    lines = ["REPAIREDRUNFOLLOWUPINTHREEWEEKS", "MRN RC79284 5"]
    assert _glued_on_a_line("Repaired run follow up in three weeks", lines)
    # A run of whole words may also be partly glued.
    assert _glued_on_a_line(
        "follow up in three weeks", ["note: FOLLOWUP in threeweeks"]
    )
    assert not _glued_on_a_line("follow up in four weeks", lines)
    assert not _glued_on_a_line("5", lines)  # one word: the word rule applies
    assert not _glued_on_a_line("a b", lines)  # too short to glue-match


@pytest.mark.parametrize(
    "typed, shown",
    [
        ("John Smith", "JOHNSMITHSON"),
        ("Ann Lee", "Dr Joann Leeds"),
        ("follow up", "NOFOLLOWUPNEEDED"),
        ("5 mg bid", "25 mg bid"),
        ("5 mg bid", "25mgbid"),
    ],
)
def test_glued_match_never_takes_part_of_a_word(typed, shown) -> None:
    from openadapt_flow.repair.self_repair import _glued_on_a_line

    assert not _glued_on_a_line(typed, [shown])
