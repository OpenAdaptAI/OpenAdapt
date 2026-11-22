"""Larkspan travel insurance quote: the arm-B pack moved onto the GUI mechanisms.

Changes from ``packs5/larkspan.py`` (written after Set 4 v2 was unsealed;
the portal source and per-case settings were read, the answer key was not):

- Travellers are one repeating group (``gui.repeats``): every traveller in
  the intake list, traveller 1 included, is entered into its own "Traveller
  N" fieldset and checked there. The traveller-1 bindings and the popup
  workaround that stopped every multi-traveller case are gone.
- The review's confirmation box is a declared attestation
  (``gui.attestations``), ticked only on a grant given to the run. The
  ``confirm_details`` derived fact is gone.
- Some cases open on a "We have these details on file" card. A step
  (``gui.steps``) takes "Update my details", which writes nothing and shows
  the details form, so the bindings check every value against the intake.
- The review's dates and travellers are read back before the commit.
- A choice no binding answers that is still blank stops before the screen is
  left (``stop_on_blank_choice``).

The original notes, from the dev case:

Written from the lta-d01 intake, the larkspan intake vocabulary and the
dev-case demonstration (``form_tx_set4v2_run/demos/larkspan.jsonl``). What
the demonstration shows:

- A member sign-in page (member number and password), then one page
  (``/trip``) that shows one step at a time, each with its own "Continue"
  button: about you, destination, dates, travellers, trip cost, plan and
  cover, then "Check everything" with an "Edit ..." button per row, a
  confirmation checkbox and "Get my quote".
- The destination is a type-ahead; the chosen entry reads
  "<name> - <region>", which tells same-named places apart.
- Dates are typed as MM/DD/YYYY.
- Travellers are one fieldset per person ("Traveller 1"); more need the
  "Add another traveller" button. The dev case had one traveller. A case
  with more stops for a person before that button is clicked (see
  ``MORE_TRAVELLERS``): nothing in the pack can add a row.
- "Help me add it up" opens a cost helper the dev case never opened, so it
  is not declared. With no total but itemised costs, the total is the sum
  of the items (as the intake vocabulary defines it) and is typed directly.
- The state select shows "Choose a state" and the CFAR box is unticked on
  a blank form. The dev intake asked for no CFAR, so unticked is not a
  proven default.
- After "Get my quote" the page "Your quote" lists "What we stored" as a
  definition list, and the quote reference (LK-...) is in the URL.
- The stored record also lists an "Evacuation limit" that no step of the
  dev case asked for; it is not bound. If a case shows that question, the
  layer watches it as an unbound control.
"""

from __future__ import annotations

from typing import Any

from openadapt_flow.form_tx.gui_pack import (
    GuiAttestationSpec,
    GuiControlRef,
    GuiMechanismsSpec,
    GuiRepeatSlot,
    GuiRepeatSpec,
    GuiStepSpec,
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

CASE_PREFIX = "lta"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
FOLD = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
DATE_MDY = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)
PHONE = FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS)

PLACEHOLDERS = ("", "Choose a state", "-- Select --", "Select...", "Choose...")

DESTINATION_KEY = "destination_choice"
TRAVELLERS_SHOWN = "travellers_shown"
CONFIRM_LABEL = "I confirm the details above are correct"

RESULT_SECTION = (r"^What we stored$",)


def _bind(
    key: str,
    label: str,
    section: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    id_pattern: str | None = None,
    typeahead: int | None = None,
    defaults: tuple[str, ...] = (),
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"lta_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",),
            section_patterns=(f"^{section}$",),
            name_or_id_patterns=(id_pattern,) if id_pattern else (),
        ),
        control_kind=kind,
        codec=codec,
        placeholder_values=PLACEHOLDERS,
        typeahead_query_chars=typeahead,
        portal_default_values=defaults,
    )


def _shown(key: str, label: str, codec: FieldCodecSpec = TEXT) -> FieldBindingSpec:
    """A read-back of the result page's "What we stored" list."""
    return FieldBindingSpec(
        binding_id=f"lta_rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=RESULT_SECTION
        ),
        codec=codec,
    )


def _review(key: str, label: str, codec: FieldCodecSpec = TEXT) -> FieldBindingSpec:
    """A read-back of one "Check everything" row, before the commit."""
    return FieldBindingSpec(
        binding_id=f"lta_rv_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=(f"^{REVIEW}$",)
        ),
        codec=codec,
    )


def _continue(section: str) -> FieldAnchorSpec:
    return FieldAnchorSpec(
        label_patterns=(r"^Continue$",), section_patterns=(f"^{section}$",)
    )


def _screen(
    screen_id: str, landmark: str, section: str, edit_row: int
) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=screen_id,
        landmark_selector=landmark,
        next_button_anchor=_continue(section),
        revisit_selector=f"#rev > div:nth-of-type({edit_row}) > button",
    )


PROFILE = UIProfileSpec(
    profile_id="set4v2_larkspan",
    auth_wall_selectors=('input[type="password"]',),
)

ABOUT = "First, about you"
WHERE = "Where are you going\\?"
WHEN = "When are you travelling\\?"
WHO = "Who's travelling\\?"
COST = "What did the trip cost\\?"
REVIEW = "Check everything"
REVIEW_ROWS = (
    "Name",
    "Email",
    "Mobile phone",
    "State of residence",
    "Destination",
    "Departure date",
    "Return date",
    "Travellers",
    "Trip cost",
    "Plan",
    "Cancel For Any Reason",
)

PACK = FormPack(
    pack_id="set4v2.larkspan.travel.gui.v1",
    domain_schema="personal_lines.travel",
    ui_profile_id=PROFILE.profile_id,
    bindings=(
        # About you
        _bind("purchaser_name", "Full name", ABOUT, id_pattern=r"^d-name$"),
        _bind("purchaser_email", "Email", ABOUT),
        _bind("purchaser_phone", "Mobile phone", ABOUT),
        _bind(
            "residence_state",
            "State of residence",
            ABOUT,
            ControlKind.SELECT,
            FOLD,
            defaults=("Choose a state",),
        ),
        # Destination: type the first letters of the name, then pick the
        # entry "<name> - <region>".
        _bind(
            DESTINATION_KEY,
            "Destination",
            WHERE,
            ControlKind.PORTAL_COMBOBOX,
            FOLD,
            typeahead=3,
        ),
        # Dates
        _bind("departure_date", "Departure date", WHEN, codec=DATE_MDY),
        _bind("return_date", "Return date", WHEN, codec=DATE_MDY),
        # Trip cost
        _bind("trip_cost", r"Total prepaid trip cost \(\$\)", COST, codec=MONEY),
        # Plan and cover
        _bind(
            "plan",
            "Choose a plan",
            "Choose a plan",
            ControlKind.RADIO_GROUP,
            FOLD,
            id_pattern=r"^plan\b",
        ),
        _bind(
            "cfar",
            r"Add Cancel For Any Reason \(CFAR\)",
            "Choose your cover",
            ControlKind.CHECKBOX,
            BOOL,
        ),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="lta_review",
            landmark_selector="#buy",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Get my quote$",)),
        ),
        # "We have these details on file": left through gui.steps.
        WizardScreenSpec(screen_id="lta_profile", landmark_selector="#profile-yes"),
        _screen("lta_about", "#d-name", ABOUT, 1),
        _screen("lta_destination", "#dest", WHERE, 5),
        _screen("lta_dates", "#dep", WHEN, 6),
        _screen("lta_travellers", "#trav-add", WHO, 8),
        _screen("lta_cost", "#cost", COST, 9),
        _screen("lta_plan", "#cfar", "Choose your cover", 10),
    ),
    summary_check=SummaryCheckSpec(
        review_readbacks=(
            _review("departure_date", "Departure date", DATE_MDY),
            _review("return_date", "Return date", DATE_MDY),
            _review(TRAVELLERS_SHOWN, "Travellers", FOLD),
        ),
        field_readbacks=(
            _shown("purchaser_name", "Purchaser"),
            _shown("purchaser_email", "Email", FOLD),
            _shown("purchaser_phone", "Phone", PHONE),
            _shown("residence_state", "State of residence", FOLD),
            _shown(DESTINATION_KEY, "Destination", FOLD),
            _shown("departure_date", "Departing", DATE_MDY),
            _shown("return_date", "Returning", DATE_MDY),
            _shown(TRAVELLERS_SHOWN, "Travellers", FOLD),
            _shown("trip_cost", "Trip cost", MONEY),
            _shown("plan", "Plan", FOLD),
            _shown("cfar", "Cancel For Any Reason", BOOL),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.reference",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Quote (?:reference|number)$",),
                ),
                value_pattern=r"\b(LK-\d+-\d+)\b",
                required=True,
            ),
            OutputExtractionSpec(
                output_key="quote.total_premium",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Total premium$",),
                    section_patterns=(r"^Your quote is ready$",),
                ),
                codec=MONEY,
                required=False,
            ),
        ),
    ),
    max_commit_mutations=1,
    gui=GuiMechanismsSpec(
        steps=(
            GuiStepSpec(
                screen_id="lta_profile",
                control=GuiControlRef(role="button", name="Update my details"),
            ),
        ),
        repeats=(
            GuiRepeatSpec(
                group_id="travellers",
                screen_id="lta_travellers",
                intake_key="travelers",
                add=GuiControlRef(role="button", name="Add another traveller"),
                instance_kind="region",
                instance_pattern=r"^Traveller \d+$",
                slots=(
                    GuiRepeatSlot(role="textbox", name="Full name", item_key="name"),
                    GuiRepeatSlot(
                        role="textbox",
                        name="Date of birth",
                        item_key="dob",
                        codec=DATE_MDY,
                    ),
                ),
            ),
        ),
        attestations=(
            GuiAttestationSpec(
                attestation_id="confirm_details",
                screen_id="lta_review",
                control=GuiControlRef(role="checkbox", name=CONFIRM_LABEL),
                statement_sha256=statement_digest(CONFIRM_LABEL),
            ),
        ),
        stop_on_blank_choice=True,
    ),
    unbound_controls=(
        # The cost helper; the dev case typed the total and never opened it.
        UnboundControlSpec(
            anchor=FieldAnchorSpec(
                label_patterns=(r"^Help me add it up$",),
                name_or_id_patterns=(r"^calc-open$",),
            ),
        ),
        # The review's "Edit ..." buttons: the layer only uses them to go back
        # (``revisit_selector``), never to change a value.
        *(
            UnboundControlSpec(
                anchor=FieldAnchorSpec(
                    label_patterns=(f"^Edit {row}$",),
                    section_patterns=(f"^{REVIEW}$",),
                )
            )
            for row in REVIEW_ROWS
        ),
    ),
    ignored_intake_keys=(
        # Entered through derived facts as the "<name> - <region>" entry.
        "destination_name",
        "destination_region",
        # Entered through derived facts per traveller.
        "travelers",
        # Added up into the total when no total is given.
        "cost_flights",
        "cost_lodging",
        "cost_tours",
        "cost_other",
        # Free-text trip notes: no step of the dev case asks for them.
        "itinerary",
    ),
    required_intake_keys=(
        "purchaser_name",
        "purchaser_email",
        "purchaser_phone",
        "residence_state",
        "departure_date",
        "return_date",
        "trip_cost",
        "plan",
        "cfar",
    ),
)


def _mdy(iso: Any) -> str | None:
    try:
        y, m, d = (int(p) for p in str(iso).split("-"))
    except ValueError:
        return None
    return f"{m:02d}/{d:02d}/{y}"


def _amount(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Intake facts restated the way the portal asks for and shows them."""
    out: dict[str, Any] = {}
    name = str(raw.get("destination_name") or "").strip()
    region = str(raw.get("destination_region") or "").strip()
    if name and region:
        out[DESTINATION_KEY] = f"{name} - {region}"
    elif name:
        # No region: the name alone matches no entry, so the pick stops.
        out[DESTINATION_KEY] = name

    travellers = raw.get("travelers")
    if isinstance(travellers, list) and travellers:
        shown = []
        for t in travellers:
            t = t if isinstance(t, dict) else {}
            t_name, t_dob = str(t.get("name") or "").strip(), _mdy(t.get("dob"))
            if not t_name or not t_dob:
                shown = []
                break
            shown.append(f"{t_name} ({t_dob})")
        if shown:
            # How the review and the stored quote show the list.
            out[TRAVELLERS_SHOWN] = "; ".join(shown)

    # The vocabulary defines the total as the sum of the cost items when no
    # total is given. Only plain numbers are added; anything else stops.
    if raw.get("trip_cost") in (None, ""):
        items = [
            raw.get(k)
            for k in ("cost_flights", "cost_lodging", "cost_tours", "cost_other")
            if raw.get(k) not in (None, "")
        ]
        amounts = [_amount(v) for v in items]
        if items and all(a is not None for a in amounts):
            total = sum(a for a in amounts if a is not None)
            out["trip_cost"] = int(total) if total == int(total) else total

    return out
