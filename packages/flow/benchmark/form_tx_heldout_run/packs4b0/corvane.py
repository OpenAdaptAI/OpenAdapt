"""Corvane motorcycle quote rater: form pack (data only).

Written from the cv-dev intake, the ``corvane`` intake vocabulary and the
dev-case demonstration (``form_tx_heldout4_run/demos/corvane.jsonl``) only;
no portal source, scored intake or answer key was read. What the
demonstration shows:

- An agent sign-in page (agent code and password) before the worksheet.
- One worksheet page of accordion panels, each with a header button and a
  "Save panel" button: Quote basics, Rider, Garaging address, Motorcycles,
  Coverage. Only the open panel's controls show. "Rate this quote" sits
  below the panels.
- Dates are typed as MM/DD/YYYY.
- Motorcycles: "Add motorcycle" opens a dialog (``#bike``): VIN, "Decode
  VIN", read-only model year, make, model and engine size filled by the
  decoder, ownership, and a lienholder name the dev case never showed
  (owned outright). "Decode VIN" opens a decoder window listing vehicles,
  one "Use this vehicle" button per row (row cells: year, make, model, cc).
  "Add to quote" puts the bike in a grid row with a "Remove" button.
- Blank-form values the dev intake did not ask for: annual mileage 6000,
  liability 25/50/25, collision and comprehensive deductibles $500, custom
  parts and equipment 0, roadside "Not included". The collision deductible
  was left at $500 by the demonstrator (the intake's value).
- "CPE worksheet" opens a custom-parts worksheet the dev case never used, so
  it is not declared: a case with parts listed but no amount stops.
- "Rate this quote" shows "Rated quote" (every stored value as a table, the
  bike as a grid, an annual premium) with "Submit application". After it the
  page "Application submitted" shows the same table; the quote reference
  (CVQ-...) is in the URL.
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
    OutputExtractionSpec,
    PopupSubroutineSpec,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

CASE_PREFIX = "cv"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
FOLD = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
DATE_MDY = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)
PHONE = FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS)

PLACEHOLDERS = ("", "-- Select --")
DASH = ("—",)

RESULT_SECTION = (r"^Application submitted$",)
BIKE_RESULT_SECTION = (r"^Motorcycles$",)
DIALOG_SECTION = (r"^Add motorcycle$",)

SAFETY = r"Completed a rider safety course in the last 3 years"
ENDORSED = r"Years with motorcycle endorsement"
LIABILITY = r"Liability limits \(BI/PD\)"

# Keys derived from the intake's one motorcycle (see derived_facts).
BIKE_VIN = "bike.vin"
BIKE_YEAR = "bike.year"
BIKE_MAKE = "bike.make"
BIKE_MODEL = "bike.model"
BIKE_OWNERSHIP = "bike.ownership"
BIKE_LIENHOLDER = "bike.lienholder"
ANNUAL_MILES = "usage.annual_mileage"
# What the decoder puts back in the dialog's Model box (not an intake key).
BIKE_DECODED = "bike.decoded_model"


def _bind(
    key: str,
    name: str,
    label: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    defaults: tuple[str, ...] = (),
    required: bool = False,
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"cv_{name}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",),
            name_or_id_patterns=(f"^{name}(?:\\s|$)",),
        ),
        control_kind=kind,
        codec=codec,
        placeholder_values=PLACEHOLDERS,
        portal_default_values=defaults,
        required_in_pack=required,
    )


def _dialog(
    key: str,
    label: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    required: bool = False,
) -> FieldBindingSpec:
    """A control of the Add motorcycle dialog (no name attribute)."""
    return FieldBindingSpec(
        binding_id=f"cv_dlg_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=DIALOG_SECTION
        ),
        control_kind=kind,
        codec=codec,
        placeholder_values=PLACEHOLDERS,
        required_in_pack=required,
    )


def _shown(
    key: str,
    label: str,
    codec: FieldCodecSpec = TEXT,
    section: tuple[str, ...] = RESULT_SECTION,
    *,
    blank: tuple[str, ...] = (),
) -> FieldBindingSpec:
    extra = {"placeholder_values": ("",) + blank} if blank else {}
    return FieldBindingSpec(
        binding_id=f"cv_rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=section
        ),
        codec=codec,
        **extra,
    )


def _panel(
    screen_id: str, landmark: str, header: str, section: str
) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=screen_id,
        landmark_selector=landmark,
        next_button_anchor=FieldAnchorSpec(
            label_patterns=(r"^Save panel$",), section_patterns=(f"^{section}$",)
        ),
        revisit_selector=f"#hdr-{header}",
    )


PROFILE = UIProfileSpec(
    profile_id="heldout4b0_corvane",
    auth_wall_selectors=('input[type="password"]',),
    # The Add motorcycle dialog is a form panel, not a confirmation: it is
    # handled as its own screen. Every other open dialog still stops the run.
    modal_dialog_selectors=(
        "dialog[open]:not(#bike)",
        '[role="dialog"][aria-modal="true"]:not(#bike)',
        "[data-modal-open='true']:not(#bike)",
    ),
)

VIN_DECODER = PopupSubroutineSpec(
    subroutine_id="cv_vin_decoder",
    trigger_anchor=FieldAnchorSpec(
        label_patterns=(r"^Decode VIN$",), section_patterns=DIALOG_SECTION
    ),
    popup_mode="window",
    popup_url_pattern=r"/rater/vin-decoder",
    # One button per decoded vehicle; keep the row whose cells equal the
    # intake's year, make and model. No single row: the run stops.
    commit_anchor=FieldAnchorSpec(
        label_patterns=(r"^Use this vehicle$",),
        row_cell_dossier_keys=(BIKE_YEAR, BIKE_MAKE, BIKE_MODEL),
    ),
    parent_return_anchor=FieldAnchorSpec(
        label_patterns=(r"^Model$",), section_patterns=DIALOG_SECTION
    ),
    return_output_key=BIKE_DECODED,
    return_codec=FOLD,
    # No condition key: with a condition set to the VIN (a string, not a
    # yes/no flag) the dev run never opened the decoder. The VIN binding is
    # required, so a case with no single motorcycle stops before this.
)

PACK = FormPack(
    pack_id="heldout4b0.corvane.motorcycle.v1",
    domain_schema="personal_lines.motorcycle",
    ui_profile_id=PROFILE.profile_id,
    bindings=(
        # Quote basics
        _bind(
            "policy.effective_date",
            "effective_date",
            "Effective date",
            codec=DATE_MDY,
            required=True,
        ),
        _bind(
            "rider.prior_insurance",
            "prior_insurance",
            "Prior motorcycle insurance",
            ControlKind.SELECT,
            FOLD,
            required=True,
        ),
        # Rider
        _bind("rider.first_name", "first_name", "First name", required=True),
        _bind("rider.last_name", "last_name", "Last name", required=True),
        _bind(
            "rider.date_of_birth",
            "dob",
            "Date of birth",
            codec=DATE_MDY,
            required=True,
        ),
        _bind("rider.phone", "phone", "Phone"),
        _bind(
            "rider.years_endorsed",
            "years_endorsed",
            ENDORSED,
            codec=INT,
            required=True,
        ),
        _bind(
            "rider.safety_course",
            "safety_course",
            SAFETY,
            ControlKind.SELECT,
            BOOL,
            required=True,
        ),
        # Garaging address
        _bind("garaging.street", "street", "Street address", required=True),
        _bind("garaging.city", "city", "City", required=True),
        _bind(
            "garaging.state",
            "state",
            "State",
            ControlKind.SELECT,
            FOLD,
            required=True,
        ),
        _bind("garaging.zip", "zip", "ZIP", required=True),
        # Motorcycles
        _bind(
            ANNUAL_MILES,
            "annual_miles",
            "Annual mileage",
            codec=INT,
            defaults=("6000",),
            required=True,
        ),
        _dialog(BIKE_VIN, "VIN", required=True),
        _dialog(BIKE_OWNERSHIP, "Ownership", ControlKind.SELECT, FOLD, required=True),
        _dialog(BIKE_LIENHOLDER, "Lienholder name"),
        # Coverage
        _bind(
            "coverage.liability_limits",
            "bi_pd_limits",
            LIABILITY,
            ControlKind.SELECT,
            TEXT,
            defaults=("25/50/25",),
            required=True,
        ),
        _bind(
            "coverage.collision_deductible",
            "collision_deductible",
            "Collision deductible",
            ControlKind.SELECT,
            MONEY,
            defaults=("500", "$500"),
            required=True,
        ),
        _bind(
            "coverage.comprehensive_deductible",
            "comprehensive_deductible",
            "Comprehensive deductible",
            ControlKind.SELECT,
            MONEY,
            defaults=("500", "$500"),
            required=True,
        ),
        _bind(
            "coverage.cpe_amount",
            "cpe_coverage",
            "Custom parts and equipment",
            codec=INT,
            defaults=("0",),
            required=True,
        ),
        _bind(
            "coverage.roadside",
            "roadside",
            "Roadside coverage",
            ControlKind.SELECT,
            FOLD,
            defaults=("Not included",),
            required=True,
        ),
    ),
    popup_subroutines=(VIN_DECODER,),
    screens=(
        WizardScreenSpec(
            screen_id="cv_rated",
            landmark_selector="#submit-app",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(
                label_patterns=(r"^Submit application$",)
            ),
        ),
        WizardScreenSpec(
            screen_id="cv_bike_dialog",
            landmark_selector="#bike[open]",
            next_button_anchor=FieldAnchorSpec(
                label_patterns=(r"^Add to quote$",), section_patterns=DIALOG_SECTION
            ),
        ),
        _panel("cv_basics", "#effective_date", "basics", "Quote basics"),
        _panel("cv_rider", "#first_name", "rider", "Rider"),
        _panel("cv_garaging", "#street", "garaging", "Garaging address"),
        # The motorcycles panel once a bike is in the grid: save it.
        _panel("cv_motorcycles", "#bike-grid tbody tr", "motorcycles", "Motorcycles"),
        # The motorcycles panel with no bike yet: add one.
        WizardScreenSpec(
            screen_id="cv_motorcycles_add",
            landmark_selector="#annual_miles",
            next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Add motorcycle$",)),
            revisit_selector="#hdr-motorcycles",
        ),
        _panel("cv_coverage", "#bi_pd_limits", "coverage", "Coverage"),
        # Every panel saved and closed: rate the quote.
        WizardScreenSpec(
            screen_id="cv_rate",
            landmark_selector="#rate",
            next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Rate this quote$",)),
        ),
    ),
    summary_check=SummaryCheckSpec(
        field_readbacks=(
            _shown("policy.effective_date", "Effective date", DATE_MDY),
            _shown("rider.prior_insurance", "Prior motorcycle insurance", FOLD),
            _shown("rider.first_name", "First name"),
            _shown("rider.last_name", "Last name"),
            _shown("rider.date_of_birth", "Date of birth", DATE_MDY),
            _shown("rider.phone", "Phone", PHONE),
            _shown("rider.years_endorsed", ENDORSED, INT),
            _shown("rider.safety_course", SAFETY, BOOL),
            _shown("garaging.street", "Street address", FOLD),
            _shown("garaging.city", "City", FOLD),
            _shown("garaging.state", "State", FOLD),
            _shown("garaging.zip", "ZIP"),
            _shown(ANNUAL_MILES, "Annual mileage", INT),
            _shown("coverage.liability_limits", LIABILITY),
            _shown("coverage.collision_deductible", "Collision deductible", MONEY),
            _shown(
                "coverage.comprehensive_deductible", "Comprehensive deductible", MONEY
            ),
            _shown("coverage.cpe_amount", "Custom parts and equipment", MONEY),
            _shown("coverage.roadside", "Roadside coverage", FOLD),
            _shown(BIKE_VIN, "VIN", FOLD, BIKE_RESULT_SECTION),
            _shown(BIKE_YEAR, "Year", INT, BIKE_RESULT_SECTION),
            _shown(BIKE_MAKE, "Make", FOLD, BIKE_RESULT_SECTION),
            _shown(BIKE_MODEL, "Model", FOLD, BIKE_RESULT_SECTION),
            _shown(BIKE_DECODED, "Model", FOLD, BIKE_RESULT_SECTION),
            _shown(BIKE_OWNERSHIP, "Ownership", FOLD, BIKE_RESULT_SECTION),
            _shown(
                BIKE_LIENHOLDER, "Lienholder", TEXT, BIKE_RESULT_SECTION, blank=DASH
            ),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.reference",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Quote (?:reference|number)$",),
                    section_patterns=RESULT_SECTION,
                ),
                value_pattern=r"\b(CVQ-\d+)\b",
                required=True,
            ),
        ),
    ),
    max_commit_mutations=1,
    ignored_intake_keys=(
        # Entered through derived_facts: the one motorcycle's VIN, ownership
        # and lienholder in the dialog; year, make and model through the
        # decoder row; mileage restated as annual mileage.
        "motorcycles",
        "motorcycles.lienholder",
        "usage.mileage",
        "usage.mileage_period",
    ),
)


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Intake facts restated the way the portal shows them.

    - The intake's motorcycle as flat keys, only when there is exactly one
      (the dialog adds one bike; with none or several, the VIN has no value
      and the run stops for a person).
    - Mileage as annual mileage (a per-month figure times twelve).
    """
    out: dict[str, Any] = {}
    bikes = raw.get("motorcycles", "Motorcycles")
    if isinstance(bikes, list) and len(bikes) == 1 and isinstance(bikes[0], dict):
        bike = bikes[0]
        for field, key in (
            ("vin", BIKE_VIN),
            ("year", BIKE_YEAR),
            ("make", BIKE_MAKE),
            ("model", BIKE_MODEL),
            ("ownership", BIKE_OWNERSHIP),
            ("lienholder", BIKE_LIENHOLDER),
        ):
            value = bike.get(field)
            if value is not None and str(value).strip():
                out[key] = value
        lien = raw.get("motorcycles.lienholder")
        if BIKE_LIENHOLDER not in out and lien is not None and str(lien).strip():
            out[BIKE_LIENHOLDER] = lien
    miles = raw.get("usage.mileage")
    # No period given: unknown, so nothing is derived and the run stops.
    period = str(raw.get("usage.mileage_period") or "").strip().casefold()
    if isinstance(miles, (int, float)) and not isinstance(miles, bool):
        if period == "per year":
            out[ANNUAL_MILES] = int(miles)
        elif period == "per month":
            out[ANNUAL_MILES] = int(miles) * 12
    return out
