"""Ferncastle pack (data only), from the dev intake and the dev-case demonstration.

A tabbed producer workstation on one URL: Insured, Location, Building,
Rental, Coverage and Rate tabs, each saved with "Save and continue". The Rate
tab shows the entered values as a table and "Request quote" submits. After
submit the same table is shown under "Quote issued"; read-backs are anchored
to that section so the pre-submit review table never counts.
"""

from __future__ import annotations

from typing import Any

from openadapt_flow.form_tx.types import (
    ControlKind,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormPack,
    PrefillPolicy,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

CASE_PREFIX = "fc"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
CASEFOLD = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
DATE = FieldCodecSpec(kind=FormCodecKind.ISO_DATE)
PHONE = FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS)
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")

PROFILE = UIProfileSpec(
    profile_id="heldout2_ferncastle",
    auth_wall_selectors=('input[type="password"]',),
)

# (dossier key, visible label, control kind, codec)
FIELDS: tuple[tuple[str, str, ControlKind, FieldCodecSpec], ...] = (
    ("applicant.first_name", "First name", ControlKind.TEXT, TEXT),
    ("applicant.last_name", "Last name", ControlKind.TEXT, TEXT),
    ("applicant.email", "Email", ControlKind.TEXT, CASEFOLD),
    ("applicant.phone", "Phone", ControlKind.TEXT, PHONE),
    ("property.address_line1", "Street address", ControlKind.TEXT, TEXT),
    ("property.city", "City", ControlKind.TEXT, TEXT),
    ("property.state", "State", ControlKind.SELECT, CASEFOLD),
    ("property.zip", "ZIP code", ControlKind.TEXT, TEXT),
    ("property.year_built", "Year built", ControlKind.TEXT, INT),
    ("property.units", "Number of units", ControlKind.SELECT, INT),
    ("property.construction", "Construction", ControlKind.SELECT, CASEFOLD),
    ("property.square_feet", "Total square feet", ControlKind.TEXT, INT),
    ("property.roof_year", r"Roof replaced \(year\)", ControlKind.TEXT, INT),
    ("rental.monthly_rent", r"Monthly rent per unit \(\$\)", ControlKind.TEXT, INT),
    ("rental.units_rented", r"(Number of )?units rented", ControlKind.SELECT, INT),
    ("coverage.effective_date", "Policy effective date", ControlKind.DATE, DATE),
    ("coverage.dwelling_limit", r"Dwelling limit \(\$\)", ControlKind.TEXT, INT),
    ("coverage.loss_of_rents", "Loss of rents", ControlKind.SELECT, CASEFOLD),
    ("coverage.liability_limit", "Premises liability", ControlKind.SELECT, MONEY),
    ("coverage.deductible", "Deductible", ControlKind.SELECT, MONEY),
)


def _bind(key: str, label: str, kind: ControlKind, codec: FieldCodecSpec) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"fc_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(label_patterns=(f"^{label}$",)),
        control_kind=kind,
        codec=codec,
        prefill_policy=PrefillPolicy.HALT_ON_CONFLICT,
    )


def _shown(key: str, label: str, codec: FieldCodecSpec) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"fc_rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=("^Quote issued",)
        ),
        codec=codec,
    )


def _tab(screen_id: str, landmark: str) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=screen_id,
        landmark_selector=landmark,
        next_button_anchor=FieldAnchorSpec(label_patterns=("^Save and continue$",)),
    )


PACK = FormPack(
    pack_id="heldout2.ferncastle.landlord.v1",
    domain_schema="personal_lines.dwelling_fire",
    ui_profile_id=PROFILE.profile_id,
    bindings=tuple(_bind(*f) for f in FIELDS),
    screens=(
        WizardScreenSpec(
            screen_id="fc_rate",
            landmark_selector="#request",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=("^Request quote$",)),
        ),
        _tab("fc_insured", "#f-first_name"),
        _tab("fc_location", "#f-street"),
        _tab("fc_building", "#f-year_built"),
        _tab("fc_rental", "#f-monthly_rent"),
        _tab("fc_coverage", "#f-eff_date"),
    ),
    summary_check=SummaryCheckSpec(
        # "Units rented" was not on the dev case's screens (one unit), so it is
        # bound but has no declared read-back: if it is ever typed, it is
        # reported not_checked.
        field_readbacks=tuple(
            _shown(k, lbl, c) for k, lbl, _, c in FIELDS if k != "rental.units_rented"
        ),
    ),
    max_commit_mutations=1,
)


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    return {}
