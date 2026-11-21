"""Pack-authoring report from dev-case demonstrations (setup time, no browser)."""

from __future__ import annotations

from openadapt_flow.form_tx.demo_compiler import compile_demo_report
from openadapt_flow.form_tx.types import (
    ControlKind,
    FieldAnchorSpec,
    FieldBindingSpec,
    FormCodecKind,
    FormPack,
    PopupSubroutineSpec,
    WizardScreenSpec,
)


def _ctrl(label: str, value: str = "", **kw):
    return {
        "control_id": label,
        "dom_selector": f"#{label.replace(' ', '_')}",
        "tag": kw.pop("tag", "select"),
        "label_text": label,
        "current_value": value,
        **kw,
    }


def _row(*controls, why: str = "before click x", banners=()):
    return {
        "step": 0,
        "why": why,
        "frame_url": "http://x/",
        "controls": list(controls),
        "banners": list(banners),
    }


DED_OPTS = [{"value": "500", "label": "$500"}, {"value": "1000", "label": "$1,000"}]

PACK = FormPack(
    pack_id="p",
    domain_schema="test/v1",
    ui_profile_id="t",
    bindings=(
        FieldBindingSpec(
            binding_id="ded",
            dossier_key="coverage.deductible",
            anchor=FieldAnchorSpec(label_patterns=("^Deductible$",)),
            control_kind=ControlKind.SELECT,
        ),
        FieldBindingSpec(
            binding_id="lim",
            dossier_key="coverage.limit",
            anchor=FieldAnchorSpec(label_patterns=("^Limit$",)),
            control_kind=ControlKind.SELECT,
        ),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="s",
            landmark_selector="#s",
            next_button_anchor=FieldAnchorSpec(label_patterns=("^Next$",)),
        ),
    ),
)


def test_defaults_are_proven_only_when_an_intake_asked_for_something_else() -> None:
    demo = [
        _row(
            _ctrl("Deductible", "500", options=DED_OPTS),
            _ctrl("Limit", "300000"),
            _ctrl("Next", tag="button"),
        )
    ]
    report = compile_demo_report(
        PACK,
        [(demo, {"coverage.deductible": "1000", "coverage.limit": "300000"})],
    )
    status = {f.binding_id: f.status for f in report.defaults}
    assert status == {"ded": "proven_default", "lim": "unproven_same_as_intake"}
    assert report.proven_defaults() == {"ded": ("500",)}
    assert report.demos_used == 1
    codec = report.option_codecs["ded"]
    assert codec.kind == FormCodecKind.ENUM_MAP and codec.enum_aliases["1000"] == (
        "$1,000",
    )


def test_a_second_demo_that_shows_another_blank_value_is_not_a_default() -> None:
    a = [_row(_ctrl("Deductible", "500"), _ctrl("Limit", ""))]
    b = [_row(_ctrl("Deductible", "1000"), _ctrl("Limit", ""))]
    report = compile_demo_report(
        PACK,
        [(a, {"coverage.deductible": "1000"}), (b, {"coverage.deductible": "500"})],
    )
    status = {f.binding_id: f.status for f in report.defaults}
    assert status["ded"] == "differs_between_demos"
    assert status["lim"] == "blank"
    assert report.proven_defaults() == {}


def test_row_key_cells_and_unopened_launchers() -> None:
    sel = dict(tag="input", input_type="button")
    demo = [
        _row(
            _ctrl("Select", "Select", row_cells=["2021 Okel", "Kite", "4D", ""], **sel),
            {
                **_ctrl(
                    "Select",
                    "Select",
                    row_cells=["2021 Okel", "Kite Wagon", "4D", ""],
                    **sel,
                ),
                "dom_selector": "#s2",
            },
            _ctrl("Estimate replacement cost", tag="button"),
            _ctrl("Next", tag="button"),
            why="before click <Locator selector='#next'>",
        )
    ]
    report = compile_demo_report(
        PACK, [(demo, {"vehicle.model": "Kite Wagon", "vehicle.year": "2021"})]
    )
    (rk,) = report.row_keys
    assert rk.button_label == "Select" and rk.dossier_keys == ("vehicle.model",)
    assert "Estimate replacement cost" in report.unopened_launchers
    assert "Next" not in report.unopened_launchers


def test_a_popup_the_pack_covers_is_not_listed() -> None:
    pack = PACK.model_copy(
        update={
            "popup_subroutines": (
                PopupSubroutineSpec(
                    subroutine_id="est",
                    trigger_anchor=FieldAnchorSpec(
                        label_patterns=("^Estimate replacement cost$",)
                    ),
                    commit_anchor=FieldAnchorSpec(label_patterns=("^Use$",)),
                    parent_return_anchor=FieldAnchorSpec(
                        label_patterns=("^Dwelling$",)
                    ),
                    return_output_key="coverage.dwelling",
                ),
            )
        }
    )
    demo = [_row(_ctrl("Estimate replacement cost", tag="button"))]
    assert compile_demo_report(pack, [(demo, {})]).unopened_launchers == ()
