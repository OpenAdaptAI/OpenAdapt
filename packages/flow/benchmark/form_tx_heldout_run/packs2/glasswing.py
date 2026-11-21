"""Glasswing renters quote widget: form pack (data only).

Written from the dev intake (gw-dev) and the dev-case demonstration
(``form_tx_heldout2_run/demos/glasswing.jsonl``) only. What the demonstration
shows: a resident site that embeds the quote widget in a same-origin iframe
(``/gw/embed``), the widget's controls inside one open shadow root, four
screens (About you, Your rental, Your belongings, Review), a street-address
type-ahead that fills read-only unit, city, state and ZIP, a belongings
calculator dialog (rooms, how furnished, valuables; Calculate, then Use this
amount), and a review list (definition list) of the stored values.
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
    PopupSubroutineSpec,
    SummaryCheckSpec,
    UIProfileSpec,
    WizardScreenSpec,
)

CASE_PREFIX = "gw"

TEXT = FieldCodecSpec(kind=FormCodecKind.WHITESPACE_COLLAPSE)
EXACT = FieldCodecSpec(kind=FormCodecKind.CASEFOLD)
INT = FieldCodecSpec(kind=FormCodecKind.INTEGER)
ISO = FieldCodecSpec(kind=FormCodecKind.ISO_DATE)
BOOL = FieldCodecSpec(kind=FormCodecKind.BOOLEAN_YES_NO)
MONEY = FieldCodecSpec(kind=FormCodecKind.CURRENCY_CENTS, currency_symbol="$")

PROFILE = UIProfileSpec(
    profile_id="heldout2_glasswing_embed",
    frame_path_patterns=(r"/gw/embed",),
    auth_wall_selectors=('input[type="password"]',),
)


def _lab(
    key: str,
    label: str,
    kind: ControlKind = ControlKind.TEXT,
    codec: FieldCodecSpec = TEXT,
) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"b_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(label_patterns=(f"^{label}$",)),
        control_kind=kind,
        codec=codec,
    )


def _shown(key: str, label: str, codec: FieldCodecSpec = TEXT) -> FieldBindingSpec:
    return FieldBindingSpec(
        binding_id=f"rb_{key}",
        dossier_key=key,
        anchor=FieldAnchorSpec(label_patterns=(f"^{label}$",)),
        codec=codec,
    )


SPRINKLER = r"Does your building have a fire sprinkler system\?"
PERSONAL_PROPERTY = r"Personal property coverage \(\$\)"

PACK = FormPack(
    pack_id="heldout2.glasswing.renters.v1",
    domain_schema="personal_lines.renters",
    ui_profile_id=PROFILE.profile_id,
    bindings=(
        _lab("applicant.first_name", "First name"),
        _lab("applicant.last_name", "Last name"),
        _lab("applicant.date_of_birth", "Date of birth", ControlKind.DATE, ISO),
        _lab("applicant.email", "Email"),
        # The type-ahead lists whole addresses ("<street> <unit>, <city>,
        # <ST> <zip>", seen on the dev case). Type the start of the street,
        # then choose the one suggestion equal to the intake's whole address.
        _lab(
            "residence.address_full",
            "Street address",
            ControlKind.PORTAL_COMBOBOX,
            TEXT,
        ).model_copy(update={"typeahead_query_chars": 6}),
        _lab("coverage.start_date", "Policy start date", ControlKind.DATE, ISO),
        _lab("building.sprinklered", SPRINKLER, ControlKind.RADIO_GROUP, BOOL),
        _lab("coverage.deductible", "Deductible", ControlKind.RADIO_GROUP, MONEY),
        _lab("coverage.liability_limit", "Liability limit", ControlKind.SELECT, MONEY),
    ),
    popup_subroutines=(
        PopupSubroutineSpec(
            subroutine_id="gw_belongings",
            popup_mode="in_page",
            trigger_anchor=FieldAnchorSpec(label_patterns=(r"^Help me estimate$",)),
            bindings=(
                _lab("contents.rooms", "Number of rooms", ControlKind.SELECT, INT),
                _lab(
                    "contents.furnished",
                    r"How furnished are they\?",
                    ControlKind.SELECT,
                    EXACT,
                ),
                _lab(
                    "contents.valuables",
                    r"Electronics, jewelry and instruments \(\$\)",
                    codec=INT,
                ),
            ),
            compute_anchors=(FieldAnchorSpec(label_patterns=(r"^Calculate$",)),),
            result_anchor=FieldAnchorSpec(label_patterns=(r"^Estimated value$",)),
            commit_anchor=FieldAnchorSpec(label_patterns=(r"^Use this amount$",)),
            parent_return_anchor=FieldAnchorSpec(
                label_patterns=(f"^{PERSONAL_PROPERTY}$",)
            ),
            return_output_key="coverage.personal_property",
            return_codec=MONEY,
        ),
    ),
    screens=(
        WizardScreenSpec(
            screen_id="gw_review",
            landmark_selector="dl",
            is_commit_screen=True,
            commit_button_anchor=FieldAnchorSpec(label_patterns=(r"^Get my price$",)),
        ),
        WizardScreenSpec(
            screen_id="gw_about",
            landmark_selector="#in-first_name",
            next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Continue$",)),
        ),
        WizardScreenSpec(
            screen_id="gw_rental",
            landmark_selector="#in-street",
            next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Continue$",)),
        ),
        WizardScreenSpec(
            screen_id="gw_belongings",
            landmark_selector="#in-personal_property",
            next_button_anchor=FieldAnchorSpec(label_patterns=(r"^Continue$",)),
        ),
    ),
    summary_check=SummaryCheckSpec(
        field_readbacks=(
            _shown("applicant.first_name", "First name"),
            _shown("applicant.last_name", "Last name"),
            _shown("applicant.date_of_birth", "Date of birth", ISO),
            _shown("applicant.email", "Email", EXACT),
            _shown("residence.address_line1", "Street address", EXACT),
            _shown("residence.address_line2", "Unit", EXACT),
            _shown("residence.city", "City", EXACT),
            _shown("residence.state", "State", EXACT),
            _shown("residence.zip", "ZIP"),
            _shown("coverage.start_date", "Policy start date", ISO),
            _shown("building.sprinklered", SPRINKLER, BOOL),
            _shown("coverage.personal_property", PERSONAL_PROPERTY, MONEY),
            _shown("coverage.deductible", "Deductible", MONEY),
            _shown("coverage.liability_limit", "Liability limit", MONEY),
        ),
    ),
    max_commit_mutations=1,
)


def derived_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """The intake's address in the form the address type-ahead lists it."""
    line1 = str(raw.get("residence.address_line1") or "").strip()
    city = str(raw.get("residence.city") or "").strip()
    state = str(raw.get("residence.state") or "").strip()
    zip_ = str(raw.get("residence.zip") or "").strip()
    if not (line1 and city and state and zip_):
        return {}
    line2 = str(raw.get("residence.address_line2") or "").strip()
    street = f"{line1} {line2}" if line2 else line1
    return {"residence.address_full": f"{street}, {city}, {state} {zip_}"}
