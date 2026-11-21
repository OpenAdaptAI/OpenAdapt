"""Pellbrook boat quote: form pack written from the dev case only (data only).

Written from the pb-dev intake, the pellbrook intake vocabulary and the
dev-case demonstration (``form_tx_heldout3_run/demos/pellbrook.jsonl``); no
portal source, scored intake or answer key was read. What the dev case
showed:

- A sign-in page, then one page (``/boat/quote``) that asks one question at
  a time as a chat-like thread. Every text question reuses the same control
  (``#answer``) and every choice question is a radio group named
  ``answer``, so each binding is anchored by the question's label only.
  Every question has the same "Continue" button.
- Dates are typed as MM/DD/YYYY; the hull value is typed as digits and
  shown back as money. "Agreed hull value" has a "Look up book value"
  button; the dev case typed the value and never opened it, so its window
  is not declared and a case without a hull value stops for a person.
- The lienholder question says "Leave blank if there is no loan."; the
  summary then shows "None".
- A lay-up period appears on the summary but was not asked on the dev case;
  its question is bound by the summary's label and was never exercised.
- "Here's what we have" lists every answer with a "Change" button per row;
  "Get my quote" submits. The stored quote is then shown under "Your boat
  quote" as a table, and the quote reference appears in the page title and
  URL.
- The first snapshot of every question showed it blank (no radio
  pre-checked), so no binding declares portal defaults.

All questions are one declared screen; the layer advances when the question
label changes. A financed case's lienholder is typed but, with no read-back
keyed to it, is reported not_checked (the derived read-back covers what the
summary shows).
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

CASE_PREFIX = "pb"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
FOLD = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
DATE_MDY = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)

SUMMARY_SECTION = (r"^Your boat quote$",)

RADIO = ControlKind.RADIO_GROUP

# (dossier key, question label, control kind, entry codec, read-back codec)
FIELDS: tuple[tuple[str, str, ControlKind, FieldCodecSpec, FieldCodecSpec], ...] = (
    ("first_name", "First name", ControlKind.TEXT, TEXT, TEXT),
    ("last_name", "Last name", ControlKind.TEXT, TEXT, TEXT),
    ("date_of_birth", "Date of birth", ControlKind.TEXT, DATE_MDY, DATE_MDY),
    ("email", "Email address", ControlKind.TEXT, FOLD, FOLD),
    ("garaging_zip", "ZIP code where the boat is kept", ControlKind.TEXT, TEXT, TEXT),
    ("boat_year", "Model year", ControlKind.TEXT, INT, INT),
    ("boat_make", "Manufacturer", ControlKind.TEXT, TEXT, FOLD),
    ("boat_model", "Model", ControlKind.TEXT, TEXT, FOLD),
    ("length_ft", "Length overall (feet)", ControlKind.TEXT, INT, INT),
    ("engine_type", "Engine type", RADIO, FOLD, FOLD),
    (
        "total_horsepower",
        "Total horsepower (all engines combined)",
        ControlKind.TEXT,
        INT,
        INT,
    ),
    ("engine_count", "Number of engines", RADIO, FOLD, FOLD),
    ("agreed_hull_value", "Agreed hull value", ControlKind.TEXT, INT, MONEY),
    (
        "storage",
        "Where is the boat kept during the boating season?",
        RADIO,
        FOLD,
        FOLD,
    ),
    ("layup_period", "Lay-up period (boat out of commission)", RADIO, FOLD, FOLD),
    ("navigation_area", "Where will you use the boat?", RADIO, FOLD, FOLD),
    (
        "safety_course",
        "Has the primary operator completed a boating safety course?",
        RADIO,
        BOOL,
        BOOL,
    ),
    (
        "losses_3yr",
        "Boat insurance claims in the last 3 years",
        ControlKind.TEXT,
        INT,
        INT,
    ),
    (
        "lienholder",
        "Lienholder (lender with a loan on this boat)",
        ControlKind.TEXT,
        TEXT,
        FOLD,
    ),
    ("liability_limit", "Liability limit", RADIO, MONEY, MONEY),
    ("deductible", "Hull deductible", RADIO, MONEY, MONEY),
    ("effective_date", "Coverage start date", ControlKind.TEXT, DATE_MDY, DATE_MDY),
)


def _pattern(label: str) -> str:
    return "^" + re.escape(label) + "$"


def _bind(
    key: str, label: str, kind: ControlKind, codec: FieldCodecSpec
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"pb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(label_patterns=(_pattern(label),)),
        control_kind=kind,
        codec=codec,
        prefill_policy=PrefillPolicy.HALT_ON_CONFLICT,
    )


def _shown(key: str, label: str, codec: FieldCodecSpec) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"pb_rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(_pattern(label),), section_patterns=SUMMARY_SECTION
        ),
        codec=codec,
    )


PROFILE = UIProfileSpec(
    profile_id="heldout3_pellbrook",
    auth_wall_selectors=('input[type="password"]',),
)

# The review's first "Change" button brings the thread back to its first
# question; it is only on the review screen (not exercised on the dev case).
_FIRST_CHANGE = "#summary > tbody > tr:nth-of-type(1) > td:nth-of-type(2) > button"

_NO_DIRECT_READBACK = frozenset({"lienholder", "layup_period"})

PACK = FormPack(
    pack_id="heldout3.pellbrook.boat.v1",
    domain_schema="personal_lines.watercraft",
    ui_profile_id=PROFILE.profile_id,
    bindings=tuple(_bind(k, lbl, kind, c) for k, lbl, kind, c, _ in FIELDS),
    screens=(
        WizardScreenSpec(
            screen_id="pb_review",
            landmark_selector="#get-quote",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Get my quote$",)),
        ),
        WizardScreenSpec(
            screen_id="pb_question",
            landmark_selector="#next",
            next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Continue$",)),
            revisit_selector=_FIRST_CHANGE,
        ),
    ),
    summary_check=SummaryCheckSpec(
        # A summary row for a key the intake does not give must be empty to
        # pass, but this summary shows "None" for an unanswered or skipped
        # question. So the lienholder is read back through a derived key, and
        # the lay-up period (skipped on the dev case) has no read-back: when
        # it is entered it is reported not_checked.
        field_readbacks=tuple(
            _shown(k, lbl, rb)
            for k, lbl, _, _, rb in FIELDS
            if k not in _NO_DIRECT_READBACK
        )
        + (
            _shown(
                "lienholder_shown",
                "Lienholder (lender with a loan on this boat)",
                FOLD,
            ),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="quote.reference",
                anchor=FieldAnchorSpec(label_patterns=(r"^Quote reference$",)),
                value_pattern=r"\b(PBM-\d+-Q)\b",
            ),
        ),
    ),
    max_commit_mutations=1,
    # financing is entered only through the lienholder (and its derived
    # "None" read-back); the hull value basis has no control of its own and
    # is never entered (the book-value lookup is not declared).
    ignored_intake_keys=("financing", "hull_value_basis"),
)


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """The lienholder as the summary shows it: the lender, or "None" when paid in full."""
    out: dict[str, Any] = {}
    lienholder = str(raw.get("lienholder") or "").strip()
    if lienholder:
        out["lienholder_shown"] = lienholder
    elif raw.get("financing") == "Paid in full":
        out["lienholder_shown"] = "None"
    return out
