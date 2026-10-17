"""Workflow cards: typed inputs from Flow, words from the operator, no PHI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_agent.bundles import load_workflow_info
from openadapt_agent.cards import (
    CARD_FILENAME,
    card_for_bundle,
    coerce_inputs,
    input_problems,
    inputs_schema,
)


def typed_bundle(tmp_path: Path) -> Path:
    from openadapt_flow.ir import ActionKind, ParamKind, ParamSpec, Step, Workflow

    bundle = tmp_path / "bundles" / "referral"
    Workflow(
        name="Referral entry",
        params={
            "patient_mrn": "1234567",
            "specialty": "Cardiology",
            "referral_date": "2026-01-02",
            "urgent": "false",
        },
        param_specs={
            "patient_mrn": ParamSpec(name="patient_mrn", example="1234567"),
            "specialty": ParamSpec(
                name="specialty",
                type=ParamKind.ENUM,
                example="Cardiology",
                choices=["Cardiology", "Dermatology"],
            ),
            "referral_date": ParamSpec(
                name="referral_date", type=ParamKind.DATE, example="2026-01-02"
            ),
            # BOOLEAN arrived after Flow 1.26; NUMBER covers the floor.
            "urgent": ParamSpec(
                name="urgent",
                type=getattr(ParamKind, "BOOLEAN", ParamKind.NUMBER),
                example=False if hasattr(ParamKind, "BOOLEAN") else "0",
            ),
        },
        steps=[
            Step(id="s1", intent="Open the chart for 1234567", action=ActionKind.CLICK),
        ],
    ).save(bundle)
    return bundle


def write_card(bundle: Path, card: dict) -> None:
    (bundle / CARD_FILENAME).write_text(json.dumps(card), encoding="utf-8")


def test_default_card_uses_flow_types_without_examples(tmp_path):
    info = load_workflow_info(typed_bundle(tmp_path))
    card = card_for_bundle(info, default_name="workflow_abc")
    schema = inputs_schema(card)
    assert card.name == "workflow_abc"
    assert card.authored is False
    assert schema["properties"]["specialty"]["enum"] == ["Cardiology", "Dermatology"]
    assert schema["properties"]["referral_date"]["format"] == "date"
    assert schema["properties"]["urgent"]["type"] in {"boolean", "number"}
    assert schema["required"] == ["patient_mrn", "referral_date", "specialty", "urgent"]
    serialized = json.dumps(card.projection())
    for recorded in ("1234567", "2026-01-02", "Open the chart"):
        assert recorded not in serialized


def test_operator_card_names_and_describes_the_workflow(tmp_path):
    bundle = typed_bundle(tmp_path)
    write_card(
        bundle,
        {
            "name": "enter_referral",
            "purpose": "Create an outgoing referral for an existing patient in the clinic EMR.",
            "done_means": "The referral appears in the EMR's referral report.",
            "inputs": {
                "patient_mrn": {"description": "Clinic MRN.", "pattern": "^[0-9]{6,10}$"},
                "specialty": {"enum": ["Cardiology"]},
            },
        },
    )
    card = card_for_bundle(load_workflow_info(bundle), default_name="workflow_abc")
    assert card.authored is True
    assert card.name == "enter_referral"
    schema = inputs_schema(card)
    assert schema["properties"]["patient_mrn"]["pattern"] == "^[0-9]{6,10}$"
    assert schema["properties"]["specialty"]["enum"] == ["Cardiology"]


def test_sidecar_card_does_not_break_flow_bundle_loading(tmp_path):
    bundle = typed_bundle(tmp_path)
    write_card(bundle, {"name": "enter_referral", "purpose": "Enter a referral."})
    assert load_workflow_info(bundle).ok


@pytest.mark.parametrize(
    "card",
    [
        {"name": "Enter Referral"},
        {"purpose": "Referral for MRN 8812345."},
        {"purpose": "Email jane.roe@example.com when done."},
        {"inputs": {"not_a_param": {"description": "x"}}},
        {"inputs": {"specialty": {"enum": ["Oncology"]}}},
        {"inputs": {"patient_mrn": {"pattern": "("}}},
        {"unexpected": True},
    ],
)
def test_invalid_card_falls_back_to_default_without_echoing(tmp_path, caplog, card):
    bundle = typed_bundle(tmp_path)
    write_card(bundle, card)
    result = card_for_bundle(load_workflow_info(bundle), default_name="workflow_abc")
    assert result.authored is False
    assert result.name == "workflow_abc"
    for text in ("8812345", "jane.roe", "Oncology"):
        assert text not in caplog.text


def test_input_problems_use_closed_words_and_never_echo_values(tmp_path):
    card = card_for_bundle(load_workflow_info(typed_bundle(tmp_path)), default_name="w_x")
    problems = input_problems(
        card,
        {
            "patient_mrn": 12,
            "specialty": "Oncology",
            "referral_date": "Jan 2",
            "Jane Roe 555-123-4567": "x",
        },
    )
    by_input = {item["input"]: item["problem"] for item in problems}
    assert by_input["patient_mrn"] == "must be text"
    assert by_input["specialty"] == "must be one of the listed values"
    assert by_input["referral_date"].startswith("must be a date")
    assert by_input["urgent"] == "is missing"
    assert "(unknown)" in by_input
    serialized = json.dumps(problems)
    for value in ("Oncology", "Jan 2", "Jane Roe", "555-123-4567"):
        assert value not in serialized


def test_valid_inputs_pass_and_render_for_flow(tmp_path):
    card = card_for_bundle(load_workflow_info(typed_bundle(tmp_path)), default_name="w_x")
    inputs = {
        "patient_mrn": "7654321",
        "specialty": "Dermatology",
        "referral_date": "2026-10-08",
        "urgent": True,
    }
    if card.inputs["urgent"]["type"] == "number":
        inputs["urgent"] = 1
    assert input_problems(card, inputs) == []
    assert coerce_inputs(card, inputs)["urgent"] in {"true", "1"}
