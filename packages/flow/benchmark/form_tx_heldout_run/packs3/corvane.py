"""Corvane homeowners quote: form pack (data only).

Written from the cv-dev intake, the intake vocabulary and the dev-case
demonstration (``form_tx_heldout3_run/demos/corvane.jsonl``) only; no portal
source, scored intake or answer key was read. What the demonstration shows:

- An agent sign-in page (password field) before the quote.
- Four "Save and continue" screens: Applicant and property, Dwelling,
  Household, Lender and coverage; then a Review screen with one "Edit"
  button per section and "Request quote".
- Every control has a ``name`` equal to its intake key and a visible label.
- Dates are typed as MM/DD/YYYY.
- "Co-applicant full name" is described as "Spouse or other person on the
  deed"; the demonstrator filled it with the spouse's name.
- Household asks for occupancy and whether dogs are kept (radio groups). The
  breed and bite-history questions appear only when dogs are kept (seen on
  the result page as "—" when they are not).
- The mortgagee is a type-ahead over a lender directory; the chosen entry
  shows the lender name, "ISAOA", and the clause address.
- Personal liability and the deductible show $300,000 and $1,000 before the
  demonstrator touched them (portal defaults). The occupancy and dogs radios
  also come pre-checked (primary residence, No), found by a dev-case run
  with those two bindings left out.
- "Estimate replacement cost" opens an estimator the dev case never used, so
  it is not declared: a case with no dwelling limit stops for a person.
- After "Request quote" the page "Quote ready" lists every stored value as a
  table, and the quote reference (CVH-Q...) is shown in the title and URL.
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

CASE_PREFIX = "cv"

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

RESULT_SECTION = (r"^Quote ready$",)

# Labels as the form and the result page show them.
CO_APPLICANT = r"Co-applicant full name"
SQ_FT = r"Finished living area \(sq ft\)"
ROOF_YEAR = r"Year roof was installed or last replaced"
DOGS = r"Are any dogs kept at this address\?"
BREEDS = r"Breed of each dog"
BITE = r"Has any dog at this address ever bitten a person or another animal\?"
MORTGAGEE = r"Mortgagee \(lender\)"
DWELLING = r"Dwelling \(Coverage A\) limit"

MORTGAGEE_KEY = "mortgagee_clause"

# The result page shows a question that was not asked (no dogs) as a dash;
# other empty rows are assumed to show it too (not seen on the dev case).
NOT_ASKED = ("\u2014",)


def _bind(
    key: str,
    name: str,
    label: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    defaults: tuple[str, ...] = (),
    required: bool = False,
    typeahead: int | None = None,
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"cv_{name}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",),
            name_or_id_patterns=(rf"^{name}(?:\s|$)",),
        ),
        control_kind=kind,
        codec=codec,
        placeholder_values=PLACEHOLDERS,
        portal_default_values=defaults,
        required_in_pack=required,
        typeahead_query_chars=typeahead,
    )


def _label_only(
    key: str,
    label: str,
    kind: ControlKind,
    codec: FieldCodecSpec,
) -> FieldBindingSpec:
    """A conditional question whose control name the dev case never showed."""
    return FieldBindingSpec(
        binding_id=f"cv_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=(r"^Household$",)
        ),
        control_kind=kind,
        codec=codec,
        placeholder_values=PLACEHOLDERS,
    )


def _shown(
    key: str,
    label: str,
    codec: FieldCodecSpec = TEXT,
    *,
    blank: tuple[str, ...] = NOT_ASKED,
) -> FieldBindingSpec:
    extra = {"placeholder_values": ("",) + blank} if blank else {}
    return FieldBindingSpec(
        binding_id=f"cv_rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=RESULT_SECTION
        ),
        codec=codec,
        **extra,
    )


def _screen(screen_id: str, landmark: str, edit_n: int) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=screen_id,
        landmark_selector=landmark,
        next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Save and continue$",)),
        revisit_selector=f"#app > h2:nth-of-type({edit_n}) > button",
    )


PROFILE = UIProfileSpec(
    profile_id="heldout3_corvane",
    auth_wall_selectors=('input[type="password"]',),
)

PACK = FormPack(
    pack_id="heldout3.corvane.homeowners.v1",
    domain_schema="personal_lines.homeowners",
    ui_profile_id=PROFILE.profile_id,
    bindings=(
        # 1. Applicant and property
        _bind("first_name", "first_name", "First name", required=True),
        _bind("last_name", "last_name", "Last name", required=True),
        _bind(
            "date_of_birth",
            "date_of_birth",
            "Date of birth",
            codec=DATE_MDY,
            required=True,
        ),
        _bind("email", "email", "Email"),
        _bind("phone", "phone", "Phone"),
        _bind("co_applicant_name", "co_applicant_name", CO_APPLICANT),
        _bind(
            "property_street",
            "property_street",
            "Property street address",
            required=True,
        ),
        _bind("property_city", "property_city", "City", required=True),
        _bind(
            "property_state",
            "property_state",
            "State",
            ControlKind.SELECT,
            FOLD,
            required=True,
        ),
        _bind("property_zip", "property_zip", "ZIP code", required=True),
        # 2. Dwelling
        _bind("year_built", "year_built", "Year built", codec=INT, required=True),
        _bind("square_feet", "square_feet", SQ_FT, codec=INT, required=True),
        _bind(
            "construction",
            "construction",
            "Construction type",
            ControlKind.SELECT,
            FOLD,
            required=True,
        ),
        _bind(
            "roof_material",
            "roof_material",
            "Roof material",
            ControlKind.SELECT,
            FOLD,
            required=True,
        ),
        _bind("roof_year", "roof_year", ROOF_YEAR, codec=INT, required=True),
        # 3. Household (both radios come pre-checked on a blank form: with
        # these two bindings removed, the dev record was still stored as
        # primary residence and no dogs)
        _bind(
            "occupancy",
            "occupancy",
            "Occupancy",
            ControlKind.RADIO_GROUP,
            FOLD,
            defaults=("Owner-occupied primary residence",),
            required=True,
        ),
        _bind(
            "dogs_on_premises",
            "dogs",
            DOGS,
            ControlKind.RADIO_GROUP,
            BOOL,
            defaults=("No",),
            required=True,
        ),
        _label_only("dog_breeds", BREEDS, ControlKind.TEXT, TEXT),
        _label_only("dog_bite_history", BITE, ControlKind.RADIO_GROUP, FOLD),
        # 4. Lender and coverage
        _bind(
            MORTGAGEE_KEY,
            "mortgagee",
            MORTGAGEE,
            ControlKind.PORTAL_COMBOBOX,
            TEXT,
            typeahead=6,
        ),
        _bind("loan_number", "loan_number", "Loan number"),
        _bind(
            "dwelling_limit",
            "dwelling_limit",
            DWELLING,
            codec=INT,
            required=True,
        ),
        _bind(
            "personal_liability",
            "personal_liability",
            "Personal liability",
            ControlKind.SELECT,
            MONEY,
            defaults=("300000", "$300,000"),
            required=True,
        ),
        _bind(
            "deductible",
            "deductible",
            "All-perils deductible",
            ControlKind.SELECT,
            MONEY,
            defaults=("1000", "$1,000"),
            required=True,
        ),
        _bind(
            "effective_date",
            "effective_date",
            "Policy effective date",
            codec=DATE_MDY,
            required=True,
        ),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="cv_review",
            landmark_selector="#submit",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Request quote$",)),
        ),
        _screen("cv_applicant", "#f-first_name", 1),
        _screen("cv_dwelling", "#f-year_built", 2),
        _screen("cv_household", "input[name='occupancy']", 3),
        _screen("cv_lender", "#f-mortgagee", 4),
    ),
    summary_check=SummaryCheckSpec(
        field_readbacks=(
            _shown("first_name", "First name"),
            _shown("last_name", "Last name"),
            _shown("date_of_birth", "Date of birth", DATE_MDY),
            _shown("email", "Email", FOLD),
            _shown("phone", "Phone", PHONE),
            _shown("co_applicant_name", CO_APPLICANT),
            _shown("property_street", "Property street address", FOLD),
            _shown("property_city", "City", FOLD),
            _shown("property_state", "State", FOLD),
            _shown("property_zip", "ZIP code"),
            _shown("year_built", "Year built", INT),
            _shown("square_feet", SQ_FT, INT),
            _shown("construction", "Construction type", FOLD),
            _shown("roof_material", "Roof material", FOLD),
            _shown("roof_year", ROOF_YEAR, INT),
            _shown("occupancy", "Occupancy", FOLD),
            _shown("dogs_on_premises", DOGS, BOOL),
            _shown("dog_breeds", BREEDS),
            _shown("dog_bite_history", BITE, FOLD),
            _shown(MORTGAGEE_KEY, MORTGAGEE),
            _shown("loan_number", "Loan number"),
            _shown("dwelling_limit", DWELLING, MONEY),
            _shown("personal_liability", "Personal liability", MONEY),
            _shown("deductible", "All-perils deductible", MONEY),
            _shown("effective_date", "Policy effective date", DATE_MDY),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.reference",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Quote (?:reference|number)$",),
                    section_patterns=RESULT_SECTION,
                ),
                value_pattern=r"\b(CVH-Q\d+)\b",
                required=True,
            ),
        ),
    ),
    max_commit_mutations=1,
    ignored_intake_keys=(
        # Entered through derived_facts as the mortgagee directory entry.
        "lender_name",
        "lender_clause_address",
        # Entered through derived_facts as the co-applicant (the portal has
        # no spouse field).
        "spouse_name",
        # The portal asks no marital status question (not on any screen of
        # the dev case).
        "marital_status",
    ),
)


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Intake facts restated the way the portal shows them.

    - The mortgagee directory entry: lender name, "ISAOA", clause address.
    - The co-applicant field asks for the spouse or other person on the
      deed; with no co-applicant given, the intake's spouse is that person.
    """
    out: dict[str, Any] = {}
    lender = str(raw.get("lender_name") or "").strip()
    clause = str(raw.get("lender_clause_address") or "").strip()
    if lender and clause:
        out[MORTGAGEE_KEY] = f"{lender} ISAOA, {clause}"
    elif lender:
        # No clause address: the name alone equals no directory entry, so
        # the run stops at the mortgagee instead of leaving it blank.
        out[MORTGAGEE_KEY] = lender
    spouse = str(raw.get("spouse_name") or "").strip()
    if spouse and not str(raw.get("co_applicant_name") or "").strip():
        out["co_applicant_name"] = spouse
    return out
