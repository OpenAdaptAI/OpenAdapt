"""Larkspan travel quote: form pack written from the dev case only (data only).

Written from the lta-d01 intake, the larkspan intake vocabulary and the
dev-case demonstration (``form_tx_set4v2_run/demos/larkspan.jsonl``); no
portal source, scored intake or answer key was read. What the dev case
showed:

- A member sign-in page (password field), then one page (``/trip``) that
  shows one step at a time, each with its own "Continue" button: about you
  (name, email, mobile phone, state select), destination (a type-ahead
  combobox whose chosen entry reads "<place> - <region>"), dates (typed
  MM/DD/YYYY), travellers (one fieldset per traveller, "Traveller 1", with
  an "Add another traveller" button), trip cost (typed as digits, with a
  "Help me add it up" calculator the dev case never opened), and cover (a
  plan radio group and a CFAR checkbox, unchecked on the blank form).
- "Check everything" lists every answer with one "Edit" button per row, an
  "I confirm the details above are correct" checkbox and "Get my quote".
- The quote page shows the stored values under "What we stored" as a
  definition list; the quote reference is in the URL (LK-..-.....).

Fail-closed choices:

- Only one traveller is supported: the layer has no way to press "Add
  another traveller", so a case with more (or no) travellers gets no
  traveller facts and stops at the required traveller name.
- The trip cost calculator is not declared: a case without a total trip
  cost stops at the required cost field (the cost items are not summed).
- The free-text itinerary has no control on any screen of the dev case and
  is not entered.
- The review's confirmation checkbox is declared as its own step whose
  "Next" is the checkbox; the run reports it as not read back.
- The summary shows the traveller as one "Name (MM/DD/YYYY)" row, read back
  through a derived key; the traveller's name and date of birth themselves
  have no row of their own and are reported not_checked. So the dev case
  submits a correct record but ends readback_incomplete, not verified.
- The summary's "Evacuation limit" had no control on the dev case and no
  intake key; it is neither entered nor read back.
"""

from __future__ import annotations

import re
from typing import Any

from openadapt_flow.form_tx.types import (
    ControlKind,
    FieldAnchorSpec,
    FieldBindingSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormPack,
    OutputExtractionSpec,
    PrefillPolicy,
    SummaryCheckSpec,
    UIProfileSpec,
    UnboundControlSpec,
    WizardScreenSpec,
)

CASE_PREFIX = "lta"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
FOLD = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
DATE_MDY = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)
PHONE = FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS)

PLACEHOLDERS = ("", "Choose a state")

ABOUT = (r"^First, about you$",)
STORED = (r"^What we stored$",)

DESTINATION_KEY = "destination_shown"
TRAVELLERS_KEY = "travellers_shown"
T1_NAME = "traveller_1_name"
T1_DOB = "traveller_1_dob"


def _p(label: str) -> str:
    return "^" + re.escape(label) + "$"


def _bind(
    key: str,
    label: str,
    id_attr: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    section: tuple[str, ...] = (),
    defaults: tuple[str, ...] = (),
    typeahead: int | None = None,
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"lta_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(_p(label),),
            name_or_id_patterns=(_p(id_attr),),
            section_patterns=section,
        ),
        control_kind=kind,
        codec=codec,
        prefill_policy=PrefillPolicy.HALT_ON_CONFLICT,
        required_in_pack=True,
        placeholder_values=PLACEHOLDERS,
        portal_default_values=defaults,
        typeahead_query_chars=typeahead,
    )


def _radio(key: str, group_label: str, name: str) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"lta_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(_p(group_label),), name_or_id_patterns=(_p(name),)
        ),
        control_kind=ControlKind.RADIO_GROUP,
        codec=FOLD,
        required_in_pack=True,
    )


def _shown(key: str, label: str, codec: FieldCodecSpec = TEXT) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"lta_rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(label_patterns=(_p(label),), section_patterns=STORED),
        codec=codec,
    )


def _screen(
    screen_id: str, landmark: str, edit_row: int, section: str
) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=screen_id,
        landmark_selector=landmark,
        next_button_anchor=FieldAnchorSpec(
            label_patterns=(r"^Continue$",), section_patterns=(_p(section),)
        ),
        # The review's "Edit <field>" button for the step's first field.
        revisit_selector=f"#rev > div:nth-of-type({edit_row}) > button",
    )


PROFILE = UIProfileSpec(
    profile_id="set4v2b0_larkspan",
    auth_wall_selectors=('input[type="password"]',),
)

PACK = FormPack(
    pack_id="set4v2b0.larkspan.travel.v1",
    domain_schema="personal_lines.travel",
    ui_profile_id=PROFILE.profile_id,
    bindings=(
        # 1. First, about you
        _bind("purchaser_name", "Full name", "d-name", section=ABOUT),
        _bind("purchaser_email", "Email", "d-email", codec=FOLD),
        _bind("purchaser_phone", "Mobile phone", "d-phone"),
        _bind(
            "residence_state",
            "State of residence",
            "d-state",
            ControlKind.SELECT,
            FOLD,
        ),
        # 2. Where are you going?
        _bind(
            DESTINATION_KEY,
            "Destination",
            "dest",
            ControlKind.PORTAL_COMBOBOX,
            TEXT,
            typeahead=4,
        ),
        # 3. When are you travelling?
        _bind("departure_date", "Departure date", "dep", codec=DATE_MDY),
        _bind("return_date", "Return date", "ret", codec=DATE_MDY),
        # 4. Who's travelling? (one traveller only, see the module notes)
        _bind(T1_NAME, "Full name", "tn0", section=(r"^Traveller 1$",)),
        _bind(
            T1_DOB,
            "Date of birth",
            "td0",
            codec=DATE_MDY,
            section=(r"^Traveller 1$",),
        ),
        # 5. What did the trip cost?
        _bind("trip_cost", "Total prepaid trip cost ($)", "cost", codec=INT),
        # 6. Choose your cover
        _radio("plan", "Choose a plan", "plan"),
        _bind(
            "cfar",
            "Add Cancel For Any Reason (CFAR)",
            "cfar",
            ControlKind.CHECKBOX,
            BOOL,
            defaults=("false",),
        ),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="lta_review",
            landmark_selector="#agree:checked",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Get my quote$",)),
        ),
        # The review's confirmation tick, declared as its own step: the
        # layer ticks it like a Next button once the review is shown.
        WizardScreenSpec(
            screen_id="lta_confirm",
            landmark_selector="#agree:not(:checked)",
            next_button_anchor=FieldAnchorSpec(
                label_patterns=(r"^I confirm the details above are correct$",)
            ),
        ),
        _screen("lta_about", "#d-name", 1, "First, about you"),
        _screen("lta_destination", "#dest", 5, "Where are you going?"),
        _screen("lta_dates", "#dep", 6, "When are you travelling?"),
        _screen("lta_travellers", "#tn0", 8, "Who's travelling?"),
        _screen("lta_cost", "#cost", 9, "What did the trip cost?"),
        _screen("lta_cover", "#cfar", 10, "Choose your cover"),
    ),
    summary_check=SummaryCheckSpec(
        field_readbacks=(
            _shown("purchaser_name", "Purchaser"),
            _shown("purchaser_email", "Email", FOLD),
            _shown("purchaser_phone", "Phone", PHONE),
            _shown("residence_state", "State of residence", FOLD),
            _shown(DESTINATION_KEY, "Destination", FOLD),
            _shown("departure_date", "Departing", DATE_MDY),
            _shown("return_date", "Returning", DATE_MDY),
            _shown(TRAVELLERS_KEY, "Travellers", FOLD),
            _shown("trip_cost", "Trip cost", MONEY),
            _shown("plan", "Plan", FOLD),
            _shown("cfar", "Cancel For Any Reason", BOOL),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.reference",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Quote (?:reference|number)$",)
                ),
                value_pattern=r"\b(LK-\d{2}-\d{4,})\b",
                required=True,
            ),
        ),
    ),
    unbound_controls=(
        UnboundControlSpec(
            anchor=FieldAnchorSpec(
                label_patterns=(r"^I confirm the details above are correct$",)
            ),
            blank_values=("false",),
        ),
    ),
    max_commit_mutations=1,
    ignored_intake_keys=(
        # Entered through derived facts: the destination as the combobox
        # shows it, and the single traveller's name and date of birth.
        "destination_name",
        "destination_region",
        "travelers",
        # No control for free-text trip notes on any screen of the dev case.
        "itinerary",
        # Cost items are not entered: the total is typed, and a case without
        # one stops at the required cost field (the calculator is not used).
        "cost_flights",
        "cost_lodging",
        "cost_tours",
        "cost_other",
    ),
)


def _mdy(iso: str) -> str:
    year, month, day = iso.split("-")
    return f"{month}/{day}/{year}"


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Intake facts restated the way the portal shows them.

    - The destination entry: "<name> - <region>".
    - With exactly one traveller: that traveller's name and date of birth,
      and the summary's "Name (MM/DD/YYYY)". With any other count no
      traveller fact is given, so the run stops at the traveller step.
    """
    out: dict[str, Any] = {}
    name = str(raw.get("destination_name") or "").strip()
    region = str(raw.get("destination_region") or "").strip()
    if name and region:
        out[DESTINATION_KEY] = f"{name} - {region}"
    travellers = raw.get("travelers")
    if isinstance(travellers, list) and len(travellers) == 1:
        t = travellers[0] if isinstance(travellers[0], dict) else {}
        t_name = str(t.get("name") or "").strip()
        t_dob = str(t.get("dob") or "").strip()
        if t_name and re.fullmatch(r"\d{4}-\d{2}-\d{2}", t_dob):
            out[T1_NAME] = t_name
            out[T1_DOB] = t_dob
            out[TRAVELLERS_KEY] = f"{t_name} ({_mdy(t_dob)})"
    return out
