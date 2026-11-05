"""Locale, date order and keyboard layout invariants (#1897)."""

from __future__ import annotations

import pytest

from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.locale_invariants import (
    LocaleInvariantMismatch,
    LocaleInvariants,
    check_locale_invariants,
    enforce_workflow_locale_invariants,
    observe_locale_invariants,
    worklist_date_needs_explicit_format,
)


def test_matching_invariants_pass() -> None:
    recorded = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="us")
    current = LocaleInvariants(
        locale="en_US.UTF-8", date_order="mdy", keyboard_layout="us"
    )
    assert check_locale_invariants(recorded, current).ok


def test_date_order_mismatch_refuses() -> None:
    recorded = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="us")
    current = LocaleInvariants(locale="en_US", date_order="dmy", keyboard_layout="us")
    result = check_locale_invariants(recorded, current)
    assert not result.ok
    assert result.mismatched == ("date_order",)
    with pytest.raises(LocaleInvariantMismatch):
        result.raise_if_refused()


def test_keyboard_mismatch_refuses_when_keystrokes_used() -> None:
    recorded = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="us")
    current = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="fr")
    assert not check_locale_invariants(recorded, current).ok
    # Semantic value-setting may ignore keyboard layout.
    assert check_locale_invariants(recorded, current, keystroke_steps=False).ok


def test_locale_mismatch_refuses() -> None:
    recorded = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="us")
    current = LocaleInvariants(locale="fr_CA", date_order="mdy", keyboard_layout="us")
    result = check_locale_invariants(recorded, current)
    assert result.mismatched == ("locale",)


def test_worklist_ambiguous_numeric_date_needs_explicit_format() -> None:
    assert worklist_date_needs_explicit_format("03/04/2026")
    assert not worklist_date_needs_explicit_format("2026-03-04")
    assert not worklist_date_needs_explicit_format("14/03/2026")  # day > 12
    assert not worklist_date_needs_explicit_format("03 March 2026")
    assert not worklist_date_needs_explicit_format("03/03/2026")  # equal parts


def test_workflow_field_omitted_when_unset() -> None:
    import json

    wf = Workflow(name="demo")
    payload = json.loads(wf.model_dump_json())
    assert "locale_invariants" not in payload
    loaded = Workflow.model_validate_json(wf.model_dump_json())
    assert loaded.locale_invariants is None


def test_workflow_round_trips_locale_invariants() -> None:
    inv = LocaleInvariants(locale="fr_CA", date_order="dmy", keyboard_layout="ca")
    wf = Workflow(name="demo", locale_invariants=inv)
    loaded = Workflow.model_validate_json(wf.model_dump_json())
    assert loaded.locale_invariants is not None
    assert loaded.locale_invariants.date_order == "dmy"
    assert loaded.locale_invariants.keyboard_layout == "ca"


def test_enforce_refuses_mismatched_workflow() -> None:
    wf = Workflow(
        name="demo",
        locale_invariants=LocaleInvariants(
            locale="de_DE", date_order="dmy", keyboard_layout="de"
        ),
        steps=[
            Step(id="s1", action=ActionKind.TYPE, intent="type a date"),
        ],
    )
    current = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="us")
    with pytest.raises(LocaleInvariantMismatch) as excinfo:
        enforce_workflow_locale_invariants(wf, current=current)
    assert "date_order" in excinfo.value.mismatched
    assert "locale" in excinfo.value.mismatched


def test_observe_accepts_explicit_overrides() -> None:
    observed = observe_locale_invariants(
        locale_name="fr_FR", date_order="dmy", keyboard_layout="fr"
    )
    assert observed.locale.startswith("fr_fr")
    assert observed.date_order == "dmy"
    assert observed.keyboard_layout == "fr"
