"""Brackwater flood quote: the arm-B pack moved onto the GUI mechanisms.

Changes from ``packs5/brackwater.py`` (written after Set 4 v2 was unsealed;
the portal source and per-case settings were read, the answer key was not):

- The hub is a declared hub (``gui.hubs``): each of the four sections is
  opened, filled by the pack's own bindings and saved, and is done only
  when its card on the hub reads "Complete" (the token the card gains on
  save). The CSS-landmark screens that could open only two sections are gone.
- The hub is left through its "Review declarations" link (``gui.hubs``'
  leave step) only when every card reads done.
- The review's confirmation box is a declared attestation, ticked only on a
  grant given to the run. The ``flow.confirm_declarations`` derived fact is
  gone.
- A choice no binding answers that is still blank stops before the screen is
  left (``stop_on_blank_choice``).

The original notes, from the dev case:

Written from the bfu-d01 intake, the brackwater intake vocabulary and the
dev-case demonstration (``form_tx_set4v2_run/demos/brackwater.jsonl``, one
demonstration); no portal source, scored intake or answer key was read.
What the dev case showed:

- An agency sign-in page (password field) before the quote.
- A hub page (``/flood/hub``) with four cards: Named insured, Property and
  map, Building details, Coverage and billing. Each card's "Edit ..." button
  opens that section in a ``<dialog>`` with "Save section" and "Cancel";
  the card then reads "Complete" instead of "Not started". A review link
  (``#to-review``, which the layer's surface does not list as a control)
  leads to "Declarations preview".
- "NFIP community" is a type-ahead over a community directory: typing the
  name lists entries such as "<name>, City of - <county> County (<number>)".
  The entry's form and number are not in the intake, so the pack types the
  community name and the run stops for a person to choose the entry (the
  layer only accepts a suggestion equal to the typed value). The person
  chooses by the intake's county, which is why ``community_county`` is not
  entered by the layer.
- The flood zone select shows "Zone AE" for the code "AE"; the deductible
  select shows "$2,000" for "2000" and comes pre-set to 2000 on a blank
  form. The dev intake also asks for 2000, so that is not proven to be a
  portal default: a case with another deductible stops at that select.
- Foundation type is a radio group; none is pre-checked.
- "Estimate replacement cost" opens an estimator (square footage,
  construction, finish quality, "Use estimate") inside the coverage dialog.
  The dev case typed the building coverage and never opened it, so its
  controls were never seen and it is not declared: a case with no building
  coverage stops for a person at that field. Construction and finish feed
  only the estimator.
- "Declarations preview" lists every value (dates as MM/DD/YYYY) with a
  confirmation checkbox and "Submit quote request". The stored quote is then
  shown under "Stored quote" (dates as ISO) with the quote reference
  (BFU-Q-...). The stored quote has a "bill to" row the dev case never
  showed a control for (blank on the dev case); a lender on the intake
  therefore keeps the run from ``verified_done``.

Hub navigation: the layer tells screens apart only by CSS landmarks, and a
card's "Not started" / "Complete" status is text, which a CSS selector
cannot test. The only CSS signals of a saved section are its selects still
on their blank option (flood zone, building type), so the hub opens Building
details and Property and map only. After that, and with every section saved,
the hub's next step is ``#to-review``, which the layer's surface does not
list as a control: the run stops at the hub for a person ("next button missing").
"""

from __future__ import annotations

from typing import Any

from openadapt_flow.form_tx.gui_pack import (
    GuiAttestationSpec,
    GuiControlRef,
    GuiHubSection,
    GuiHubSpec,
    GuiMechanismsSpec,
)
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
from openadapt_flow.gui.attestations import statement_digest

CASE_PREFIX = "bfu"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
FOLD = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
DATE_MDY = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")
ISO = FieldCodecSpec(kind=FormCodecKind.ISO_DATE)
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
PHONE = FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS)
BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)
# Option codes and their visible text, from the compiler report.
ZONE = FieldCodecSpec(
    kind=FormCodecKind.ENUM_MAP,
    enum_aliases={z: (f"Zone {z}",) for z in ("A", "AE", "AO", "VE", "X", "D")},
)
DEDUCTIBLE = FieldCodecSpec(
    kind=FormCodecKind.ENUM_MAP,
    enum_aliases={
        "1250": ("$1,250",),
        "2000": ("$2,000",),
        "5000": ("$5,000",),
        "10000": ("$10,000",),
    },
)

PLACEHOLDERS = ("", "Select", "Select zone")

INSURED = "Named insured"
PROPERTY = "Property and map"
BUILDING = "Building details"
COVERAGE = "Coverage and billing"

REVIEW_SECTION = (r"^Declarations preview$",)
STORED_SECTION = (r"^Stored quote$",)

CONFIRM_LABEL = "I confirm these declarations match the applicant's information"


def _bind(
    key: str,
    name: str,
    label: str,
    section: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    **extra: Any,
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"bfu_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",),
            name_or_id_patterns=(f"^{name}(?:\\s|$)",) if name else (),
            section_patterns=(f"^{section}$",),
        ),
        control_kind=kind,
        codec=codec,
        placeholder_values=PLACEHOLDERS,
        required_in_pack=True,
        **extra,
    )


def _shown(
    key: str, label: str, section: tuple[str, ...], codec: FieldCodecSpec = TEXT
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"bfu_rb_{key}" if section == STORED_SECTION else f"bfu_rv_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=section
        ),
        codec=codec,
    )


SECTION_DIALOGS = ("#dlg-insured", "#dlg-property", "#dlg-building", "#dlg-coverage")
_NOT_SECTION = "".join(f":not({d})" for d in SECTION_DIALOGS)

PROFILE = UIProfileSpec(
    profile_id="set4v2_brackwater",
    auth_wall_selectors=('input[type="password"]',),
    # The four section editors are <dialog> elements of the form itself; any
    # other open dialog still stops the run.
    modal_dialog_selectors=(
        "dialog[open]" + _NOT_SECTION,
        '[role="dialog"][aria-modal="true"]' + _NOT_SECTION,
        "[data-modal-open='true']",
    ),
)

PACK = FormPack(
    pack_id="set4v2.brackwater.flood.gui.v1",
    domain_schema="personal_lines.flood",
    ui_profile_id=PROFILE.profile_id,
    bindings=(
        # Named insured
        _bind("insured_name", "insured_name", "Insured name", INSURED),
        _bind("insured_phone", "insured_phone", "Phone number", INSURED),
        # Property and map
        _bind(
            "property_street",
            "property_street",
            "Property street address",
            PROPERTY,
        ),
        # The control has no name; typed whole, and only a suggestion equal
        # to it is accepted (none is, on the dev case: a person chooses).
        _bind(
            "community_name",
            "",
            "NFIP community",
            PROPERTY,
            ControlKind.PORTAL_COMBOBOX,
            TEXT,
        ),
        _bind(
            "flood_zone_certificate",
            "flood_zone",
            "Flood zone",
            PROPERTY,
            ControlKind.SELECT,
            ZONE,
            portal_default_values=("Select zone",),
        ),
        # Building details
        _bind(
            "building_type",
            "building_type",
            "Building type",
            BUILDING,
            ControlKind.SELECT,
            FOLD,
            portal_default_values=("Select",),
        ),
        _bind(
            "foundation",
            "foundation",
            "Foundation type",
            "Foundation type",
            ControlKind.RADIO_GROUP,
            FOLD,
        ),
        _bind("year_built", "year_built", "Year built", BUILDING, codec=INT),
        _bind(
            "square_feet",
            "square_feet",
            "Total square footage",
            BUILDING,
            codec=INT,
        ),
        # Coverage and billing
        _bind(
            "building_coverage",
            "building_coverage",
            r"Building coverage \(\$\)",
            COVERAGE,
            codec=INT,
        ),
        _bind(
            "contents_coverage",
            "contents_coverage",
            r"Contents coverage \(\$\)",
            COVERAGE,
            codec=INT,
        ),
        _bind(
            "deductible",
            "deductible",
            "Deductible",
            COVERAGE,
            ControlKind.SELECT,
            DEDUCTIBLE,
        ),
        _bind(
            "prior_claims",
            "prior_claims",
            "Flood claims in the last 10 years",
            COVERAGE,
            codec=INT,
        ),
        _bind(
            "effective_date",
            "effective_date",
            "Policy effective date",
            COVERAGE,
            codec=DATE_MDY,
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
            revisit_selector="#to-review",
        ),
        # The hub: walked by gui.hubs, left through its review link.
        WizardScreenSpec(screen_id="bfu_hub", landmark_selector="#card-insured"),
    ),
    summary_check=SummaryCheckSpec(
        review_readbacks=(
            _shown("insured_name", "Insured name", REVIEW_SECTION),
            _shown("insured_phone", "Phone number", REVIEW_SECTION, PHONE),
            _shown("property_street", "Property street address", REVIEW_SECTION, FOLD),
            _shown("flood_zone_certificate", "Flood zone", REVIEW_SECTION, FOLD),
            _shown("building_type", "Building type", REVIEW_SECTION, FOLD),
            _shown("foundation", "Foundation type", REVIEW_SECTION, FOLD),
            _shown("year_built", "Year built", REVIEW_SECTION, INT),
            _shown("square_feet", "Total square footage", REVIEW_SECTION, INT),
            _shown("building_coverage", "Building coverage", REVIEW_SECTION, MONEY),
            _shown("contents_coverage", "Contents coverage", REVIEW_SECTION, MONEY),
            _shown("deductible", "Deductible", REVIEW_SECTION, MONEY),
            _shown(
                "prior_claims",
                "Flood claims in the last 10 years",
                REVIEW_SECTION,
                INT,
            ),
            _shown("effective_date", "Policy effective date", REVIEW_SECTION, DATE_MDY),
        ),
        # The community is shown as the directory entry, which the intake
        # does not give in full: it has no read-back and is reported
        # not_checked.
        field_readbacks=(
            _shown("insured_name", "insured name", STORED_SECTION),
            _shown("insured_phone", "insured phone", STORED_SECTION, PHONE),
            _shown("property_street", "property street", STORED_SECTION, FOLD),
            _shown("flood_zone_certificate", "flood zone", STORED_SECTION, FOLD),
            _shown("building_type", "building type", STORED_SECTION, FOLD),
            _shown("foundation", "foundation", STORED_SECTION, FOLD),
            _shown("year_built", "year built", STORED_SECTION, INT),
            _shown("square_feet", "square feet", STORED_SECTION, INT),
            _shown("building_coverage", "building coverage", STORED_SECTION, MONEY),
            _shown("contents_coverage", "contents coverage", STORED_SECTION, MONEY),
            _shown("deductible", "deductible", STORED_SECTION, MONEY),
            _shown("prior_claims", "prior claims", STORED_SECTION, INT),
            _shown("effective_date", "effective date", STORED_SECTION, ISO),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.reference",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Quote reference$",),
                    section_patterns=(r"^Quote issued$",),
                ),
                value_pattern=r"\b(BFU-Q-\d+)\b",
            ),
        ),
    ),
    max_commit_mutations=1,
    gui=GuiMechanismsSpec(
        hubs=(
            GuiHubSpec(
                screen_id="bfu_hub",
                sections=tuple(
                    GuiHubSection(
                        section_id=sid,
                        opener=GuiControlRef(
                            role="button",
                            name=f"Edit {title.lower()}",
                            containers=(title,),
                        ),
                        dialog_name=title,
                        save=GuiControlRef(role="button", name="Save section"),
                        card=title,
                        done_tokens=("complete",),
                        pending_tokens=("not", "started"),
                        save_writes=1,
                    )
                    for sid, title in (
                        ("insured", INSURED),
                        ("property", PROPERTY),
                        ("building", BUILDING),
                        ("coverage", COVERAGE),
                    )
                ),
                leave=GuiControlRef(
                    role="link", name="Review declarations", target="/flood/review"
                ),
            ),
        ),
        attestations=(
            GuiAttestationSpec(
                attestation_id="confirm_declarations",
                screen_id="bfu_review",
                control=GuiControlRef(role="checkbox", name=CONFIRM_LABEL),
                statement_sha256=statement_digest(CONFIRM_LABEL),
            ),
        ),
        stop_on_blank_choice=True,
    ),
    unbound_controls=(
        # Each section dialog's Cancel (only the open dialog's is visible).
        UnboundControlSpec(anchor=FieldAnchorSpec(label_patterns=(r"^Cancel$",))),
        # The two hub cards the layer cannot tell saved from unsaved (see
        # the screens): left for a person.
        UnboundControlSpec(
            anchor=FieldAnchorSpec(label_patterns=(r"^Edit named insured$",))
        ),
        UnboundControlSpec(
            anchor=FieldAnchorSpec(label_patterns=(r"^Edit coverage and billing$",))
        ),
        # The replacement-cost estimator, never opened on the dev case.
        UnboundControlSpec(
            anchor=FieldAnchorSpec(label_patterns=(r"^Estimate replacement cost$",))
        ),
    ),
    ignored_intake_keys=(
        # Used by the person who chooses the community directory entry.
        "community_county",
        # Inputs of the replacement-cost estimator only (not declared).
        "construction",
        "finish",
    ),
    # A lender has no control on any screen of the dev case, but the stored
    # quote has a "bill to" row: a lender the layer did not enter is an error.
    required_intake_keys=("mortgage_lender", "lien_type"),
)


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """No derived facts: the confirmation box is an attestation, not a fact."""
    return {}
