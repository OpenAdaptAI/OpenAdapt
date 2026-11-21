"""Wrenfield condo quote: form pack (data only).

Written from the wf-dev intake, the intake vocabulary and the dev-case
demonstration (``form_tx_heldout4_run/demos/wrenfield.jsonl``) only; no portal
source, scored intake or answer key was read. What the demonstration shows:

- A sign-in page (password field) before the quote.
- Four steps on one page, with a step bar ("1. Your unit" ... "4. Review"):
  Your unit, About you, Coverage ("Continue", "Continue", "Continue to
  review"), then Review with one "Edit" button per section, a confirmation
  checkbox and "Get my quote".
- Every control has a ``name`` equal to its own id and a visible label.
- Dates are typed as MM/DD/YYYY; coverage amounts are typed as digits.
- The condominium association is a type-ahead over a directory; the chosen
  entry reads "<association name> (<city>, <state>)".
- Water and sewer backup shows "Not included" and the deductible radio shows
  $1,000 checked before the demonstrator touched them (portal defaults).
- "Estimate it" next to Coverage A opens an estimator the dev case never
  used, so it is not declared: a case without a Coverage A amount stops for a
  person. Questions that appear only for other occupancies (months occupied,
  seasonal credit, tenant lease end) were never shown, so they are not bound
  either and stop for a person when they appear.
- After "Get my quote" the page "Your condo quote is ready" lists every stored
  value as a table; the quote reference (WF-...) is shown in the URL.
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
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

CASE_PREFIX = "wf"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
FOLD = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
DATE_MDY = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)
PHONE = FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS)

PLACEHOLDERS = (
    "",
    "Select…",
    "Select...",
    "-- Select --",
)

RESULT_SECTION = (r"^Your condo quote is ready$",)

ASSOCIATION_KEY = "association.listing"
CONFIRM_KEY = "flow.confirm_accurate"

# Labels as the form and the result page show them.
ASSOCIATION = r"Condominium association"
YEAR_BUILT = r"Year the building was built"
FLOOR = r"Floor the unit is on"
OCCUPANCY = r"How is the unit occupied\?"
WOOD_STOVE = r"Wood-burning stove or fireplace insert"
IMPROVEMENTS = r"Unit improvements and betterments \(Coverage A\)"
PERSONAL_PROPERTY = r"Personal property \(Coverage C\)"
WATER_BACKUP = r"Water and sewer backup"


def _bind(
    key: str,
    name: str,
    label: str,
    section: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    defaults: tuple[str, ...] = (),
    required: bool = False,
    typeahead: int | None = None,
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"wf_{name}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",),
            name_or_id_patterns=(rf"^{name}(?:\s|$)",),
            section_patterns=(f"^{section}$",),
        ),
        control_kind=kind,
        codec=codec,
        placeholder_values=PLACEHOLDERS,
        portal_default_values=defaults,
        required_in_pack=required,
        typeahead_query_chars=typeahead,
    )


def _shown(key: str, label: str, codec: FieldCodecSpec = TEXT) -> FieldBindingSpec:
    """A read-back of the result page's table of stored values."""
    return FieldBindingSpec(
        binding_id=f"wf_rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=RESULT_SECTION
        ),
        codec=codec,
    )


def _screen(
    screen_id: str, landmark: str, next_label: str, step: int
) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=screen_id,
        landmark_selector=landmark,
        next_button_anchor=FieldAnchorSpec(label_patterns=(f"^{next_label}$",)),
        revisit_selector=f"body > div > nav > ol > li:nth-of-type({step}) > button",
    )


PROFILE = UIProfileSpec(
    profile_id="heldout4b0_wrenfield",
    auth_wall_selectors=('input[type="password"]',),
)

PACK = FormPack(
    pack_id="heldout4b0.wrenfield.condo.v1",
    domain_schema="personal_lines.condo",
    ui_profile_id=PROFILE.profile_id,
    bindings=(
        # 1. Your unit
        _bind(
            ASSOCIATION_KEY,
            "association",
            ASSOCIATION,
            "Your unit",
            ControlKind.PORTAL_COMBOBOX,
            TEXT,
            required=True,
            typeahead=6,
        ),
        _bind(
            "unit.street", "street", "Unit street address", "Your unit", required=True
        ),
        _bind("unit.unit_number", "unit_number", "Unit number", "Your unit"),
        _bind("unit.city", "city", "City", "Your unit", required=True),
        _bind(
            "unit.state",
            "state",
            "State",
            "Your unit",
            ControlKind.SELECT,
            FOLD,
            required=True,
        ),
        _bind("unit.zip", "zip", "ZIP code", "Your unit", required=True),
        _bind(
            "building.year_built",
            "year_built",
            YEAR_BUILT,
            "Your unit",
            codec=INT,
            required=True,
        ),
        _bind("unit.floor", "floor", FLOOR, "Your unit", codec=INT, required=True),
        _bind(
            "unit.occupancy",
            "occupancy",
            OCCUPANCY,
            "Your unit",
            ControlKind.SELECT,
            FOLD,
            required=True,
        ),
        _bind(
            "unit.wood_stove",
            "wood_stove",
            WOOD_STOVE,
            "Your unit",
            ControlKind.SELECT,
            BOOL,
            required=True,
        ),
        # 2. About you
        _bind(
            "owner.first_name", "first_name", "First name", "About you", required=True
        ),
        _bind("owner.last_name", "last_name", "Last name", "About you", required=True),
        _bind(
            "owner.date_of_birth",
            "dob",
            "Date of birth",
            "About you",
            codec=DATE_MDY,
            required=True,
        ),
        _bind("owner.email", "email", "Email", "About you"),
        _bind("owner.phone", "phone", "Mobile phone", "About you"),
        # 3. Coverage
        _bind(
            "coverage.improvements",
            "improvements",
            IMPROVEMENTS,
            "Coverage",
            codec=INT,
            required=True,
        ),
        _bind(
            "coverage.personal_property",
            "personal_property",
            PERSONAL_PROPERTY,
            "Coverage",
            codec=INT,
            required=True,
        ),
        _bind(
            "coverage.loss_assessment",
            "loss_assessment",
            "Loss assessment",
            "Coverage",
            ControlKind.SELECT,
            MONEY,
            required=True,
        ),
        _bind(
            "coverage.water_backup",
            "water_backup",
            WATER_BACKUP,
            "Coverage",
            ControlKind.SELECT,
            FOLD,
            defaults=("Not included",),
            required=True,
        ),
        _bind(
            "coverage.deductible",
            "deductible",
            "Deductible",
            "Deductible",
            ControlKind.RADIO_GROUP,
            MONEY,
            defaults=("1000", "$1,000"),
            required=True,
        ),
        _bind(
            "policy.start_date",
            "start_date",
            "Policy start date",
            "Coverage",
            codec=DATE_MDY,
            required=True,
        ),
        # 4. Review: the confirmation box, ticked by the person submitting.
        FieldBindingSpec(
            binding_id="wf_confirm",
            dossier_key=CONFIRM_KEY,
            anchor=FieldAnchorSpec(
                label_patterns=(r"^I confirm the information above is accurate$",),
                name_or_id_patterns=(r"^confirm$",),
            ),
            control_kind=ControlKind.CHECKBOX,
            codec=BOOL,
        ),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="wf_review",
            landmark_selector="#get-quote",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Get my quote$",)),
            revisit_selector="body > div > nav > ol > li:nth-of-type(4) > button",
        ),
        _screen("wf_unit", "#association", "Continue", 1),
        _screen("wf_about", "#first_name", "Continue", 2),
        _screen("wf_coverage", "#improvements", "Continue to review", 3),
    ),
    summary_check=SummaryCheckSpec(
        field_readbacks=(
            _shown(ASSOCIATION_KEY, ASSOCIATION, FOLD),
            _shown("unit.street", "Unit street address", FOLD),
            _shown("unit.unit_number", "Unit number", FOLD),
            _shown("unit.city", "City", FOLD),
            _shown("unit.state", "State", FOLD),
            _shown("unit.zip", "ZIP code"),
            _shown("building.year_built", YEAR_BUILT, INT),
            _shown("unit.floor", FLOOR, INT),
            _shown("unit.occupancy", OCCUPANCY, FOLD),
            _shown("unit.wood_stove", WOOD_STOVE, BOOL),
            _shown("owner.first_name", "First name"),
            _shown("owner.last_name", "Last name"),
            _shown("owner.date_of_birth", "Date of birth", DATE_MDY),
            _shown("owner.email", "Email", FOLD),
            _shown("owner.phone", "Mobile phone", PHONE),
            _shown("coverage.improvements", IMPROVEMENTS, MONEY),
            _shown("coverage.personal_property", PERSONAL_PROPERTY, MONEY),
            _shown("coverage.loss_assessment", "Loss assessment", MONEY),
            _shown("coverage.water_backup", WATER_BACKUP, FOLD),
            _shown("coverage.deductible", "Deductible", MONEY),
            _shown("policy.start_date", "Policy start date", DATE_MDY),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.reference",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Quote (?:reference|number)$",),
                    section_patterns=RESULT_SECTION,
                ),
                value_pattern=r"\b(WF-\d+)\b",
                required=True,
            ),
        ),
    ),
    max_commit_mutations=1,
    ignored_intake_keys=(
        # Entered through derived_facts as the association directory entry.
        "association.name",
    ),
)


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Intake facts restated the way the portal shows them.

    - The association directory entry: "<name> (<city>, <state>)", with the
      unit's city and state (the unit is in the association's building).
      Without both, the name alone matches no entry and the run stops.
    - The review page's confirmation box, ticked by the person submitting.
    """
    out: dict[str, Any] = {}
    name = str(raw.get("association.name") or "").strip()
    city = str(raw.get("unit.city") or "").strip()
    state = str(raw.get("unit.state") or "").strip()
    if name and city and state:
        out[ASSOCIATION_KEY] = f"{name} ({city}, {state})"
    elif name:
        out[ASSOCIATION_KEY] = name
    out[CONFIRM_KEY] = True
    return out
