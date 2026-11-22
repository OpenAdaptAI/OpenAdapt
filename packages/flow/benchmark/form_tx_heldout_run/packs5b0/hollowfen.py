"""Hollowfen BOP rating worksheet: form pack (data only).

Written from the hbm-d01 intake, the hollowfen intake vocabulary and the
dev-case demonstration (``form_tx_set4v2_run/demos/hollowfen.jsonl``), plus
dev-case runs of this pack; no portal source, scored intake or answer key
was read. What the dev case showed:

- A producer sign-in page (password field), then one worksheet page
  (``/bop/worksheet``) with three tables: Applicant, Location and Coverage,
  each with its own Save button, and a "Rate this risk" button. Every
  control has a ``name`` equal to its intake key and a visible label; the
  worksheet controls carry no section text.
- "Look up entity" checks the state entity number against a registry; on the
  dev case it found no match ("Enter the entity details manually"). The
  entity number is not on the rating summary or the stored record.
- "Find class..." opens a class finder: search box, Search, then one
  "Select" button per result row. Choosing a row fills the read-only class
  code ("21890 Picture framing shop"). The row text never equals an intake
  value, so a search that lists several rows stops for a person.
- "Open BPP worksheet" (equipment, inventory, furniture and fixtures) was not
  opened on the dev case; its controls were never seen, so it is not
  declared.
- Money is typed as bare digits, the effective date as MM/DD/YYYY.
- The property deductible shows $1,000 on the blank form, before anything was
  typed (a portal default; it happens to equal the dev intake's value).
- "Rate this risk" shows a rating summary with "Submit application"; after
  submit the page "Application received" lists the stored record as a table
  (raw values: ``False``, ``290000``, ``2027-01-01``), and the application
  reference (HBM-...) is in the URL.
- The stored record also has an empty "hnoa" row that no dev-case control
  fed and no intake key grounds. If a control named ``hnoa`` shows up on a
  case, it is bound to a key no intake gives, so the run stops for a person
  instead of submitting it unanswered.

Sections. "Rate this risk" rates only saved sections (found on the dev case:
with one screen and no saves, rating stopped and every field came back
blank). Each Save reloads the page with the saved section as the URL
fragment (``#applicant``, ``#location``, ``#coverage``), so the screens are
told apart by ``:target``. The class finder runs on the first screen the
layer sees, and the class code it puts back lives only in the page until
Location is saved (with Applicant saved first, the code was lost). So
Location is saved first, then Applicant, then Coverage, then the risk is
rated. No section link or "Change" control was seen, so no screen declares
``revisit_selector``.

Read-back limits on the dev case: the class finder's search word is not
shown anywhere after submit, and the finder shows no labelled result to
match the returned class code against, so both are reported not_checked and
the run ends ``readback_incomplete`` even when every stored value is right.
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
    PopupSubroutineSpec,
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
DATE_ISO = FieldCodecSpec(kind=FormCodecKind.ISO_DATE)
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")
BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)

PLACEHOLDERS = ("", "-- select --", "-- Select --")

RESULT_SECTION = (r"^Application received$",)
CLASS_KEY = "class_code"


def _p(label: str) -> str:
    return "^" + re.escape(label) + "$"


def _bind(
    key: str,
    label: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
    *,
    name: str | None = None,
    defaults: tuple[str, ...] = (),
    required: bool = False,
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"hbm_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(_p(label),),
            name_or_id_patterns=(rf"^{name or key}(?:\s|$)",),
        ),
        control_kind=kind,
        codec=codec,
        placeholder_values=PLACEHOLDERS,
        portal_default_values=defaults,
        required_in_pack=required,
    )


def _shown(key: str, label: str, codec: FieldCodecSpec = TEXT) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"hbm_rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(
            label_patterns=(_p(label),), section_patterns=RESULT_SECTION
        ),
        codec=codec,
    )


def _section(screen_id: str, landmark: str, button: str) -> WizardScreenSpec:
    return WizardScreenSpec(
        screen_id=screen_id,
        landmark_selector=landmark,
        next_button_anchor=FieldAnchorSpec(label_patterns=(_p(button),)),
    )


PROFILE = UIProfileSpec(
    profile_id="set4v2b0_hollowfen",
    auth_wall_selectors=('input[type="password"]',),
)

CLASS_FINDER = PopupSubroutineSpec(
    subroutine_id="hbm_class_finder",
    trigger_anchor=FieldAnchorSpec(label_patterns=(_p("Find class..."),)),
    popup_mode="window",
    popup_url_pattern=r"/bop/classfinder",
    bindings=(
        FieldBindingSpec(
            binding_id="hbm_class_keyword",
            dossier_key="class_keyword",
            anchor=FieldAnchorSpec(
                label_patterns=(_p("Search classes"),),
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
        label_patterns=(_p("Class code"),),
        name_or_id_patterns=(r"^class_code(?:\s|$)",),
    ),
    return_output_key=CLASS_KEY,
    return_codec=TEXT,
)

PACK = FormPack(
    pack_id="set4v2b0.hollowfen.bop.v1",
    domain_schema="commercial_lines.bop",
    ui_profile_id=PROFILE.profile_id,
    bindings=(
        # Applicant
        _bind("legal_name", "Legal name", required=True),
        _bind("dba_name", "Doing business as (DBA)"),
        _bind("entity_type", "Entity type", ControlKind.SELECT, FOLD, required=True),
        _bind("fein", "FEIN", required=True),
        _bind("year_established", "Year established", codec=INT, required=True),
        # Location
        _bind("location_street", "Location street address", required=True),
        _bind("employees", "Number of employees", codec=INT, required=True),
        _bind("annual_revenue", "Annual gross sales ($)", codec=INT, required=True),
        _bind(
            "building_owned",
            "Does the insured own the building?",
            ControlKind.SELECT,
            BOOL,
            required=True,
        ),
        _bind("building_limit", "Building limit ($)", codec=INT, required=True),
        # Coverage
        _bind(
            "bpp_limit",
            "Business personal property limit ($)",
            codec=INT,
            required=True,
        ),
        _bind(
            "liability_limit",
            "Liability limits",
            ControlKind.SELECT,
            TEXT,
            required=True,
        ),
        _bind(
            "deductible",
            "Property deductible",
            ControlKind.SELECT,
            MONEY,
            defaults=("1000", "$1,000"),
            required=True,
        ),
        _bind(
            "effective_date",
            "Effective date",
            codec=DATE_MDY,
            required=True,
        ),
        # Never seen on the dev case (see the module notes): a stop if shown.
        FieldBindingSpec(
            binding_id="hbm_hnoa",
            dossier_key="hnoa_not_in_intake",
            anchor=FieldAnchorSpec(name_or_id_patterns=(r"^hnoa(?:\s|$)",)),
            required_in_pack=True,
            placeholder_values=PLACEHOLDERS,
        ),
    ),
    popup_subroutines=(CLASS_FINDER,),
    screens=(
        WizardScreenSpec(
            screen_id="hbm_summary",
            title_pattern=r"^Rating summary$",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(
                label_patterns=(r"^Submit application$",)
            ),
        ),
        _section("hbm_rate", "#coverage:target", "Rate this risk"),
        _section("hbm_coverage", "#applicant:target", "Save coverage"),
        _section("hbm_applicant", "#location:target", "Save applicant"),
        _section("hbm_location", "#rate", "Save location"),
    ),
    summary_check=SummaryCheckSpec(
        field_readbacks=(
            _shown("legal_name", "legal name"),
            _shown("dba_name", "dba name"),
            _shown("entity_type", "entity type", FOLD),
            _shown("fein", "fein"),
            _shown("year_established", "year established", INT),
            _shown("location_street", "location street", FOLD),
            _shown(CLASS_KEY, "class code", FOLD),
            _shown("employees", "employees", INT),
            _shown("annual_revenue", "annual revenue", MONEY),
            _shown("building_owned", "building owned", BOOL),
            _shown("building_limit", "building limit", MONEY),
            _shown("bpp_limit", "bpp limit", MONEY),
            _shown("liability_limit", "liability limit"),
            _shown("deductible", "deductible", MONEY),
            _shown("effective_date", "effective date", DATE_ISO),
        ),
        outputs=(
            OutputExtractionSpec(
                output_key="application.reference",
                anchor=FieldAnchorSpec(
                    label_patterns=(r"^(?:application|reference|quote)\b.*$",),
                    section_patterns=RESULT_SECTION,
                ),
                value_pattern=r"\b(HBM-\d+)\b",
                required=True,
            ),
        ),
    ),
    max_commit_mutations=1,
    unbound_controls=(
        # The registry lookup is not used: the entity number is not stored
        # with the application, and a registry match could prefill details
        # that differ from the intake.
        UnboundControlSpec(
            anchor=FieldAnchorSpec(
                label_patterns=(_p("State entity number"),),
                name_or_id_patterns=(r"^entity_number(?:\s|$)",),
            ),
        ),
    ),
    ignored_intake_keys=(
        # Not stored with the application (see unbound_controls).
        "entity_number",
        # No control asks for it; the class is chosen through the finder.
        "class_description",
        # No control asks for free-text operations notes.
        "operations_notes",
        # Entered as their sum in the BPP limit (derived_facts) when the
        # intake gives no BPP limit.
        "bpp_equipment",
        "bpp_inventory",
        "bpp_furniture",
    ),
)

_BPP_ITEMS = ("bpp_equipment", "bpp_inventory", "bpp_furniture")


def _money(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else None
    text = str(value).replace("$", "").replace(",", "").strip()
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else None


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """The BPP limit as the sum of its items, when the intake gives no limit.

    The intake vocabulary says an absent BPP limit is the sum of the bpp_*
    items. Only when all three items are given; otherwise the limit stays
    ungrounded and the run stops for a person.
    """
    out: dict[str, Any] = {}
    if raw.get("bpp_limit") in (None, ""):
        items = [_money(raw.get(k)) for k in _BPP_ITEMS]
        if all(v is not None for v in items):
            out["bpp_limit"] = sum(v for v in items if v is not None)
    return out
