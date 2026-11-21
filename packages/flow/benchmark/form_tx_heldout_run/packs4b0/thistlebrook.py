"""Thistlebrook pet health quote: form pack (data only).

Written from the tb-dev intake, the thistlebrook intake vocabulary and the
dev-case demonstration (``form_tx_heldout4_run/demos/thistlebrook.jsonl``)
only; no portal source, scored intake or answer key was read. What the
demonstration shows:

- A sign-in page (password field), then one page (``/quote``) that shows one
  card at a time, each with the same "Next" (and "Back") button: name;
  contact; pet's name and species; breed, weight and birth month and year;
  health questions; plan; policy start date. Then "Check <pet>'s quote" with
  one "Change" button per section and "Get my quote".
- Choice questions are ARIA radio groups (buttons with role radio) with a
  visible group label and no name or id; text and select controls have ids.
  So every binding is anchored by its visible label only, and all cards are
  one declared screen: the layer moves on when the labels change.
- The breed is a type-ahead combobox over a breed list.
- The birth date is asked as a month name and a year (two selects); no day.
- The weight box is labelled "Weight (lb)"; no unit control was shown.
- The start date is typed as MM/DD/YYYY and the blank form shows a date
  computed by the portal (the day after the dev run).
- On the blank plan card the annual limit ($10,000), reimbursement (80%) and
  deductible ($250) are already selected (portal defaults) and the wellness
  box is unticked. The dev intake asks for the same values.
- No question for mixed breed, spay or neuter date, time outdoors, indoor
  only or condition notes appeared on the dev case. They are bound by guessed
  labels; when a guess does not match, the intake key is not entered and the
  run cannot reach ``verified_done``.
- After "Get my quote" the page "<pet>'s quote is ready" lists every stored
  value as a definition list; the quote reference (TPH-...) is in the URL.
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
    WizardScreenSpec,
)

CASE_PREFIX = "tb"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
FOLD = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
DATE_MDY = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)
PHONE = FieldCodecSpec(kind=FormCodecKind.PHONE_DIGITS)

RADIO = ControlKind.RADIO_GROUP
SELECT = ControlKind.SELECT

PLACEHOLDERS = ("", "Month", "Year")

RESULT_SECTION = (r"quote is ready$",)

_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)  # fmt: skip

# Derived keys (see derived_facts).
PHONE_KEY = "owner.phone_shown"
BREED_KEY = "pet.breed_entry"
WEIGHT_KEY = "pet.weight_lb"
BIRTH_MONTH_KEY = "pet.birth_month"
BIRTH_YEAR_KEY = "pet.birth_year"

# Guessed labels for questions the dev case never showed (form and result).
SPAY_DATE = (
    r"(?:spay|neuter)\w*\b.*\b(?:date|when)\b|\b(?:date|when)\b.*(?:spay|neuter)"
)
OUTDOOR = (
    r"\bminutes?\b.*\b(?:outdoors?|outside)\b|\b(?:outdoors?|outside)\b.*\bminutes?\b"
)
INDOOR = r"\bindoors?\b.*\bonly\b|\bonly\b.*\bindoors?\b"
_NOTE_WORD = r"(?:notes?|details?|describe|tell us)"
_HEALTH_WORD = r"(?:conditions?|illness|injury)"
NOTES = rf"\b{_NOTE_WORD}\b.*\b{_HEALTH_WORD}\b|\b{_HEALTH_WORD}\b.*\b{_NOTE_WORD}\b"


def _p(label: str) -> str:
    return "^" + re.escape(label) + "$"


def _bind(
    key: str,
    label: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    exact: bool = True,
    defaults: tuple[str, ...] = (),
    required: bool = False,
    rerender: bool = False,
    typeahead: int | None = None,
    prefill: PrefillPolicy = PrefillPolicy.HALT_ON_CONFLICT,
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"tb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(label_patterns=(_p(label) if exact else label,)),
        control_kind=kind,
        codec=codec,
        prefill_policy=prefill,
        placeholder_values=PLACEHOLDERS,
        portal_default_values=defaults,
        required_in_pack=required,
        triggers_re_render=rerender,
        typeahead_query_chars=typeahead,
    )


def _shown(
    key: str, label: str, codec: FieldCodecSpec = TEXT, *, exact: bool = True
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"tb_rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(_p(label) if exact else label,),
            section_patterns=RESULT_SECTION,
        ),
        codec=codec,
    )


PROFILE = UIProfileSpec(
    profile_id="heldout4b0_thistlebrook",
    auth_wall_selectors=('input[type="password"]',),
)

# The review's first "Change" button brings the cards back to the first one
# (only on the review screen; not exercised on the dev case).
_FIRST_CHANGE = "#sum > div:nth-of-type(1) > button"

PACK = FormPack(
    pack_id="heldout4b0.thistlebrook.pet.v1",
    domain_schema="personal_lines.pet_health",
    ui_profile_id=PROFILE.profile_id,
    bindings=(
        # You
        _bind("owner.first_name", "First name", required=True),
        _bind("owner.last_name", "Last name", required=True),
        # Contact
        _bind("owner.email", "Email", required=True),
        _bind(PHONE_KEY, "Mobile number", required=True),
        _bind("owner.zip", "ZIP code", required=True),
        # Your pet
        _bind("pet.name", "Pet's name", required=True),
        _bind("pet.species", "Species", RADIO, FOLD, required=True, rerender=True),
        # Breed and age
        _bind(
            BREED_KEY,
            "Breed",
            ControlKind.PORTAL_COMBOBOX,
            TEXT,
            required=True,
        ),
        _bind(WEIGHT_KEY, "Weight (lb)", codec=INT, required=True),
        _bind(BIRTH_MONTH_KEY, "Birth month", SELECT, FOLD, required=True),
        _bind(BIRTH_YEAR_KEY, "Birth year", SELECT, FOLD, required=True),
        # Health
        _bind(
            "pet.spayed_neutered",
            "Spayed or neutered",
            RADIO,
            BOOL,
            required=True,
            rerender=True,
        ),
        _bind("pet.spay_neuter_date", SPAY_DATE, codec=DATE_MDY, exact=False),
        _bind(
            "pet.recent_illness",
            "Illness or injury in the last 12 months",
            RADIO,
            BOOL,
            required=True,
            rerender=True,
        ),
        _bind("pet.condition_notes", NOTES, exact=False),
        _bind("pet.outdoor_minutes_per_day", OUTDOOR, codec=INT, exact=False),
        _bind("pet.indoor_only", INDOOR, RADIO, FOLD, exact=False, rerender=True),
        # Plan (three radios come pre-selected on the blank card)
        _bind(
            "plan.annual_limit",
            "Annual limit",
            RADIO,
            FOLD,
            defaults=("$10,000",),
            required=True,
        ),
        _bind(
            "plan.reimbursement",
            "Reimbursement",
            RADIO,
            FOLD,
            defaults=("80%",),
            required=True,
        ),
        _bind(
            "plan.deductible",
            "Annual deductible",
            RADIO,
            MONEY,
            defaults=("250", "$250"),
            required=True,
        ),
        _bind(
            "plan.wellness",
            "Add routine care (wellness)",
            ControlKind.CHECKBOX,
            BOOL,
            defaults=("false",),
            required=True,
        ),
        # Start date: the blank card shows a date the portal computes from
        # today, so it differs between runs; the intake date is typed over it.
        _bind(
            "policy.start_date",
            "Policy start date",
            codec=DATE_MDY,
            defaults=("11/22/2026",),
            required=True,
            prefill=PrefillPolicy.OVERWRITE_WITH_DOSSIER,
        ),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="tb_review",
            landmark_selector="#get-quote",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Get my quote$",)),
        ),
        WizardScreenSpec(
            screen_id="tb_card",
            landmark_selector="#next",
            next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Next$",)),
            revisit_selector=_FIRST_CHANGE,
        ),
    ),
    summary_check=SummaryCheckSpec(
        field_readbacks=(
            _shown("owner.first_name", "First name"),
            _shown("owner.last_name", "Last name"),
            _shown("owner.email", "Email", FOLD),
            _shown(PHONE_KEY, "Mobile number", PHONE),
            _shown("owner.zip", "ZIP code"),
            _shown("pet.name", "Pet's name"),
            _shown("pet.species", "Species", FOLD),
            _shown(BREED_KEY, "Breed", FOLD),
            _shown(WEIGHT_KEY, "Weight (lb)", INT),
            _shown(BIRTH_MONTH_KEY, "Birth month", FOLD),
            _shown(BIRTH_YEAR_KEY, "Birth year", FOLD),
            _shown("pet.spayed_neutered", "Spayed or neutered", BOOL),
            _shown("pet.spay_neuter_date", SPAY_DATE, DATE_MDY, exact=False),
            _shown(
                "pet.recent_illness", "Illness or injury in the last 12 months", BOOL
            ),
            _shown("pet.condition_notes", NOTES, exact=False),
            _shown("pet.outdoor_minutes_per_day", OUTDOOR, INT, exact=False),
            _shown("pet.indoor_only", INDOOR, FOLD, exact=False),
            _shown("plan.annual_limit", "Annual limit", FOLD),
            _shown("plan.reimbursement", "Reimbursement", FOLD),
            _shown("plan.deductible", "Annual deductible", MONEY),
            _shown("plan.wellness", "Add routine care (wellness)", BOOL),
            _shown("policy.start_date", "Policy start date", DATE_MDY),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.reference",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^Quote (?:reference|number|ID)$",),
                    section_patterns=RESULT_SECTION,
                ),
                value_pattern=r"\b(TPH-[0-9A-Z]+)\b",
                required=True,
            ),
        ),
    ),
    max_commit_mutations=1,
    ignored_intake_keys=(
        # Entered through derived facts in the form the portal asks for.
        "owner.phone",
        "pet.breed",
        "pet.weight",
        "pet.weight_unit",
        "pet.birth_date",
        # No mixed-breed question was shown; a mixed breed withholds the
        # breed fact (derived_facts), so the breed stops for a person.
        "pet.mixed_breed",
    ),
)


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Intake facts restated the way the portal asks for and shows them.

    - The mobile number as ddd-ddd-dddd (as the review shows it).
    - The breed, unless the intake says mixed breed (no such question was
      seen, so a person decides how to enter it).
    - The weight, only when the intake gives it in pounds (the box is
      labelled "Weight (lb)"; no conversion).
    - The birth month name and year (the portal asks for no day).
    """
    out: dict[str, Any] = {}
    digits = re.sub(r"\D", "", str(raw.get("owner.phone") or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        out[PHONE_KEY] = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    breed = str(raw.get("pet.breed") or "").strip()
    if breed and raw.get("pet.mixed_breed") is not True:
        out[BREED_KEY] = breed
    weight = raw.get("pet.weight")
    if weight is not None and str(raw.get("pet.weight_unit") or "") == "lb":
        out[WEIGHT_KEY] = weight
    iso = str(raw.get("pet.birth_date") or "")
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso)
    if m and 1 <= int(m.group(2)) <= 12:
        out[BIRTH_MONTH_KEY] = _MONTHS[int(m.group(2)) - 1]
        out[BIRTH_YEAR_KEY] = m.group(1)
    return out
