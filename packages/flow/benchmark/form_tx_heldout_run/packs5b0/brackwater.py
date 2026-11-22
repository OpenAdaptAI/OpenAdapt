"""Brackwater flood quote: form pack (data only).

Written from the bfu-d01 intake, the brackwater entry of the intake
vocabulary and the dev-case demonstration
(``form_tx_set4v2_run/demos/brackwater.jsonl``); no portal source, scored
intake or answer key was read. What the dev case showed:

- An agency sign-in page, then a hub (``/flood/hub``) with four section
  cards (Named insured, Property and map, Building details, Coverage and
  billing). Each card's "Edit ..." button opens that section in an in-page
  dialog (``#dlg-<card>``) with "Save section" and "Cancel"; the hub shows
  "Not started" or "Complete" per card as page text only.
- "Review declarations" on the hub is a link, not a control the surface
  lists, so the layer cannot open the review: the run stops at the hub for a
  person once every section is saved (``bfu_hub``).
- NFIP community is a type-ahead. Typing the community name lists entries
  such as "<name>, City of - <county> County (<community number>)"; the
  intake has no community number, so no fact can equal an entry and the
  run stops there for a person to choose (the dev case does).
- The deductible select shows $2,000 before anyone touches it (a portal
  default). "Estimate replacement cost" (square footage, construction,
  finish) was never opened on the dev case, so it is not declared: a case
  with no building coverage stops at that required field.
- Dates are typed MM/DD/YYYY; the stored quote shows them as YYYY-MM-DD.
- After "Submit quote request" the page lists the stored quote ("Stored
  quote") with lowercase labels, an empty "bill to" row, and the quote
  reference (BFU-Q-...) under "Quote issued". No dev-case control enters
  the mortgage lender, lien type or bill-to, so those keys are not bound or
  ignored: a case that gives them never verifies.
"""

from __future__ import annotations

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
    UnboundControlSpec,
    WizardScreenSpec,
)

CASE_PREFIX = "bfu"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
FOLD = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
PHONE = FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS)
DATE_MDY = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")
DATE_ISO = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="YMD")
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")

PLACEHOLDERS = ("", "Select", "Select zone")

STORED = (r"^Stored quote$",)


def _bind(
    key: str,
    label: str,
    name: str,
    section: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    defaults: tuple[str, ...] = (),
    required: bool = False,
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"bfu_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",),
            name_or_id_patterns=(f"^{name}(?:\\s|$)|(?:^|\\s){name}$",),
            section_patterns=(f"^{section}$",),
        ),
        control_kind=kind,
        codec=codec,
        placeholder_values=PLACEHOLDERS,
        portal_default_values=defaults,
        required_in_pack=required,
    )


def _shown(key: str, label: str, codec: FieldCodecSpec = TEXT) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"bfu_rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(label_patterns=(f"^{label}$",), section_patterns=STORED),
        codec=codec,
    )


def _dialog(screen_id: str, card: str, section: str) -> WizardScreenSpec:
    """One section dialog of the hub, open; "Save section" stores it."""
    return WizardScreenSpec(
        screen_id=screen_id,
        landmark_selector=f"#dlg-{card}[open]",
        next_button_anchor=FieldAnchorSpec(
            label_patterns=(r"^Save section$",), section_patterns=(f"^{section}$",)
        ),
        revisit_selector=f"#card-{card} > button",
    )


def _hub(screen_id: str, card: str, edit: str) -> WizardScreenSpec:
    """The hub while section ``card`` has no control the run edited."""
    return WizardScreenSpec(
        screen_id=screen_id,
        landmark_selector=(
            "body:not(:has(dialog[open]))"
            f":not(:has(#dlg-{card} :is(input, select):user-valid)) #card-{card}"
        ),
        next_button_anchor=FieldAnchorSpec(label_patterns=(edit,)),
    )


_SECTION_DIALOGS = "".join(
    f":not(#dlg-{card})" for card in ("insured", "property", "building", "coverage")
)

PROFILE = UIProfileSpec(
    profile_id="set4v2b0_brackwater",
    auth_wall_selectors=('input[type="password"]',),
    # The four section dialogs are the form itself; any other open dialog
    # is still a modal that stops the run.
    modal_dialog_selectors=(
        f"dialog[open]{_SECTION_DIALOGS}",
        f'[role="dialog"][aria-modal="true"]{_SECTION_DIALOGS}',
        "[data-modal-open='true']",
    ),
)

INSURED = "Named insured"
PROPERTY = "Property and map"
BUILDING = "Building details"
COVERAGE = "Coverage and billing"

PACK = FormPack(
    pack_id="set4v2b0.brackwater.flood.v1",
    domain_schema="personal_lines.flood",
    ui_profile_id=PROFILE.profile_id,
    bindings=(
        _bind("insured_name", "Insured name", "insured_name", INSURED, required=True),
        _bind("insured_phone", "Phone number", "insured_phone", INSURED),
        _bind(
            "property_street",
            "Property street address",
            "property_street",
            PROPERTY,
            required=True,
        ),
        _bind(
            "community_name",
            "NFIP community",
            "f-comm",
            PROPERTY,
            ControlKind.PORTAL_COMBOBOX,
            TEXT,
            required=True,
        ),
        _bind(
            "flood_zone_certificate",
            "Flood zone",
            "flood_zone",
            PROPERTY,
            ControlKind.SELECT,
            FOLD,
            required=True,
        ),
        _bind(
            "building_type",
            "Building type",
            "building_type",
            BUILDING,
            ControlKind.SELECT,
            FOLD,
            required=True,
        ),
        _bind(
            "foundation",
            "Foundation type",
            "foundation",
            "Foundation type",
            ControlKind.RADIO_GROUP,
            FOLD,
            required=True,
        ),
        _bind(
            "year_built", "Year built", "year_built", BUILDING, codec=INT, required=True
        ),
        _bind(
            "square_feet",
            "Total square footage",
            "square_feet",
            BUILDING,
            codec=INT,
            required=True,
        ),
        _bind(
            "building_coverage",
            r"Building coverage \(\$\)",
            "building_coverage",
            COVERAGE,
            codec=INT,
            required=True,
        ),
        _bind(
            "contents_coverage",
            r"Contents coverage \(\$\)",
            "contents_coverage",
            COVERAGE,
            codec=INT,
        ),
        _bind(
            "deductible",
            "Deductible",
            "deductible",
            COVERAGE,
            ControlKind.SELECT,
            MONEY,
            defaults=("2000", "$2,000"),
            required=True,
        ),
        _bind(
            "prior_claims",
            "Flood claims in the last 10 years",
            "prior_claims",
            COVERAGE,
            codec=INT,
            required=True,
        ),
        _bind(
            "effective_date",
            "Policy effective date",
            "effective_date",
            COVERAGE,
            codec=DATE_MDY,
            required=True,
        ),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="bfu_review",
            landmark_selector="#confirm",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(
                label_patterns=(r"^Submit quote request$",)
            ),
        ),
        _dialog("bfu_insured", "insured", INSURED),
        _dialog("bfu_property", "property", PROPERTY),
        _dialog("bfu_building", "building", BUILDING),
        _dialog("bfu_coverage", "coverage", COVERAGE),
        # The hub with no dialog open: open the first section the run has
        # not typed into yet (":user-valid" matches a control only after it
        # was edited). A section that failed to save keeps its dialog open,
        # and the dialog screens above come first.
        _hub("bfu_hub_insured", "insured", r"^Edit named insured$"),
        _hub("bfu_hub_property", "property", r"^Edit property and map$"),
        _hub("bfu_hub_building", "building", r"^Edit building details$"),
        _hub("bfu_hub_coverage", "coverage", r"^Edit coverage and billing$"),
        # Every section typed. "Review declarations" is a link, which the
        # surface does not list as a control, so this next button is never
        # found and the run stops here for a person to open the review.
        WizardScreenSpec(
            screen_id="bfu_hub",
            landmark_selector="#card-insured",
            next_button_anchor=FieldAnchorSpec(
                label_patterns=(r"^Review declarations$",)
            ),
        ),
    ),
    summary_check=SummaryCheckSpec(
        field_readbacks=(
            _shown("insured_name", "insured name"),
            _shown("insured_phone", "insured phone", PHONE),
            _shown("property_street", "property street", FOLD),
            _shown("community_name", "community", FOLD),
            _shown("flood_zone_certificate", "flood zone", FOLD),
            _shown("building_type", "building type", FOLD),
            _shown("foundation", "foundation", FOLD),
            _shown("year_built", "year built", INT),
            _shown("square_feet", "square feet", INT),
            _shown("building_coverage", "building coverage", MONEY),
            _shown("contents_coverage", "contents coverage", MONEY),
            _shown("deductible", "deductible", MONEY),
            _shown("prior_claims", "prior claims", INT),
            _shown("effective_date", "effective date", DATE_ISO),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.reference",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Quote reference$",),
                    section_patterns=(r"^Quote issued$",),
                ),
                value_pattern=r"\b(BFU-Q-\d+)\b",
                required=True,
            ),
        ),
    ),
    max_commit_mutations=1,
    unbound_controls=(
        UnboundControlSpec(
            anchor=FieldAnchorSpec(
                label_patterns=(r"^I confirm these declarations match",),
                name_or_id_patterns=(r"^confirm(?:\s|$)",),
            ),
            blank_values=("", "false"),
        ),
    ),
    ignored_intake_keys=(
        # Only the replacement-cost estimator asks for these (not declared).
        "construction",
        "finish",
        # Part of the community entry a person chooses; the type-ahead
        # stops on anything but an entry equal to the community name.
        "community_county",
    ),
)
