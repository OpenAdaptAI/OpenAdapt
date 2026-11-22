"""Hollowfen BOP quote: unchanged from ``packs5/hollowfen.py`` apart from its pack id
(this portal uses none of the GUI mechanisms).

The original notes:

Hollowfen BOP rating worksheet: form pack written from the dev case only (data only).

Written from the hbm-d01 intake, the hollowfen intake vocabulary and the
dev-case demonstration (``form_tx_set4v2_run/demos/hollowfen.jsonl``); no
portal source, scored intake or answer key was read. What the dev case
showed:

- A producer sign-in page, then one worksheet page (``/bop/worksheet``) with
  three tables (applicant, location, coverage), each with its own "Save"
  button, and "Rate this risk" at the bottom. Every control has a visible
  label and a ``name`` equal to its intake key; the surface sees no section
  headings on the worksheet.
- Each "Save" stores only its own table and reloads the page at that table's
  fragment (``?saved=location#location``); values typed in the other tables
  are not kept. "Rate this risk" rates only what was saved. Found by dev-case
  smoke runs of this pack, not from the demonstration. The layer tells the
  steps apart by the fragment (``#location:target`` and so on).
- "Look up entity" next to the entity number checked a state registry; on
  the dev case it found no match ("Enter the entity details manually") and
  filled nothing. The entity number is not part of the stored application,
  so the pack neither types it nor runs the lookup; every applicant field is
  typed from the intake.
- The class code is read-only and set by "Find class...", which opens a
  class finder window: a search box ("Search classes"), "Search", and one
  "Select" button per result row; choosing a row writes the class back and
  closes the window. The dev case searched the class keyword and the list
  had one row. The row shows the code and its name, which no intake key
  holds, so the row cannot be keyed: with more than one result the Select
  button is ambiguous and the run stops.
- "Open BPP worksheet" (equipment, inventory, furniture and fixtures, a
  calculated total and "Copy to limit") was never opened, so it is not
  declared. The dev intake gives the BPP limit, which is typed. The
  vocabulary defines an absent limit as the sum of the itemised amounts;
  ``derived_facts`` restates that sum only when all three items are given,
  otherwise the run stops at the limit for a person.
- The property deductible showed $1,000 on the blank form, which is also
  the dev intake's deductible, so it is not proven to be a portal default
  and is not declared as one: a case with another deductible stops there.
- "Rate this risk" leads to a "Rating summary" review (money and Yes/No
  formatting, MM/DD/YYYY date) with "Submit application"; the submitted
  record is then shown under "Application received" with raw stored values,
  and the reference (HBM-...) is in the page text and URL.

Dev-case smoke: the record is stored correctly, but the run ends
readback_incomplete, never verified. Every worksheet step shows every field,
so the layer records each field once per step; the result page is read back
once per step to cover those records (one stored value, read four times).
The class keyword typed in the finder and the class code it returns stay
not_checked: the record never shows the keyword, and the finder shows its
result only as a table row, not as a readable result.
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
    PrefillPolicy,
    SummaryCheckSpec,
    UIProfileSpec,
    UnboundControlSpec,
    WizardScreenSpec,
)

CASE_PREFIX = "hbm"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
FOLD = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
DATE_MDY = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="MDY")
DATE_ISO = FieldCodecSpec(kind=FormCodecKind.ISO_DATE, date_order="YMD")
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)
# The deductible select's option codes and the text it shows (compiler report).
DEDUCTIBLE = FieldCodecSpec(
    kind=FormCodecKind.ENUM_MAP,
    enum_aliases={
        "500": ("$500",),
        "1000": ("$1,000",),
        "2500": ("$2,500",),
        "5000": ("$5,000",),
    },
)

PLACEHOLDERS = ("", "-- select --", "-- Select --")

REVIEW_SECTION = (r"^Rating summary$",)
RESULT_SECTION = (r"^Application received$",)

SELECT = ControlKind.SELECT

# (dossier key, form label, control kind, entry codec, optional)
FIELDS: tuple[tuple[str, str, ControlKind, FieldCodecSpec, bool], ...] = (
    ("legal_name", "Legal name", ControlKind.TEXT, TEXT, False),
    ("dba_name", r"Doing business as \(DBA\)", ControlKind.TEXT, TEXT, True),
    ("entity_type", "Entity type", SELECT, FOLD, False),
    ("fein", "FEIN", ControlKind.TEXT, TEXT, False),
    ("year_established", "Year established", ControlKind.TEXT, INT, False),
    ("location_street", "Location street address", ControlKind.TEXT, TEXT, False),
    ("employees", "Number of employees", ControlKind.TEXT, INT, False),
    ("annual_revenue", r"Annual gross sales \(\$\)", ControlKind.TEXT, INT, False),
    ("building_owned", r"Does the insured own the building\?", SELECT, BOOL, False),
    ("building_limit", r"Building limit \(\$\)", ControlKind.TEXT, INT, False),
    (
        "bpp_limit",
        r"Business personal property limit \(\$\)",
        ControlKind.TEXT,
        INT,
        False,
    ),
    ("liability_limit", "Liability limits", SELECT, TEXT, False),
    ("deductible", "Property deductible", SELECT, DEDUCTIBLE, False),
    ("effective_date", "Effective date", ControlKind.TEXT, DATE_MDY, False),
)


def _bind(
    key: str, label: str, kind: ControlKind, codec: FieldCodecSpec, optional: bool
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"hbm_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",),
            name_or_id_patterns=(f"^{key}(?:\\s|$)",),
        ),
        control_kind=kind,
        codec=codec,
        prefill_policy=PrefillPolicy.HALT_ON_CONFLICT,
        placeholder_values=PLACEHOLDERS,
        optional=optional,
    )


def _shown(
    key: str,
    label: str,
    codec: FieldCodecSpec,
    section: tuple[str, ...],
    tag: str = "",
) -> FieldBindingSpec:
    stem = "hbm_rb" if section is RESULT_SECTION else "hbm_rv"
    return FieldBindingSpec(
        binding_id=f"{stem}{tag}_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(f"^{label}$",), section_patterns=section
        ),
        codec=codec,
    )


# (dossier key, review label, review codec, result label, result codec)
READBACKS: tuple[tuple[str, str, FieldCodecSpec, str, FieldCodecSpec], ...] = (
    ("legal_name", "Legal name", TEXT, "legal name", TEXT),
    ("dba_name", r"Doing business as \(DBA\)", TEXT, "dba name", TEXT),
    ("entity_type", "Entity type", FOLD, "entity type", FOLD),
    ("fein", "FEIN", TEXT, "fein", TEXT),
    ("year_established", "Year established", INT, "year established", INT),
    ("location_street", "Location street address", TEXT, "location street", TEXT),
    ("employees", "Number of employees", INT, "employees", INT),
    ("annual_revenue", "Annual gross sales", MONEY, "annual revenue", MONEY),
    ("building_owned", "Owns building", BOOL, "building owned", BOOL),
    ("building_limit", "Building limit", MONEY, "building limit", MONEY),
    (
        "bpp_limit",
        "Business personal property limit",
        MONEY,
        "bpp limit",
        MONEY,
    ),
    ("liability_limit", "Liability limits", TEXT, "liability limit", TEXT),
    ("deductible", "Property deductible", MONEY, "deductible", MONEY),
    ("effective_date", "Effective date", DATE_MDY, "effective date", DATE_ISO),
)


def _ws(screen_id: str, landmark: str, button: str) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=screen_id,
        landmark_selector=landmark,
        next_button_anchor=FieldAnchorSpec(label_patterns=(button,)),
    )


BPP_ITEMS = ("bpp_equipment", "bpp_inventory", "bpp_furniture")

WORKSHEET_STEPS = ("location", "coverage", "applicant", "rate")

PROFILE = UIProfileSpec(
    profile_id="set4v2_hollowfen",
    auth_wall_selectors=('input[type="password"]',),
)

CLASS_FINDER = PopupSubroutineSpec(
    subroutine_id="hbm_class_finder",
    popup_mode="window",
    popup_url_pattern=r"/bop/classfinder",
    trigger_anchor=FieldAnchorSpec(label_patterns=(r"^Find class\.\.\.$",)),
    bindings=(
        FieldBindingSpec(
            binding_id="hbm_class_keyword",
            dossier_key="class_keyword",
            anchor=FieldAnchorSpec(
                label_patterns=(r"^Search classes$",),
                name_or_id_patterns=(r"^q(?:\s|$)",),
            ),
            codec=TEXT,
        ),
    ),
    compute_anchors=(
        FieldAnchorSpec(
            label_patterns=(r"^Search$",), section_patterns=(r"^Class finder$",)
        ),
    ),
    commit_anchor=FieldAnchorSpec(
        label_patterns=(r"^Select$",), section_patterns=(r"^Class finder$",)
    ),
    parent_return_anchor=FieldAnchorSpec(
        label_patterns=(r"^Class code$",), name_or_id_patterns=(r"^class_code\b",)
    ),
    return_output_key="class_code",
    return_codec=TEXT,
)

PACK = FormPack(
    pack_id="set4v2gui.hollowfen.bop.v1",
    domain_schema="commercial_lines.bop",
    ui_profile_id=PROFILE.profile_id,
    bindings=tuple(_bind(*f) for f in FIELDS),
    popup_subroutines=(CLASS_FINDER,),
    screens=(
        WizardScreenSpec(
            screen_id="hbm_review",
            title_pattern=r"^Rating summary$",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(
                label_patterns=(r"^Submit application$",),
                section_patterns=REVIEW_SECTION,
            ),
        ),
        # The worksheet is one page; each "Save" reloads it at its section's
        # fragment (``#applicant``, ``#location``, ``#coverage``), which is
        # how the layer tells the steps apart.
        _ws("hbm_rate", "#applicant:target", r"^Rate this risk$"),
        _ws("hbm_applicant", "#coverage:target", r"^Save applicant$"),
        _ws("hbm_coverage", "#location:target", r"^Save coverage$"),
        _ws("hbm_location", "#en", r"^Save location$"),
    ),
    summary_check=SummaryCheckSpec(
        review_readbacks=tuple(
            _shown(k, lbl, c, REVIEW_SECTION) for k, lbl, c, _, _ in READBACKS
        ),
        # One read-back of the stored record per worksheet step: every step
        # shows every field, so the layer records each field once per step.
        field_readbacks=tuple(
            _shown(k, lbl, c, RESULT_SECTION, f"_{step}")
            for step in WORKSHEET_STEPS
            for k, _, _, lbl, c in READBACKS
        )
        + (_shown("class_code", "class code", TEXT, RESULT_SECTION),),
        outputs=(
            OutputExtractionSpec(
                output_key="application.reference",
                anchor=FieldAnchorSpec(
                    label_patterns=(
                        r"^(?:Application|Reference|Quote) (?:number|reference)$",
                    ),
                    section_patterns=RESULT_SECTION,
                ),
                value_pattern=r"\b(HBM-\d+)\b",
            ),
        ),
    ),
    max_commit_mutations=1,
    unbound_controls=(
        # The entity number only feeds the registry lookup, which the pack
        # does not run; it is not part of the stored application.
        UnboundControlSpec(
            anchor=FieldAnchorSpec(
                label_patterns=(r"^State entity number$",),
                name_or_id_patterns=(r"^entity_number\b",),
            )
        ),
        # The BPP worksheet launcher is never opened (see the docstring).
        UnboundControlSpec(
            anchor=FieldAnchorSpec(label_patterns=(r"^Open BPP worksheet$",))
        ),
    ),
    required_intake_keys=(
        "legal_name",
        "entity_type",
        "fein",
        "year_established",
        "location_street",
        "class_keyword",
        "employees",
        "annual_revenue",
        "building_owned",
        "liability_limit",
        "deductible",
        "effective_date",
    ),
    ignored_intake_keys=(
        # Only used by the registry lookup (see unbound_controls).
        "entity_number",
        # The portal asks for neither (no field on the worksheet).
        "class_description",
        "operations_notes",
        # The BPP worksheet is not declared: the limit is typed, given or
        # restated as the items' sum by derived_facts.
        *BPP_ITEMS,
    ),
)


def _money(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value) == int(value) else None
    text = str(value).replace("$", "").replace(",", "").strip()
    return int(text) if text.isdigit() else None


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Intake facts restated as the worksheet asks for them.

    - The BPP limit, when absent, is the sum of the itemised amounts (the
      intake vocabulary's own definition), only when all three are given.
    - The building limit, when absent and the building is not owned, is 0
      (the intake vocabulary's and the form's own rule).
    """
    out: dict[str, Any] = {}
    if raw.get("bpp_limit") in (None, ""):
        items = [_money(raw.get(k)) for k in BPP_ITEMS]
        if all(v is not None for v in items):
            out["bpp_limit"] = sum(v for v in items if v is not None)
    if raw.get("building_limit") in (None, "") and raw.get("building_owned") is False:
        out["building_limit"] = 0
    return out
