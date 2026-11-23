"""Choice folding preserves identity and independent recorded actions."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openadapt_flow.compiler.compile import _choice_contract, _fold_choices
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    DialogResponse,
    IdentityTemplate,
    Postcondition,
    PostconditionKind,
    Step,
    StructuralLocator,
    TokenTemplate,
)


def _click(name: str, **anchor_changes) -> Step:
    return Step(
        id=name,
        intent="open choices",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=f"templates/{name}.png",
            structural=StructuralLocator(selector="#record-choice"),
            region=(10, 20, 80, 30),
            click_point=(50, 35),
            **anchor_changes,
        ),
        identity_armed=True,
        identifier_crop_missing_reason="original crop provenance",
    )


def _entry(step: Step, event=None):
    return step, b"before", b"after", {"i": int(step.id[-1]), **(event or {})}


def _choice(kind="listed_option", **extra):
    return {
        "kind": kind,
        "label": "Alpha Care",
        "value": "ALPHA",
        "readback": "control_text" if kind == "listed_option" else "select",
        "shape": {"role": "option", "tag": "li", "classes": []},
        **extra,
    }


def _fold(entries):
    aliases, notes, cache = {}, [], {}
    return _fold_choices(entries, aliases, notes, cache), aliases, notes


@pytest.mark.parametrize("kind", ["native_select", "listed_option"])
@pytest.mark.parametrize("evidence", ["pixels", "ocr", "template", "structured"])
@given(
    selector=st.sampled_from(
        ["#record-choice", "table tr:first-child select", "[role=combobox]"]
    )
)
def test_choice_folding_preserves_every_original_identity_tier(
    kind, evidence, selector
):
    identity = {
        "pixels": {
            "identifier_crop": "templates/identifiers/open.png",
            "identifier_region": (0, 10, 120, 20),
        },
        "ocr": {"context_text": "Synthetic record AB-001"},
        "template": {
            "identity_template": IdentityTemplate(
                salt="synthetic",
                band_len=1,
                tokens=[TokenTemplate(c="a", r="b", n=5, idsh=True)],
            ),
        },
        "structured": {
            "structured_identity": "Synthetic record AB-001",
            "record_key": "AB-001",
        },
    }[evidence]
    opened = _click("step_0", **identity)
    opened.anchor.structural = StructuralLocator(selector=selector)
    option = _click("step_1")
    option.anchor.structural = opened.anchor.structural.model_copy(deep=True)
    out, aliases, _ = _fold([_entry(opened), _entry(option, {"choice": _choice(kind)})])
    assert len(out) == 1
    selected = out[0][0]
    assert selected.action is ActionKind.SELECT_OPTION
    assert selected.anchor == opened.anchor
    assert selected.anchor is not opened.anchor
    assert selected.identity_armed == opened.identity_armed
    assert selected.identity_unarmed_reason == opened.identity_unarmed_reason
    assert (
        selected.identifier_crop_missing_reason == opened.identifier_crop_missing_reason
    )
    assert aliases == {opened.id: selected.id}


@pytest.mark.parametrize(
    "raw_value, field_value, expected",
    [
        ("EXPLICIT", "FALLBACK", "EXPLICIT"),
        (None, "FALLBACK", "FALLBACK"),
        (12, "FALLBACK", "FALLBACK"),
        (None, 12, None),
        (None, False, None),
        ("", "FALLBACK", ""),
    ],
)
def test_listed_contract_retains_exact_demonstrated_commit_field(
    raw_value, field_value, expected
):
    contract = _choice_contract(
        _choice(value=raw_value, field="department", field_value=field_value), None
    )
    assert contract is not None
    assert contract.commit_field == "department"
    assert contract.value == expected
    assert contract.label == "Alpha Care"


@pytest.mark.parametrize("field", [None, 9, False, {}])
def test_nonstring_commit_field_is_not_invented(field):
    contract = _choice_contract(_choice(field=field, field_value="CODE"), None)
    assert contract is not None
    assert contract.commit_field is None


def test_native_contract_does_not_infer_value_from_listed_field_readback():
    contract = _choice_contract(
        _choice("native_select", value=None, field="field", field_value="CODE"), None
    )
    assert contract is not None
    assert contract.value is None
    assert contract.commit_field is None


@pytest.mark.parametrize("kind", ["native_select", "listed_option"])
@pytest.mark.parametrize("position", ["opening", "option"])
@pytest.mark.parametrize(
    "authority", ["dialog", "effect", "irreversible", "navigation"]
)
def test_choice_folding_keeps_independent_authority_at_each_click(
    kind, position, authority
):
    from openadapt_flow.runtime.effects.effect import Effect

    opened, option = _click("step_0"), _click("step_1")
    changes = {
        "dialog": {
            "dialog": DialogResponse(message="Confirm this write", response="accept")
        },
        "effect": {
            "effects": [Effect(kind="record_written", match={"id": "synthetic"})]
        },
        "irreversible": {"risk": "irreversible"},
        "navigation": {"expect": [Postcondition(kind=PostconditionKind.URL_CHANGED)]},
    }[authority]
    if position == "opening":
        opened = Step.model_validate(opened.model_copy(update=changes).model_dump())
    else:
        option = Step.model_validate(option.model_copy(update=changes).model_dump())
    entries = [_entry(opened), _entry(option, {"choice": _choice(kind)})]
    out, aliases, _ = _fold(entries)
    assert out[0] == entries[0]
    assert aliases == {}
    if kind == "listed_option" or position == "option":
        assert out == entries
    else:
        # Native change evidence can stand alone, but cannot erase the
        # earlier click's independent authority.
        assert out[1][0].action is ActionKind.SELECT_OPTION


@pytest.mark.parametrize("key", ["Enter", "ArrowDown", "ArrowUp", "Escape"])
def test_native_choice_does_not_absorb_keys_without_a_matching_opening_control(key):
    opened = _click("step_0")
    opened.anchor.structural = StructuralLocator(selector="#unrelated")
    pressed = Step(id="step_1", intent="prior key", action=ActionKind.KEY, key=key)
    option = _click("step_2")
    entries = [
        _entry(opened),
        _entry(pressed),
        _entry(option, {"choice": _choice("native_select")}),
    ]
    out, aliases, _ = _fold(entries)
    assert out[:2] == entries[:2]
    assert out[2][0].action is ActionKind.SELECT_OPTION
    assert aliases == {}


def test_native_choice_keeps_a_key_with_its_own_dialog():
    opened = _click("step_0")
    pressed = Step(
        id="step_1",
        intent="confirm",
        action=ActionKind.KEY,
        key="Enter",
        dialog=DialogResponse(message="Confirm this write", response="accept"),
    )
    option = _click("step_2")
    entries = [
        _entry(opened),
        _entry(pressed),
        _entry(option, {"choice": _choice("native_select")}),
    ]
    out, aliases, _ = _fold(entries)
    assert out[:2] == entries[:2]
    assert out[2][0].action is ActionKind.SELECT_OPTION
    assert aliases == {}


def _search_entries(auxiliary=False):
    opened = _click("step_0", context_text="Synthetic record AB-001")
    typed = Step(
        id="step_2", intent="filter choices", action=ActionKind.TYPE, text="Care"
    )
    option = _click("step_3")
    entries = [_entry(opened, {"choice_gesture": "doc:gesture:1"})]
    if auxiliary:
        search = _click("step_1")
        search.anchor.structural = StructuralLocator(selector="#popup-search")
        entries.append(
            _entry(
                search,
                {
                    "choice_search_gesture": "doc:gesture:1",
                    "input_session": "doc:input:2",
                },
            )
        )
    entries.extend(
        [
            _entry(
                typed,
                {"choice_gesture": "doc:gesture:1", "input_session": "doc:input:2"},
            ),
            _entry(
                option,
                {
                    "choice": _choice(
                        opening_gesture="doc:gesture:1",
                        search_input_session="doc:input:2",
                    )
                },
            ),
        ]
    )
    return entries


@pytest.mark.parametrize("auxiliary", [False, True])
def test_bound_search_folds_from_the_original_control_and_baseline(auxiliary):
    entries = _search_entries(auxiliary)
    out, aliases, _ = _fold(entries)
    assert len(out) == 1
    selected = out[0][0]
    assert selected.action is ActionKind.SELECT_OPTION
    assert selected.anchor == entries[0][0].anchor
    assert selected.choice.query == "words"
    assert selected.choice.query_words == (1, 1)
    assert out[0][1] == entries[0][1]
    assert out[0][2] == entries[-1][2]
    assert aliases == {entry[0].id: selected.id for entry in entries[:-1]}


@pytest.mark.parametrize("auxiliary", [False, True])
@pytest.mark.parametrize(
    "broken",
    [
        "opening",
        "typed_gesture",
        "input_session",
        "final_gesture",
        "final_session",
        "absent",
    ],
)
@given(
    unrelated=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=20)
)
def test_matching_label_words_never_replace_missing_or_mismatched_search_proof(
    auxiliary, broken, unrelated
):
    entries = _search_entries(auxiliary)
    if broken == "opening":
        entries[0][3]["choice_gesture"] = unrelated
    elif broken == "typed_gesture":
        entries[-2][3]["choice_gesture"] = unrelated
    elif broken == "input_session":
        entries[-2][3]["input_session"] = unrelated
    elif broken == "final_gesture":
        entries[-1][3]["choice"]["opening_gesture"] = unrelated
    elif broken == "final_session":
        entries[-1][3]["choice"]["search_input_session"] = unrelated
    else:
        for entry in entries:
            entry[3].pop("choice_gesture", None)
            entry[3].pop("choice_search_gesture", None)
            entry[3].pop("input_session", None)
        entries[-1][3]["choice"].pop("opening_gesture", None)
        entries[-1][3]["choice"].pop("search_input_session", None)
    out, aliases, notes = _fold(entries)
    assert out == entries
    assert aliases == {}
    assert notes[-1]["change"] == "choice_not_folded"


@pytest.mark.parametrize("field", ["choice_search_gesture", "input_session"])
def test_auxiliary_search_click_requires_its_own_exact_link(field):
    entries = _search_entries(True)
    entries[1][3][field] = "another-session"
    out, aliases, _ = _fold(entries)
    assert out == entries
    assert aliases == {}


@pytest.mark.parametrize("position", ["typed", "search_click"])
def test_linked_search_does_not_absorb_independent_dialog_authority(position):
    entries = _search_entries(True)
    index = -2 if position == "typed" else 1
    old = entries[index]
    protected = Step.model_validate(
        old[0]
        .model_copy(
            update={
                "dialog": DialogResponse(
                    message="Confirm independent action", response="accept"
                ),
            }
        )
        .model_dump()
    )
    entries[index] = (protected, *old[1:])
    out, aliases, _ = _fold(entries)
    assert out == entries
    assert aliases == {}


def test_unrelated_typing_keeps_its_action_even_if_its_geometry_and_label_match():
    entries = _search_entries()
    entries[-2][3].update(field_rect=[10, 20, 80, 30], field_label="Care")
    entries[-2][3].pop("choice_gesture")
    out, aliases, _ = _fold(entries)
    assert out == entries
    assert aliases == {}


@pytest.mark.parametrize("kind", ["native_select", "listed_option"])
def test_recorded_choice_cannot_claim_a_different_opening_gesture(kind):
    opened, option = _click("step_0"), _click("step_1")
    entries = [
        _entry(opened, {"choice_gesture": "doc:gesture:1"}),
        _entry(option, {"choice": _choice(kind, opening_gesture="doc:gesture:2")}),
    ]
    out, aliases, _ = _fold(entries)
    assert out[0] == entries[0]
    assert aliases == {}
    if kind == "listed_option":
        assert out == entries
