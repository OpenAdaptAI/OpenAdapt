"""The ``focus_on`` postcondition: a key that moves the focus is checked.

A Tab onto a small icon button, or an ArrowDown that highlights an option,
changes nothing the compiler can see, so the step used to carry no check and
a strict policy refused it as vacuous. ``focus_on`` checks who has the focus
after the key. It counts only when the element has a specific identity (a
testid, an id, or a role with a non-empty accessible name) that no other
element on the page shares; anything weaker is still vacuous. At replay the
focused element must have that identity, and be the only one with it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from openadapt_flow.compiler.compile import (
    _focus_postcondition,
    _structural_postconditions,
)
from openadapt_flow.ir import (
    ActionKind,
    Postcondition,
    PostconditionKind,
    Step,
    focus_identity_is_specific,
)
from openadapt_flow.policy import is_vacuous
from openadapt_flow.runtime.replayer import Replayer


def _focus(**over):
    seen = {
        "none": False,
        "tag": "button",
        "testid": None,
        "testid_count": 0,
        "id": None,
        "id_count": 0,
        "role": "button",
        "name": "Add Telecom",
        "role_name_count": 1,
        "active_option": False,
    }
    seen.update(over)
    return seen


BEFORE = _focus(tag="input", role="textbox", name="Value")


# -- what the compiler mines -------------------------------------------------


def test_a_unique_testid_wins():
    pc = _focus_postcondition(BEFORE, _focus(testid="add", testid_count=1))
    assert pc.focus_by == "testid" and pc.focus_value == "add"


def test_a_unique_role_and_name():
    pc = _focus_postcondition(BEFORE, _focus())
    assert (pc.focus_by, pc.focus_role, pc.focus_name) == (
        "role_name",
        "button",
        "Add Telecom",
    )
    assert focus_identity_is_specific(pc)


@pytest.mark.parametrize(
    "after",
    [
        _focus(role="button", name=None),
        _focus(role=None, name=None),
        _focus(role_name_count=2),
        _focus(testid="remove", testid_count=3, role_name_count=2),
    ],
    ids=["role without a name", "tag only", "shared role and name", "shared testid"],
)
def test_no_specific_identity_means_no_check(after):
    assert _focus_postcondition(BEFORE, after) is None


def test_a_key_that_moved_nothing_proves_nothing():
    assert _focus_postcondition(_focus(), _focus()) is None
    assert _focus_postcondition(_focus(), {"none": True}) is None


def test_an_option_made_active_on_the_same_control():
    combo = _focus(tag="input", role="combobox", name="Gender")
    pc = _focus_postcondition(combo, {**combo, "active_option": True})
    assert pc.focus_by == "role_name" and pc.focus_active_option is True


def test_only_key_steps_without_visual_checks_get_it():
    event = {
        "kind": "key",
        "key": "Tab",
        "focus_before": BEFORE,
        "focus_after": _focus(),
    }
    assert [p.kind for p in _structural_postconditions(event)] == [
        PostconditionKind.FOCUS_ON
    ]
    assert _structural_postconditions({**event, "kind": "click"}) == []


# -- the policy ---------------------------------------------------------------


def _key_step(expect):
    return Step(
        id="k", intent="press Tab", action=ActionKind.KEY, key="Tab", expect=expect
    )


def test_a_specific_focus_check_is_not_vacuous():
    pc = _focus_postcondition(BEFORE, _focus())
    assert is_vacuous(_key_step([])) is True
    assert is_vacuous(_key_step([pc])) is False


def test_a_focus_check_without_a_specific_identity_stays_vacuous():
    for pc in (
        Postcondition(
            kind=PostconditionKind.FOCUS_ON, focus_by="role_name", focus_role="button"
        ),
        Postcondition(kind=PostconditionKind.FOCUS_ON),
        Postcondition(kind=PostconditionKind.FOCUS_ON, focus_by="testid"),
    ):
        assert not focus_identity_is_specific(pc)
        assert is_vacuous(_key_step([pc])) is True


def test_existing_postconditions_serialize_as_before():
    dumped = Postcondition(
        kind=PostconditionKind.TEXT_PRESENT, text="Saved"
    ).model_dump()
    assert not any(k.startswith("focus_") for k in dumped)


# -- the check at replay, in a real browser --------------------------------------

PAGE = (
    "data:text/html,<input placeholder=Value>"
    "<button>Remove</button><button>Remove</button>"
    "<button>Add Telecom</button><button>Add Address</button>"
    "<input role=combobox aria-label=Gender aria-controls=box>"
    "<div id=box role=listbox><div id=o1 role=option>Female</div></div>"
)


@pytest.fixture
def backend():
    pytest.importorskip("playwright")
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    try:
        backend, close = PlaywrightBackend.launch(PAGE)
    except Exception as exc:  # noqa: BLE001 - no browser on this machine
        pytest.skip(f"chromium unavailable: {exc}")
    yield backend
    close()


def _held(backend, pc):
    return Replayer._focus_on_held(SimpleNamespace(backend=backend), pc)


ADD_TELECOM = Postcondition(
    kind=PostconditionKind.FOCUS_ON,
    focus_by="role_name",
    focus_role="button",
    focus_name="Add Telecom",
)


def test_it_passes_on_the_recorded_focus(backend):
    backend.page.get_by_role("button", name="Add Telecom").focus()
    assert _held(backend, ADD_TELECOM) is True


def test_it_halts_on_another_button_with_the_same_role(backend):
    backend.page.get_by_role("button", name="Add Address").focus()
    assert _held(backend, ADD_TELECOM) is False


def test_it_halts_when_the_identity_is_not_unique(backend):
    backend.page.get_by_role("button", name="Remove").first.focus()
    remove = ADD_TELECOM.model_copy(update={"focus_name": "Remove"})
    assert _held(backend, remove) is False


def test_it_fails_without_a_specific_identity(backend):
    backend.page.get_by_role("button", name="Add Telecom").focus()
    vague = Postcondition(
        kind=PostconditionKind.FOCUS_ON, focus_by="role_name", focus_role="button"
    )
    assert _held(backend, vague) is False


def test_an_active_option_is_checked(backend):
    combo = Postcondition(
        kind=PostconditionKind.FOCUS_ON,
        focus_by="role_name",
        focus_role="combobox",
        focus_name="Gender",
        focus_active_option=True,
    )
    page = backend.page
    page.get_by_role("combobox", name="Gender").focus()
    assert _held(backend, combo) is False
    page.evaluate("document.getElementById('o1').setAttribute('aria-selected', 'true')")
    assert _held(backend, combo) is True


def test_the_recorder_keeps_the_focus_around_a_key(backend, tmp_path):
    from openadapt_flow.recorder import Recorder

    backend.page.get_by_role("button", name="Add Telecom").focus()
    recorder = Recorder(backend, tmp_path / "rec", settle_timeout_s=2.0)
    recorder.press("Tab")
    recorder.finish()
    event = json.loads((tmp_path / "rec" / "events.jsonl").read_text().splitlines()[-1])
    assert event["focus_before"]["name"] == "Add Telecom"
    assert event["focus_after"]["name"] == "Add Address"
